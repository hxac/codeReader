# u1-l2 仓库目录结构与代码地图

## 1. 本讲目标

上一讲我们知道了 OLMo「是什么」：一个模型代码、训练配置、checkpoint 全部公开的语言模型训练框架。这一讲解决「东西都在哪」的问题。学完本讲，你应该能够：

1. 说出仓库每个顶层目录（`olmo`、`olmo_data`、`scripts`、`configs`、`hf_olmo`、`inference`、`evaluation`、`tests` 等）的职责，拿到一个文件路径时能立刻判断它属于哪一层。
2. 理解 `pyproject.toml` 中 `[tool.setuptools.packages.find]` 的 `include` / `exclude` 规则：`pip install ai2-olmo` 时到底哪些目录会进入发布的包，哪些只是「仓库内的工具目录」。
3. 知道 `Makefile` 提供的开发命令（`run-checks`、`check-cpu-install`、`check-cuda-install` 等），以及它们背后的质量门禁由哪几步组成。

这一讲是后续所有讲义的「地图」：后面讲到训练入口、数据流水线、模型结构时，你都需要先在这张地图上定位。

## 2. 前置知识

本讲不需要任何深度学习知识，但需要几个 Python 工程概念，我们先用大白话过一遍：

- **Python 包（package）**：一个含有 `__init__.py` 的目录就是可以被 `import` 的包。例如 `olmo/` 下有 `__init__.py`，所以你能写 `import olmo`。
- **pyproject.toml**：Python 项目的「户口本」。构建工具（这里是 setuptools）读它来决定：项目叫什么名字、依赖哪些库、版本号从哪来、哪些目录要打进安装包。
- **可编辑安装（editable install）**：`pip install -e .` 不会复制代码，而是把当前目录「挂」进 Python 环境里，改源码立即生效。README 推荐训练/微调用户用这种方式装 OLMo。
- **setuptools 的包发现（packages.find）**：setuptools 自动扫描仓库找「含 `__init__.py` 的目录」，再用 `include`（白名单通配模式）和 `exclude`（黑名单通配模式）过滤，得到最终要打包的包列表。
- **package data**：包里非 `.py` 的文件（如 JSON 词表）默认不会进安装包，必须显式声明为 package data 才会随包分发。
- **Makefile**：一个用 `make 目标名` 执行命令的自动化文件。这里的目标多为「组合好的检查命令」，例如一次跑完代码风格检查、类型检查和全部测试。

记住一个贯穿全讲的判断标准：**「会被 pip 打包的库代码」和「只在仓库里使用的工具/配置/数据」是两类东西**，OLMo 仓库用目录划分 + 打包规则把这两类东西分得非常清楚。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/README.md) | 项目说明书：安装、复现训练、推理、评估入口，是理解各目录用途的第一手证据 |
| [pyproject.toml](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/pyproject.toml) | 构建、打包、代码风格、类型检查、pytest 的全部配置，本讲第二、三模块的主线文件 |
| [Makefile](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/Makefile) | 开发命令入口：`run-checks` 质量门禁、CPU/CUDA 安装自检、Docker 镜像与 Beaker/Gantry 集群任务 |
| [olmo_data/data.py](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/olmo_data/data.py) | 辅助证据：展示 `olmo_data` 包内置数据资产如何被程序定位，解释它为何必须随包分发 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**目录职责**（仓库怎么分层）、**构建与打包**（哪些目录进入 pip 包）、**开发工具链**（Makefile 与质量检查）。

### 4.1 模块一：目录职责

#### 4.1.1 概念说明

一个成熟的开源项目仓库通常不是「一个巨大的源码目录」，而是分成几类角色：

1. **库代码**：用户 `pip install` 之后真正 `import` 的部分（OLMo 的 `olmo`、`hf_olmo`、`olmo_data`）。
2. **入口脚本**：被命令行执行的程序（`scripts/train.py` 是训练总入口）。
3. **配置资产**：声明「怎么训练」的 YAML（`configs/`）。
4. **独立工具集**：功能完整但依赖较重、面向特定场景的子项目（`inference/`、`evaluation/`）。
5. **质量保障**：测试与测试数据（`tests/`、`test_fixtures/`）。

分层的价值在于：库保持轻量可安装，工具保持独立可演进，配置保持可追溯（官方配置就是论文实验的存档）。

#### 4.1.2 核心流程

下面这张目录树是本讲最重要的一张图（已剔除版本管理文件，只列关键内容）：

```
OLMo/
├── olmo/                  # ① 核心库：训练框架本体（会被 pip 打包）
│   ├── model.py           #    OLMo 模型定义（Transformer 结构）
│   ├── train.py           #    Trainer 训练循环
│   ├── config.py          #    BaseConfig / TrainConfig 配置体系
│   ├── optim.py           #    优化器与学习率调度器
│   ├── checkpoint.py      #    检查点保存/恢复
│   ├── tokenizer.py       #    Tokenizer 封装
│   ├── beam_search.py     #    生成解码算法
│   ├── data/              #    数据流水线（memmap 数据集、collator、dataloader）
│   └── eval/              #    训练内评估（lm 困惑度 + downstream 下游任务）
├── olmo_data/             # ② 内置数据资产包：随 pip 包分发的数据文件
│   ├── data.py            #    资产定位工具（importlib_resources）
│   ├── tokenizers/        #    tokenizer 词表 JSON（纯数据目录，无 __init__.py）
│   ├── hf_datasets/
│   └── oe_eval_tasks/
├── hf_olmo/               # ③ HuggingFace 集成：OLMoForCausalLM + 权重转换脚本
├── scripts/               # ④ 命令行脚本：train.py 训练入口、unshard、convert_* 等
├── configs/               # ⑤ 训练配置：tiny/ 小配置、official-*/ 官方大模型配置
├── inference/             # ⑥ 推理工具：量化（compression/）、效率基准（efficiency/）
├── evaluation/            # ⑦ 下游评估工具：tango/catwalk 评估管线
├── tests/                 # ⑧ pytest 测试套件（按模块分文件）
├── test_fixtures/         # ⑨ 测试夹具数据：c4 样例 json.gz、test-olmo-model 小模型
├── docs/                  # 文档：Checkpoints.md、Safetensors.md、RELEASE_PROCESS.md
├── docker/                # 4 个 Dockerfile（base / gantry / test / lumi）
├── conftest.py            # pytest 根级夹具定义
├── Makefile               # 开发命令（模块三详解）
└── pyproject.toml         # 构建/打包/工具链配置（模块二详解）
```

读文件的「定位口诀」：

- 想看**模型长什么样** → `olmo/model.py`
- 想跑**训练** → `scripts/train.py` + `configs/` 下的一份 YAML
- 想**推理/转换权重** → `hf_olmo/`（HF 格式）或 `scripts/convert_*`（转换脚本）
- 想知道**官方用了什么数据、什么超参** → `configs/official-*`
- 想确认**某个行为是否有测试保障** → `tests/` 对应文件

#### 4.1.3 源码精读

**证据一：README 说明训练复现方式，证实 `scripts/` 是入口、`configs/` 是配置。** README 的「Steps to reproduce」一节给出统一复现命令，并说明可用命令行参数覆盖配置项：[README.md:L63-L81](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/README.md#L63-L81)。这段代码（命令）表明：`scripts/train.py` 是被 `torchrun` 直接执行的脚本，`{path_to_train_config}` 来自 `configs/`。README 还专门给出 Mac 单机用法示例 `python scripts/train.py configs/tiny/OLMo-20M.yaml --save_overwrite`：[README.md:L87-L95](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/README.md#L87-L95)。

**证据二：README 把「评估」指向独立工具，证实 `inference/`、`evaluation/` 是面向场景的工具集。** README 的 Evaluation 一节明确说额外评估工具在 OLMo-eval 与 olmes 仓库：[README.md:L192-L194](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/README.md#L192-L194)。而在推理场景中，README 提供的 Modal.com 在线服务示例脚本位于 `scripts/olmo2_modal_openai.py`：[README.md:L196-L208](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/README.md#L196-L208)。另外，目录浏览可以确认 `inference/` 和 `evaluation/` 各自带有一份独立的 `requirements.txt`——这是「独立子项目」的典型标志，它们的重依赖（量化、tango 管线等）不进入主包（原因在模块二展开）。

**证据三：`olmo_data/` 是「随包分发的数据资产」，程序用统一入口定位它。** 词表 JSON 等文件不是靠硬编码路径找的，而是通过 `importlib_resources` 以包资源方式定位：[olmo_data/data.py:L9-L10](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/olmo_data/data.py#L9-L10) 这两行把相对路径挂到 `olmo_data` 包上；对外开放的 `get_data_path` 上下文管理器：[olmo_data/data.py:L22-L27](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/olmo_data/data.py#L22-L27) 负责把包资源落成临时文件路径供使用。这解释了为什么 `tokenizers/` 里的 JSON 必须被声明为 package data（见 4.2.3），否则用户安装后 `get_data_path` 会找不到文件。

#### 4.1.4 代码实践

**实践：亲手绘制并验证目录地图（源码阅读型）**

1. **实践目标**：不看本讲义，独立列全仓库顶层目录并写出每个的一句话职责，形成自己的「代码地图」笔记。
2. **操作步骤**：
   - 在仓库根目录执行 `ls -la`（或 `ls`），抄下全部目录名；
   - 对每个不认识的目录执行 `ls <目录名>` 看内部文件，猜职责；
   - 用一句话写下每个目录的职责，保存为自己的笔记文件（注意：不要写进 `OLMo-tutorial/`，讲义目录由生成器维护）。
3. **需要观察的现象**：`olmo/` 里全是 `.py`；`configs/` 里全是 `.yaml` 与子目录；`test_fixtures/` 里是 `c4-sample.*.json.gz` 与 `test-olmo-model/`；`docs/` 里是 3 篇 Markdown。
4. **预期结果**：你写出的职责描述应与本讲 4.1.2 的目录树一致；`scripts/` 中应能看到 `train.py`、`unshard.py`、`init_config.py`、`convert_olmo2_to_hf.py` 等脚本。

#### 4.1.5 小练习与答案

**练习 1**：你只想阅读 OLMo 模型的 Transformer 结构代码，应该打开哪个文件？如果想看「数据是怎么变成训练批次的」呢？

> **答案**：模型结构在 [olmo/model.py](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/olmo/model.py)；数据流水线在 `olmo/data/` 包（数据集、collator、dataloader 都在这里，后续单元 2 会逐个精读）。

**练习 2**：`test_fixtures/` 和 `tests/` 有什么区别？为什么官方 README 推荐的安装命令装出来的包里没有它们？

> **答案**：`tests/` 是 pytest 测试代码，`test_fixtures/` 是测试用的小数据（c4 样例）与小模型（test-olmo-model）；它们只在开发/贡献代码时需要，不属于库 API，所以被打包规则排除（见模块二）。普通用户 `pip install ai2-olmo` 不需要下载这些测试资产。

**练习 3**：判断题：`configs/official-1124/` 下的 YAML 是「可执行的程序」。对还是错？

> **答案**：错。配置只是声明式数据（模型尺寸、数据路径、超参），真正执行它的是 `scripts/train.py` 读取配置后构建的 Trainer；配置文件本身不包含任何逻辑。

### 4.2 模块二：构建与打包

#### 4.2.1 概念说明

`pip install ai2-olmo` 装到用户机器上的，是整个仓库的一个**子集**。决定「子集边界」的就是 `pyproject.toml` 里的打包配置。理解这条边界有三个实际好处：

1. **读源码时**知道哪些文件是「库的一部分」，改动会影响所有用户；
2. **贡献代码时**知道往哪放文件才不会「本地能跑、用户装不上」（比如把数据文件放进 `olmo_data/` 却没声明 package data）；
3. **排查问题时**能区分「主包依赖」和「工具目录自己的依赖」（`inference/`、`evaluation/` 各有 requirements.txt）。

#### 4.2.2 核心流程

setuptools 构建 `ai2-olmo` 包的流程可以概括为：

```text
pip install -e .[all]
        │
        ▼
读取 [build-system]           → 用 setuptools 构建
        │
        ▼
读取 [project]                → 包名 ai2-olmo、版本来自 olmo.version.VERSION（dynamic）
        │                       运行依赖：torch、omegaconf、tokenizers、transformers 等
        ▼
[tool.setuptools.packages.find]
        ├─ 扫描所有含 __init__.py 的目录
        ├─ include 白名单：只保留 olmo* / hf_olmo* / olmo_data*
        └─ exclude 黑名单：显式排除 tests、test_fixtures、docs*、scripts*、
                           evaluation.*、inference.* 等
        │
        ▼
[tool.setuptools.package-data] → olmo 的 py.typed + olmo_data 下全部文件(**)
        │                       （词表 JSON 等数据因此随包分发）
        ▼
得到最终包内容：5 个 Python 包 + olmo_data 的数据文件
（scripts、configs、inference、evaluation、tests、docs、docker 均不进入安装包）
```

关键结论（下节逐条对源码验证）：真正会被打包的 Python 包只有 **`olmo`、`olmo.data`、`olmo.eval`、`hf_olmo`、`olmo_data`** 这 5 个——它们恰好是仓库中带 `__init__.py` 且匹配 `include` 白名单的全部目录。`olmo_data/tokenizers` 等子目录**没有** `__init__.py`，不是包，它们以 package data 身份进入发行包。

#### 4.2.3 源码精读

**构建后端与项目元信息**：[pyproject.toml:L1-L3](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/pyproject.toml#L1-L3) 声明用 setuptools 作为构建后端；[pyproject.toml:L5-L14](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/pyproject.toml#L5-L14) 定义发布名 `ai2-olmo`（注意：PyPI 包名带 `ai2-` 前缀，`import` 名才是 `olmo`）、`dynamic = ["version"]` 表示版本号运行时再取，`requires-python = ">=3.10"`。

**版本号动态来源**：[pyproject.toml:L72-L73](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/pyproject.toml#L72-L73) 把版本号指向 `olmo.version.VERSION` 变量——这就是上一讲 `check_install()` 能打印版本号的同一来源，版本只有一份定义，不会出现「pyproject 里一个版本、代码里另一个版本」。

**可选依赖组**：[pyproject.toml:L30-L56](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/pyproject.toml#L30-L56) 定义了 `dev`（ruff/mypy/black/pytest 等开发工具）、`train`（wandb、torchmetrics、safetensors 等训练才需要的库）、`all`（= dev + train）与 `figures`。这解释了 README 为什么推荐 `pip install -e .[all]`：训练用户同时需要 dev 工具与 train 依赖。

**包发现白名单（本模块核心）**：[pyproject.toml:L75-L76](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/pyproject.toml#L75-L76) 的 `include = ["olmo*", "hf_olmo*", "olmo_data*"]` 表示：自动扫描到的包里，只有名字以这三个前缀开头的才保留。

**包发现黑名单**：[pyproject.toml:L77-L91](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/pyproject.toml#L77-L91) 显式排除 `tests`、`test_fixtures`、`docs*`、`scripts*`、`evaluation.*`、`inference.*`，以及历史上的 `olmo_tokenizer.*`、`pretrain_data.*` 等遗留名字。

这里要回答实践任务里的问题——**`scripts`、`evaluation`、`inference` 为何被排除？** 分两层看：

1. **白名单本身已把它们挡住**：`scripts`、`evaluation`、`inference` 都不匹配 `olmo*` / `hf_olmo*` / `olmo_data*` 任何模式，所以即使不写 exclude 也进不了包。特别注意 `scripts/` 与 `docs/` 连 `__init__.py` 都没有，setuptools 根本不会把它们识别为包。
2. **exclude 是「双保险 + 意图声明」**：`inference/` 和 `evaluation/` 其实**各有一个 `__init__.py`**（是可导入的目录），一旦将来有人放宽 include 模式（或改用命名空间包发现），没有这层 exclude 它们就会被误打包。更深一层的设计原因：它们**不是库 API**——`scripts/` 是被 `python`/`torchrun` 执行的命令行脚本集合；`inference/` 与 `evaluation/` 是自带独立 `requirements.txt` 的重型工具集（GPTQ 量化、tango 评估管线），若随主包发布，这些重依赖就会强加给每一个只想用 `import olmo` 的用户。

**package data 声明**：[pyproject.toml:L65-L70](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/pyproject.toml#L65-L70) 中 `include-package-data = true` 开启包内数据打包，并把 `olmo_data = ["**"]` 声明为数据——即 `olmo_data` 目录下**所有文件**（包括没有 `__init__.py` 的 `tokenizers/` 词表 JSON）都会进入发行包，同时 `olmo` 包带上类型标记文件 `py.typed`。这与 4.1.3 的 `get_data_path` 呼应：没有这条声明，用户安装后词表文件就丢了。

#### 4.2.4 代码实践

**实践：亲手验证「哪些包真的被装进了环境」**

1. **实践目标**：用命令证据确认 4.2.2 推导的打包清单（5 个包 + 数据文件），而不是只信讲义。
2. **操作步骤**：
   - 先做纸面推导：对照 [pyproject.toml:L75-L91](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/pyproject.toml#L75-L91)，写出你认为会被打包的包名列表；
   - 在已安装 OLMo 的环境里执行：
     ```bash
     python -c "import olmo, hf_olmo, olmo_data; print(olmo.__file__); print(olmo_data.__file__)"
     ```
   - 再执行 `python -m pip show -f ai2-olmo | head -50` 查看已安装文件清单（若是以 `-e` 可编辑方式安装，则改为在输出的 `Location` 里查看目录）。
3. **需要观察的现象**：`olmo.__file__` 指向仓库内的 `olmo/__init__.py`（可编辑安装）或 site-packages 下的路径；`pip show -f` 列出的文件路径前缀应只有 `olmo/`、`olmo_data/`、`hf_olmo/` 三类。
4. **预期结果**：清单里找不到任何 `scripts/`、`configs/`、`inference/`、`evaluation/`、`tests/` 前缀的文件；`olmo_data/tokenizers/*.json` 出现在清单中（package data 生效）。具体输出因安装方式（wheel / editable）而异，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`pip install ai2-olmo` 之后，`import olmo` 能成功，但你想读官方 7B 的训练配置 `configs/official-1124/OLMo2-7B-stage1.yaml` 却发现本地没有这个文件，为什么？

> **答案**：`configs/` 不在打包白名单里（见 [pyproject.toml:L75-L91](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/pyproject.toml#L75-L91)），pip 包只含 5 个 Python 包与 `olmo_data` 数据。要获取配置需要 clone 仓库，或从 GitHub 上直接浏览该文件。

**练习 2**：假如你往 `olmo_data/tokenizers/` 里新增了一个词表 JSON，却不修改 pyproject.toml，用户安装后能用到它吗？

> **答案**：能。因为 [pyproject.toml:L68-L70](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/pyproject.toml#L68-L70) 已把 `olmo_data = ["**"]` 全量声明为 package data，新增文件自动被覆盖。反过来，如果你把文件放到一个**新目录**（比如新建顶层 `assets/`），就必须改打包配置，否则不会被分发。

**练习 3**：`olmo_data/tokenizers/` 没有 `__init__.py`，它是以「包」还是「数据」身份进入发行包的？判断依据是什么？

> **答案**：数据身份。setuptools 的包发现只把含 `__init__.py` 的目录当作包；`tokenizers/` 靠 `olmo_data = ["**"]` 的 package-data 声明随 `olmo_data` 包整体分发。

### 4.3 模块三：开发工具链

#### 4.3.1 概念说明

「开发工具链」指维护这份代码时反复使用的命令。OLMo 把它们集中在 `Makefile`，用 `make 目标名` 调用。核心有三类：

1. **质量门禁 `run-checks`**：代码风格 + 类型 + 测试的一条龙检查，贡献代码前必须过；
2. **安装自检 `check-cpu-install` / `check-cuda-install`**：验证当前环境能否正常导入并使用 OLMo；
3. **镜像与集群任务**：Docker 镜像构建、Beaker/Gantry 集群提交（AI2 内部基础设施，普通读者了解即可）。

#### 4.3.2 核心流程

`make run-checks` 的执行顺序（任何一步失败即中止）：

```text
isort --check .        # 检查 import 排序
   │
black --check .        # 检查代码格式
   │
ruff check .           # lint 静态检查
   │
mypy .                 # 类型检查
   │
CUDA_VISIBLE_DEVICES='' pytest -v tests/   # CPU 模式跑全部测试
```

注意最后一步刻意用 `CUDA_VISIBLE_DEVICES=''` 屏蔽 GPU，保证测试在纯 CPU 环境也可复现；`tests/` 中的 GPU 专属测试用 pytest 的 `gpu` 标记区分（见 4.3.3 的 pyproject 配置）。

#### 4.3.3 源码精读

**质量门禁**：[Makefile:L10-L16](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/Makefile#L10-L16) 定义 `run-checks`，按 isort → black → ruff → mypy → pytest 的顺序执行五个检查。这五步的参数大多不在 Makefile 里，而是配置在 pyproject.toml：例如 ruff 的行宽与忽略规则在 [pyproject.toml:L116-L118](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/pyproject.toml#L116-L118)，pytest 的测试路径与 `gpu` 标记在 [pyproject.toml:L163-L173](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/pyproject.toml#L163-L173)——`testpaths = "tests/"` 限定只在 tests/ 目录收集用例，`markers = ["gpu"]` 注册了 GPU 测试标记。这体现了一种常见分工：**Makefile 只负责「按什么顺序跑什么命令」，细节配置统一收敛在 pyproject.toml**。

**安装自检**：[Makefile:L119-L125](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/Makefile#L119-L125) 定义了两个目标，分别以 `cuda=False` / `cuda=True` 调用上一讲见过的 `olmo.check_install()`，用于快速确认「装好了没、CUDA 可用没」。

**镜像构建**：[Makefile:L24-L43](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/Makefile#L24-L43) 用 `docker/` 目录下的 4 个 Dockerfile 构建 base/gantry/test 镜像并推送到 AI2 的 Beaker 平台，这是 CI 与集群测试的运行环境来源。

**集群上的真实训练命令（值得一读）**：[Makefile:L91-L117](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/Makefile#L91-L117) 的 `gantry-run-ib` 目标展示了 AI2 用 Gantry 在多机集群上发起训练的完整命令，最后一行是：

```bash
torchrun --master-addr $BEAKER_LEADER_REPLICA_HOSTNAME --master_port 1234 \
  --nnodes 4 --node-rank $BEAKER_REPLICA_RANK --nproc-per-node 8 \
  scripts/train.py configs/c4-large.yaml
```

它与 README 里给普通用户的复现命令（`torchrun --nproc_per_node=8 scripts/train.py ...`）完全同构——**官方自己训练也是走同一个入口脚本**，只是多了多机参数。这印证了 OLMo「配置与流程全公开可复现」的定位。

#### 4.3.4 代码实践

**实践：跑通安装自检，并「彩排」一次质量门禁**

1. **实践目标**：会使用 Makefile 的自检与检查命令，理解 run-checks 每一步在做什么。
2. **操作步骤**：
   - 执行 `make check-cpu-install`（等价于 `python -c 'from olmo import check_install; check_install(cuda=False)'`）；
   - 执行 `make -n run-checks`：`-n` 是 dry-run，只打印将执行的命令而不真正运行，用它核对 4.3.2 的五步顺序；
   - 如果你已安装 dev 依赖（`pip install -e .[dev]`），可以挑单步执行试试，例如 `ruff check olmo/`。
3. **需要观察的现象**：`check-cpu-install` 应打印 OLMo 版本号（来自 `olmo.version.VERSION`）以及各依赖版本、CUDA 可用性信息；`make -n run-checks` 依次打印 isort、black、ruff、mypy、pytest 五条命令。
4. **预期结果**：自检以版本号开头、无异常退出（exit code 0）。本讲不假设你已装好环境，具体输出**待本地验证**；若报 `ModuleNotFoundError: olmo`，说明尚未安装，回到上一讲的安装步骤。

#### 4.3.5 小练习与答案

**练习 1**：`make run-checks` 里 pytest 前为什么要加 `CUDA_VISIBLE_DEVICES=''`？

> **答案**：强制测试在 CPU 上运行。好处有二：一是没有 GPU 的贡献者/CI 也能跑同一套门禁；二是测试结果不因机器是否有卡而变化。需要 GPU 的测试用 `gpu` 标记单独标识（[pyproject.toml:L171-L173](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/pyproject.toml#L171-L173)）。

**练习 2**：`make check-cpu-install` 和 `make check-cuda-install` 调的是不是同一个函数？差别在哪？

> **答案**：是同一个函数 `olmo.check_install`，差别只在 `cuda` 参数为 `False` 还是 `True`（[Makefile:L119-L125](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/Makefile#L119-L125)）；`cuda=True` 时会额外检查 CUDA 相关的安装状态。

**练习 3**：你改了一行 `olmo/model.py` 想提交 PR，本地至少应该跑哪个 make 目标？它不包含下面哪项：格式检查、类型检查、单元测试、Docker 镜像构建？

> **答案**：至少跑 `make run-checks`；它**不包含** Docker 镜像构建（镜像相关是 `images`/`gantry-image`/`test-image` 等独立目标，面向 AI2 内部的 Beaker 基础设施）。

## 5. 综合实践

把本讲三个模块串起来，完成一份属于你的「OLMo 代码地图」文档：

1. **画目录树**：在仓库根目录执行 `ls`，为每个顶层目录写一句话职责（对照 4.1.2 自查，重点目录：`olmo`、`olmo_data`、`scripts`、`configs`、`hf_olmo`、`inference`、`evaluation`、`tests`、`test_fixtures`、`docs`、`docker`）。
2. **推导打包清单**：打开 [pyproject.toml:L75-L91](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/pyproject.toml#L75-L91)，写出 `include` 白名单匹配、且实际含 `__init__.py` 的包，应得到 `olmo`、`olmo.data`、`olmo.eval`、`hf_olmo`、`olmo_data` 共 5 个；再说明 `scripts`、`evaluation`、`inference` 被排除的两层原因（白名单不匹配 + exclude 显式声明；它们是脚本/自带依赖的工具集，不属于库 API，其中 `scripts`、`docs` 连 `__init__.py` 都没有，`inference`、`evaluation` 反而有——这正是 exclude 作为双保险的价值）。
3. **命令验证**：运行 `make check-cpu-install` 确认环境可用，再运行 `make -n run-checks` 把五步门禁抄进你的文档。
4. **自查标准**：随机挑 3 个文件路径（如 `olmo/train.py`、`configs/tiny/OLMo-20M.yaml`、`inference/compression/run_quantization.py`），你都能不假思索地说出「它在哪个目录、那个目录管什么、会不会进 pip 包」。

产出物：一份 Markdown 笔记（自存，不要写进 `OLMo-tutorial/`），后续每一讲开始前都可以扫一眼它来定位。

## 6. 本讲小结

- 仓库按角色分层：库代码（`olmo`、`hf_olmo`、`olmo_data`）、入口脚本（`scripts/train.py`）、配置资产（`configs/`）、独立工具集（`inference/`、`evaluation/`，各带 requirements.txt）、质量保障（`tests/`、`test_fixtures/`）、文档与基础设施（`docs/`、`docker/`）。
- `pip install ai2-olmo` 只装 5 个 Python 包（`olmo`、`olmo.data`、`olmo.eval`、`hf_olmo`、`olmo_data`）加 `olmo_data` 的数据文件；白名单是 `include = ["olmo*", "hf_olmo*", "olmo_data*"]`，exclude 是对 tests/scripts/evaluation/inference 等工具目录的双保险。
- `olmo_data` 里的词表 JSON 靠 `package-data olmo_data = ["**"]` 随包分发，程序内用 `importlib_resources`（`get_data_path`）定位，而不是硬编码路径。
- `make run-checks` 是贡献代码的质量门禁：isort → black → ruff → mypy → CPU 模式 pytest，细节配置收敛在 pyproject.toml。
- `make check-cpu-install` / `check-cuda-install` 复用 `olmo.check_install()` 做安装自检；Makefile 里的 Gantry 目标显示官方集群训练与 README 给用户的命令同构，都走 `scripts/train.py`。
- 判断「一个文件属不属于发布包」的三步法：有没有 `__init__.py` → 匹不匹配 include → 是否被 package-data 声明覆盖。

## 7. 下一步学习建议

下一讲 **u1-l3《跑通第一个训练：tiny 配置实战》** 将第一次真正「点火」：用 `python scripts/train.py configs/tiny/OLMo-20M.yaml` 在单机上启动一次训练。届时本讲的地图会全部派上用场——入口在 `scripts/`，配置在 `configs/tiny/`，被调用的是 `olmo/` 里的库代码。

在进入下一讲之前，建议你先自己浏览一遍：

- [scripts/train.py](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/scripts/train.py)——只看 `main()` 的整体轮廓，数一数它分几大步；
- [configs/tiny/OLMo-20M.yaml](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/configs/tiny/OLMo-20M.yaml)——最迷你的官方配置，扫一眼有哪些配置段（model、optimizer、data……）。

带着「这份 YAML 的每一行最终被 train.py 的哪段代码消费」这个问题进入下一讲，效果最好。
