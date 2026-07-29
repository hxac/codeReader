# 源码目录结构地图

## 1. 本讲目标

上一讲我们把 transformers 装好了、跑起来了。但当你打开仓库，第一眼看到的会是**几百个目录、几千个文件**——`models/` 下躺着 500 多个模型，根目录还散落着 `generation/`、`pipelines/`、`quantizers/`、`exporters/`、`integrations/`……很容易「迷路」。

本讲要帮你建立一张**全局心智地图**。学完后你应当能够：

- 说出仓库**顶层**的核心目录（`src/`、`docs/`、`examples/`、`tests/`、`utils/`）各自负责什么。
- 说出 `src/transformers/` 内部**每一个子系统目录**的职责，并能在源码中快速定位功能模块。
- 区分两类代码：**框架级模块**（根目录下的扁平 `.py` 文件）和**子系统目录**（`models/`、`generation/` 等）。
- 看懂库入口 `__init__.py` 里的 `_import_structure` 字典——它其实就是**整本库的目录索引**。

> 这一篇承接 `u1-l1`（项目定位）和 `u1-l2`（安装运行）。它不讲解任何具体算法，只解决一个问题：**东西都在哪儿**。后续讲义（`u1-l4` 惰性导入、`u2` Auto 类、`u5` 配置与模型基类……）都会反复引用这里的目录坐标。

---

## 2. 前置知识

读这一篇只需要两个概念：

- **模型定义框架（model-definition framework）**：transformers 把「模型结构 + 超参 + 预处理」集中定义在一处，让整个生态（训练框架、推理引擎）共同复用。这是 `u1-l1` 的核心结论。
- **「东西」分成三层**：① 被 import 的**核心库代码**（在 `src/transformers/`）；② 帮助你学会用的**文档与示例**（`docs/`、`examples/`）；③ 保证代码质量的**测试与工具脚本**（`tests/`、`utils/`）。

本讲用到的两个术语：

- **框架级模块（framework-level module）**：放在 `src/transformers/` 根目录下的那些 `.py` 文件，比如 `configuration_utils.py`、`modeling_utils.py`。它们提供的是所有模型共用的「基类与机制」。
- **子系统目录（subsystem directory）**：按职责归类的一组文件，比如 `generation/`（生成）、`pipelines/`（任务流水线）。

---

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
| --- | --- |
| `README.md` | 项目自我介绍、安装命令、生态定位，是了解项目的第一份材料 |
| `src/transformers/__init__.py` | **库的入口**。用 `_import_structure` 字典登记所有可导入对象，再用 `_LazyModule` 延迟加载 |
| `src/transformers/models/` | 500+ 个模型的「家」，每个模型一个独立目录 |
| `src/transformers/{generation,pipelines,quantizers,exporters,...}/` | 按职责归类的子系统目录 |
| `docs/` `examples/` `tests/` `utils/` | 围绕核心库的文档、示例、测试、仓库自动化脚本 |

---

## 4. 核心概念与源码讲解

### 4.1 仓库顶层：核心库 + 外围工程目录

#### 4.1.1 概念说明

transformers 是一个**超大型开源仓库**。它的顶层不是把所有代码堆在一起，而是清晰地分成「核心库」和「外围工程资产」：

- **核心库代码**全部在 `src/` 下（安装后就是 `import transformers` 拿到的东西）。
- 其余目录都是**围绕核心库**的辅助资产——文档、示例、测试、打包、CI 脚本。它们**不会**被装进用户的 Python 包里。

理解这一点很重要：改代码时，**功能性代码改 `src/`，文档改 `docs/`，测试改 `tests/`**，三者井水不犯河水。

#### 4.1.2 顶层目录速览

```text
huggingface-transformers/            ← 仓库根目录
├── README.md            # 项目说明 + 安装命令 + 生态定位
├── setup.py / pyproject.toml  # 打包与依赖声明（见 u1-l2）
├── Makefile             # 仓库一致性目标：make style / typing / fix-repo（见 u11-l4）
├── CONTRIBUTING.md      # 贡献指南（提交 PR 前必读）
├── AGENTS.md / CLAUDE.md # 给 AI 编程助手的工程指引
├── MIGRATION_GUIDE_V5.md # v5 迁移指南（旧版 API → v5）
│
├── src/                 # ★ 核心库代码（import transformers 的来源）
├── docs/                # 文档源码（多语言，docs/source/en/ 为英文主版本）
├── examples/            # 训练 / 推理示例脚本（按任务组织）
├── tests/               # 测试套件（模型 / 生成 / pipeline / 量化 …）
├── utils/               # 仓库自动化脚本（checkers.py 等一致性检查器）
├── benchmark/  benchmark_v2/  # 性能基准测试
├── docker/              # Docker 镜像定义
├── scripts/             # 杂项脚本（发布、清理等）
├── notebooks/           # 教程 notebook
└── i18n/                # 国际化资源
```

#### 4.1.3 源码精读

仓库版本号与入口声明在库入口文件的开头。注意版本号是开发版（`.dev0`），这能帮你确认手里这份源码对应的版本：

[README.md](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/README.md) 的开头给出项目的一句话定位、生态徽章与安装命令，是了解「项目是什么」的最快入口（`u1-l1` 已精读）。

[src/transformers/__init__.py:21](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/__init__.py#L21) 声明了库的版本号——你看到的 `5.15.0.dev0` 正是本手册对应的版本：

```python
__version__ = "5.15.0.dev0"
```

#### 4.1.4 代码实践

**目标**：确认你「站在正确的仓库根目录」，并区分哪些目录会被打包、哪些不会。

**步骤**：

1. 在仓库根目录执行 `ls`，对照上面的树确认你看到了 `src/ docs/ examples/ tests/ utils/` 这几个核心目录。
2. 打开 `setup.py`，找到 `package_dir` 或 `packages` 相关配置（`u1-l2` 读过它的 `_deps` 与 `extras`）。你会看到打包的根是 `src/`——这从工程上印证了「只有 `src/` 进包」。
3. **观察现象**：`docs/`、`examples/`、`tests/` 在 `setup.py` 里**不会**作为 package 出现。
4. **预期结果**：理解功能性代码与工程资产的物理隔离；以后找 bug 改 `src/`、补说明改 `docs/`。
5. 若本地环境不便运行，可改为「源码阅读型实践」：在 `setup.py` 里用 `find_packages(where="src")` 定位打包范围即可。

#### 4.1.5 小练习与答案

**练习 1**：假设你要给 README 补一句中文描述，该改哪个目录下的哪个文件？
**答案**：改仓库根目录的 `README.md`；若要补充详细文档，则改 `docs/source/<语言>/`（中文在 `docs/source/zh/`）。

**练习 2**：`benchmark/` 目录里的代码会被 `pip install transformers` 装到用户机器上吗？
**答案**：不会。只有 `src/` 下的代码进包；`benchmark/` 属于仓库内部的性能基准资产。

---

### 4.2 src/transformers/ 子系统目录树

#### 4.2.1 概念说明

进入 `src/transformers/`，你会看到**十来个子系统目录**。这是本讲的重头戏。每个目录是一个**职责边界清晰的子系统**：

- 想找某个**模型**（如 Llama）→ 进 `models/`；
- 想找**生成逻辑**（`model.generate()`）→ 进 `generation/`；
- 想找**任务流水线**（`pipeline("text-generation")`）→ 进 `pipelines/`；
- 想找**量化**（4bit 加载）→ 进 `quantizers/`。

把这些目录的职责记牢，你就能在 500+ 个模型的大仓库里**直接跳到对的目录**，而不是满世界搜。

#### 4.2.2 核心流程：子系统目录职责表

下表是 `src/transformers/` 下**每一个子系统目录**的职责（对照本仓库实际目录结构）：

| 子系统目录 | 职责（一句话） | 举例文件 |
| --- | --- | --- |
| `models/` | **500+ 个模型的定义**，每个模型一个独立目录（config + modeling + tokenization 三件套） | `models/llama/modeling_llama.py` |
| `generation/` | 文本生成：`generate()` 主循环、解码策略、logits 处理、停止条件、流式输出、候选生成 | `generation/utils.py`、`logits_process.py` |
| `pipelines/` | 任务流水线：把 tokenizer+model+后处理串成一条端到端管线，**一个任务一个文件** | `pipelines/text_generation.py`、`base.py` |
| `quantizers/` | 量化后端的统一抽象与各实现（bnb / gptq / awq / quanto / hqq …） | `quantizers/auto.py`、`quantizer_bnb_4bit.py` |
| `exporters/` | 模型导出后端：ONNX / Dynamo(torch.export) / ExecuTorch | `exporters/exporter_onnx.py` |
| `distributed/` | 分布式训练：FSDP 初始化、分片工具、分布式 mixin | `distributed/fsdp.py`、`sharding_utils.py` |
| `integrations/` | 第三方集成：DeepSpeed / PEFT / Accelerate、各类注意力与内核后端、实验追踪（WandB/TensorBoard…） | `integrations/deepspeed.py`、`flash_attention.py` |
| `loss/` | 损失函数，按任务拆分（目标检测、语音 RNN-T 等）+ 通用 `loss_utils.py` | `loss/loss_utils.py`、`loss_for_object_detection.py` |
| `data/` | 数据处理：`DataCollator` 系列、数据集封装、metrics、processors | `data/data_collator.py` |
| `cli/` | 命令行入口：`serve` / `chat` / `download`、`add_new_model_like`，以及 `serving/` 子目录 | `cli/serve.py`、`cli/transformers.py` |
| `utils/` | 内部工具：依赖检测、Hub 下载、日志、聊天模板、量化配置、各 `dummy_*_objects` 占位文件 | `utils/import_utils.py`、`hub.py` |

子系统目录树（精简版）：

```text
src/transformers/
├── __init__.py            # ★ 库入口（4.4 节精读）
├── （框架级 .py 文件）     # 4.3 节讲解：基类与共用机制
│
├── models/                # 500+ 模型定义
├── generation/            # 文本生成
├── pipelines/             # 任务流水线（一个任务一个文件）
├── quantizers/            # 量化后端
├── exporters/             # 模型导出（ONNX/Dynamo/ExecuTorch）
├── distributed/           # 分布式（FSDP）
├── integrations/          # 第三方集成（DeepSpeed/PEFT/各注意力后端/日志）
├── loss/                  # 损失函数
├── data/                  # 数据整理器与数据集
├── cli/                   # 命令行（serve/chat/download + serving/）
└── utils/                 # 内部工具（import/hub/logging/dummy_）
```

> **为什么 `utils/` 出现两次？** 注意区分：仓库**根目录**的 `utils/` 是 CI/仓库自动化脚本（如 `checkers.py`）；而 `src/transformers/utils/` 是**库内部**运行时工具（如 `hub.py` 下载逻辑）。两者完全不同，别混淆。

#### 4.2.3 源码精读：用 _import_structure 印证子系统划分

库入口 `__init__.py` 里的 `_import_structure` 字典，**顶层 key 正是这些子系统/模块**。这个字典就是「整本库的目录索引」——它告诉你每个子模块对外暴露哪些名字。下面三段真实代码分别印证了 `configuration`、`generation`、`pipelines` 三个子系统的存在与内容：

[src/transformers/__init__.py:62-66](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/__init__.py#L62-L66) 登记 `configuration_utils` 模块对外暴露 `PreTrainedConfig`（配置基类，详见 u5-l1）：

```python
# Base objects, independent of any specific backend
_import_structure = {
    ...
    "configuration_utils": ["PreTrainedConfig", "PreTrainedConfig"],
```

[src/transformers/__init__.py:113-122](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/__init__.py#L113-L122) 登记 `generation` 子系统暴露的对象——`GenerationConfig`、各类 `Streamer`、`CompileConfig` 等：

```python
    "generation": [
        "AsyncTextIteratorStreamer",
        "CompileConfig",
        "ContinuousBatchingConfig",
        "GenerationConfig",
        ...
        "TextStreamer",
        "WatermarkingConfig",
    ],
```

[src/transformers/__init__.py:141-173](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/__init__.py#L141-L173) 登记 `pipelines` 子系统暴露的对象——**几乎每种任务一条**（`TextGenerationPipeline`、`ImageClassificationPipeline`、`AutomaticSpeechRecognitionPipeline`…），这印证了「一个任务一个文件」的组织方式：

```python
    "pipelines": [
        "AnyToAnyPipeline",
        ...
        "TextGenerationPipeline",
        ...
        "ZeroShotObjectDetectionPipeline",
        "pipeline",
    ],
```

> 注意一个细节：`_import_structure` 里有不少值为空列表 `[]` 的 key（如 `"quantizers": []`、`"distributed": []`、`"loss": []`）。这**不代表这些子系统为空**，而是说它们的公开 API 不在顶层 `transformers.` 命名空间直接暴露，需要通过子模块路径访问（如 `transformers.quantizers.auto.AutoHfQuantizer`）。空列表只是「登记了模块存在，但没有顶层导出名」。

#### 4.2.4 代码实践

**目标**：亲手把「子系统目录 ↔ 职责」对应关系落实下来。

**步骤**：

1. 在 `src/transformers/` 下用 `ls -d */` 列出所有子系统目录。
2. 把它们的数量与名字和 4.2.2 的表对照。
3. 选**三个你最感兴趣的目录**，用一句话写下你猜测它的职责（先别看表）。
4. 然后翻到对应目录，挑一个文件读开头 docstring，校对你的猜测。
5. **观察现象**：例如打开 `src/transformers/generation/utils.py`，看 `GenerationMixin` 类的 docstring，确认它确实是「生成主循环」。
6. **预期结果**：你能不看表，准确说出 8 个以上子系统目录的职责。
7. 若无法本地运行 `ls`，改为「源码阅读型实践」：直接读本讲引用的 `__init__.py` 的 `_import_structure` key 列表，同样能得到子系统清单。

#### 4.2.5 小练习与答案

**练习 1**：你想把一个模型导出成 ONNX 给推理引擎用，应该进哪个子系统目录找代码？
**答案**：`exporters/`（具体是 `exporters/exporter_onnx.py`，详见 u10-l2）。

**练习 2**：`_import_structure` 里 `"quantizers": []` 是空列表，是不是说明没有量化功能？
**答案**：不是。空列表只表示量化子系统**不在顶层命名空间导出名**；实际量化代码都在 `quantizers/` 目录下，通过 `transformers.quantizers.auto.AutoHfQuantizer` 等路径访问（详见 u10-l1）。

**练习 3**：仓库根目录的 `utils/` 和 `src/transformers/utils/` 有何区别？
**答案**：前者是仓库工程化/CI 脚本（如 `checkers.py` 驱动 `make fix-repo`，不进包）；后者是库运行时内部工具（如 `hub.py` 的下载逻辑，进包）。二者职责完全不同。

---

### 4.3 src/transformers/ 顶层的框架级 .py 文件

#### 4.3.1 概念说明

除了子系统目录，`src/transformers/` 根目录下还散落着**几十个扁平的 `.py` 文件**。它们是「**框架级模块**」——不是某个具体模型的代码，而是**所有模型共用的基类与机制**。

打个比方：如果 `models/llama/` 是「一辆具体的车」，那么 `modeling_utils.py`（定义 `PreTrainedModel` 基类）就是「底盘平台」，`configuration_utils.py`（定义 `PreTrainedConfig`）是「参数表模板」。**所有车都建在这些共用件之上**。

#### 4.3.2 框架级模块速查表（按职能分组）

| 分组 | 代表文件 | 作用（一句话） | 对应后续讲义 |
| --- | --- | --- | --- |
| 配置 | `configuration_utils.py` | `PreTrainedConfig`：所有模型超参的基类 | u5-l1 |
| 模型基类 | `modeling_utils.py` | `PreTrainedModel`：所有 PyTorch 模型的基类、`from_pretrained` 加载链路 | u5-l2 |
| 分词 | `tokenization_utils_base.py` | `PreTrainedTokenizerBase`：分词器抽象基类 | u3-l1 |
| 慢→快转换 | `convert_slow_tokenizer.py` | 把 Python 慢速分词器转成 Rust 快速分词器 | u3-l3 |
| 多模态预处理 | `processing_utils.py`、`image_processing_utils.py`、`feature_extraction_utils.py`、`video_processing_utils.py`、`audio_utils.py` | Processor / 图像 / 特征 / 视频 / 音频预处理 | u4 |
| 输出结构 | `modeling_outputs.py` | `ModelOutput` 及各类标准输出（`last_hidden_state` 等） | u5-l4 |
| 注意力机制 | `masking_utils.py`、`modeling_rope_utils.py`、`modeling_flash_attention_utils.py`、`cache_utils.py` | 掩码生成、RoPE 位置编码、Flash Attention、KV Cache | u6 |
| 可复用层 | `modeling_layers.py`、`activations.py`、`initialization.py` | 通用层、激活函数、权重初始化 | u5-l3 / u5-l5 |
| 训练 | `trainer.py`、`training_args.py`、`trainer_callback.py`、`optimization.py`、`trainer_pt_utils.py` | Trainer 主类、训练参数、回调、优化器/调度器 | u9 |
| 加载/权重 | `core_model_loading.py`、`safetensors_conversion.py`、`modeling_gguf_pytorch_utils.py` | 预训练对象加载、格式转换、GGUF 加载 | u2-l3 / u7-l4 |
| 依赖/工具 | `dependency_versions_check.py`、`dependency_versions_table.py` | 运行时依赖版本校验 | u11-l1 |

> **记忆窍门**：看到 `*_utils.py` 后缀，多半是「基类 / 工具机制」；看到 `models/<某模型>/` 才是「具体模型」。**框架级模块 = 共用件，模型目录 = 具体产品**。

#### 4.3.3 源码精读

[src/transformers/__init__.py:88-101](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/__init__.py#L88-L101) 把 `data.data_collator` 模块里的整理器（`DataCollatorWithPadding`、`DataCollatorForSeq2Seq` 等）登记到顶层命名空间——注意它用的是**点分路径** `data.data_collator`，正说明 `data_collator.py` 是子系统目录 `data/` 内的模块：

```python
    "data.data_collator": [
        "DataCollator",
        "DataCollatorForLanguageModeling",
        ...
        "DataCollatorForSeq2Seq",
        ...
        "DataCollatorWithPadding",
        "DefaultDataCollator",
        "default_data_collator",
    ],
```

[src/transformers/__init__.py:186-192](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/__init__.py#L186-L192) 直接登记根目录扁平文件 `tokenization_utils_base`，暴露 `PreTrainedTokenizerBase`、`BatchEncoding`、`AddedToken` 等——这就是「框架级模块」对外提供共用基类的典型例子：

```python
    "tokenization_utils_base": [
        "AddedToken",
        "BatchEncoding",
        "CharSpan",
        "PreTrainedTokenizerBase",
        "TokenSpan",
    ],
```

#### 4.3.4 代码实践

**目标**：体会「框架级模块被所有模型复用」。

**步骤**：

1. 在 `src/transformers/` 根目录用 `ls *.py` 列出所有扁平文件。
2. 打开 `modeling_utils.py`，搜索 `class PreTrainedModel`，确认它是 `torch.nn.Module` 的子类——这是所有模型的共同祖先。
3. 再打开任意一个模型目录（如 `models/llama/modeling_llama.py`），搜索 `PreTrainedModel`，确认 `LlamaPreTrainedModel(PreTrainedModel)`。
4. **观察现象**：Llama 的模型类通过继承「共用件」`PreTrainedModel`，自动获得 `from_pretrained` / `save_pretrained` 能力，而不必自己实现。
5. **预期结果**：理解「共用件 + 具体产品」分层——这正是 `u1-l1` 所说「集中化模型定义」在目录结构上的体现。
6. 若不便运行，改为阅读 `class PreTrainedModel` 与 `class LlamaPreTrainedModel` 两处定义，对比继承关系即可。

#### 4.3.5 小练习与答案

**练习 1**：`masking_utils.py` 和 `models/llama/modeling_llama.py` 谁是框架级、谁是模型级？
**答案**：`masking_utils.py` 是框架级（所有模型共用的掩码生成机制）；`modeling_llama.py` 是模型级（仅 Llama 的实现）。前者是「共用件」，后者是「具体产品」。

**练习 2**：为什么 `PreTrainedModel` 定义在 `modeling_utils.py` 而不在某个模型目录里？
**答案**：因为它是**所有**模型的共同祖先（提供 `from_pretrained`、权重加载等通用能力）。放在根目录的框架级模块里，才能被 `models/` 下每一个模型统一继承复用。

---

### 4.4 库入口 __init__.py 与 _import_structure：整本库的目录索引

#### 4.4.1 概念说明

最后把整张地图收口到**一个文件**：`src/transformers/__init__.py`（871 行）。它是 `import transformers` 真正执行的入口，里面有两样东西值得现在就知道：

1. **`_import_structure` 字典**：本讲反复引用的「整本库目录索引」。它的 key 是子模块路径，value 是该模块对外暴露的名字列表。**读完它的 key，你就读完了库的全部子系统与框架级模块。**
2. **`_LazyModule` 接管**：在文件末尾，库把自己的模块对象替换成一个「懒加载模块」——`import transformers` 时**不会**立即加载 PyTorch 等重量级后端，只有真正访问某个名字（如 `AutoModel`）才按需加载。

> 第二点的完整机制（`_LazyModule.__getattr__`、`is_*_available` 检测）是下一讲 `u1-l4` 的主题。本讲只需知道：**`_import_structure` 是地图，`_LazyModule` 是按这张地图延迟开箱的引擎。**

#### 4.4.2 核心流程：import transformers 时发生了什么

```text
import transformers
   │
   ├─ 执行 src/transformers/__init__.py
   │     ├─ 1. 构建 _import_structure 字典（手工登记 + models/ 自动发现）
   │     └─ 2. 末尾：sys.modules[__name__] = _LazyModule(...)
   │
   └─ 此时 transformers. 命名空间「挂」满了名字，
        但背后的重量级模块（torch、各模型代码）尚未真正加载
              │
              ▼
   首次访问 transformers.AutoModel
   │
   └─ _LazyModule.__getattr__ 触发 → 按需真正 import 对应子模块
```

关键点：`_import_structure` 里 `models/` 下的内容是**自动发现**的——库里新增模型不需要手动维护入口列表。

#### 4.4.3 源码精读

[src/transformers/__init__.py:62-63](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/__init__.py#L62-L63) 开始构建「整本库目录索引」`_import_structure`，开头的注释也点明了「登记对象时要加两次」（一次进字典、一次进 `TYPE_CHECKING` 分支），这是这个文件的核心约定：

```python
# Base objects, independent of any specific backend
_import_structure = {
    "audio_utils": [],
    ...
```

[src/transformers/__init__.py:805-817](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/__init__.py#L805-L817) 是入口的「收口」：先把手工登记的 `_import_structure` 转成集合，再调用 `define_import_structure(... models ...)` **自动扫描** `models/` 目录补全所有模型的导出项，最后用 `_LazyModule` 接管整个 `transformers` 模块：

```python
else:
    _import_structure = {k: set(v) for k, v in _import_structure.items()}

    import_structure = define_import_structure(Path(__file__).parent / "models", prefix="models")
    import_structure[frozenset({})].update(_import_structure)

    sys.modules[__name__] = _LazyModule(
        __name__,
        globals()["__file__"],
        import_structure,
        module_spec=__spec__,
        extra_objects={"__version__": __version__},
    )
```

注意 `define_import_structure(Path(__file__).parent / "models", prefix="models")` 这一行——它解释了为什么 `models/` 下有 502 个目录却不需要在 `_import_structure` 里逐一列出：**入口会自动扫描 `models/` 把它们全部纳入索引**。

[src/transformers/__init__.py:868-871](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/__init__.py#L868-L871) 在最后给出一个温和警告：没装 PyTorch 时，模型不可用、只有 tokenizer/config/文件/数据工具可用——这再次印证了「核心轻量、后端可选」的工程取舍（`u1-l2` 讲过的依赖策略）：

```python
if not is_torch_available():
    logger.warning_advice(
        "PyTorch was not found. Models won't be available and only tokenizers, configuration and file/data utilities can be used."
    )
```

#### 4.4.4 代码实践

**目标**：用代码验证「`_import_structure` 的 key 就是子系统清单」，并体会懒加载的存在。

**步骤**：

1. 写一段最小脚本（**示例代码**，非项目原有）：

   ```python
   import transformers                              # 这一行很轻，不加载 torch
   keys = sorted(transformers._import_structure.keys())
   print("子系统/模块数量：", len(keys))
   for k in keys[:15]:
       print(" -", k)
   ```

2. 运行它，观察打印出的 key 列表——对照本讲 4.2 / 4.3 的表，你会发现它们高度吻合。
3. 再额外打印 `import time; t=time.time(); import transformers; print(time.time()-t)`，对比「装了 torch」与「故意不装 torch」两种情况下的导入耗时。
4. **观察现象**：即使不装 torch，`import transformers` 也不报错（只警告），且能访问 tokenizer/config 相关名字；访问 `AutoModel` 才会因缺 torch 而失败。
5. **预期结果**：直观感受到「懒加载」——入口文件把名字先挂上，真正加载推迟到首次访问。
6. 若本地无法安装 torch，重点跑步骤 1–2 即可（它们不依赖 torch）。
7. **待本地验证**：步骤 3 的耗时数字随机器而异，记录你自己的观测值即可。

#### 4.4.5 小练习与答案

**练习 1**：库里新增了一个模型 `xyz`，开发者需要手动在 `__init__.py` 的 `_import_structure` 里加一行登记它吗？
**答案**：不需要。入口末尾的 `define_import_structure(... "models" ...)` 会自动扫描 `models/` 目录，新增的 `models/xyz/` 会被自动纳入索引。开发者要做的是在 `models/auto/auto_mappings.py` 里登记 AUTO 映射（详见 u11-l2）。

**练习 2**：`_import_structure` 的 key 用的是「模块路径」（如 `data.data_collator`、`tokenization_utils_base`），这传递了什么信息？
**答案**：key 就是「去哪里找代码」的路径。点分多段（如 `data.data_collator`）指向子系统目录 `data/` 内的模块；单段（如 `tokenization_utils_base`）指向根目录的框架级扁平文件。

---

## 5. 综合实践

把本讲的所有知识点串起来，完成一份**你自己的「目录地图交付物」**：

1. **画树**：手画或用 Markdown 画一份 `src/transformers/` 的子系统目录树（含根目录的代表性框架级 `.py` 文件）。
2. **标职责**：为树里**每一个子系统目录**写一句话职责说明（参考 4.2.2 的表，但用自己的话）。
3. **连线**：用箭头把「日常需求 → 对应目录」连起来，至少 5 条，例如：
   - 「我想让模型生成文本」→ `generation/`
   - 「我想用 4bit 加载省显存」→ `quantizers/`
   - 「我要把模型导出给 vLLM/ONNX」→ `exporters/`
   - 「我要给训练加 WandB 日志」→ `integrations/`
   - 「我想用 `pipeline()` 一行推理」→ `pipelines/`
4. **验证**：打开 `src/transformers/__init__.py`，把 `_import_structure` 的所有 key 抄下来，和你的树对照——补上你漏掉的目录，标出空列表 `[]` 的那些（说明它不在顶层命名空间直接暴露）。
5. **选三个最感兴趣的目录**：在树里高亮它们，并写一句「我后续想在这里深入什么」（这会成为你选择后续讲义的依据）。

**预期结果**：得到一份能长期当「速查表」用的目录地图文档；以后遇到任何 transformers 问题，第一反应是「去哪个目录」，而不是茫然搜索。

---

## 6. 本讲小结

- 仓库**顶层**分为「核心库 `src/`」与「外围资产 `docs/ examples/ tests/ utils/`」；只有 `src/` 会被打包进用户的 Python 环境。
- `src/transformers/` 内部有两类代码：**框架级扁平 `.py` 文件**（所有模型共用的基类与机制）和**子系统目录**（按职责归类的代码）。
- 11 个子系统目录职责清晰：`models/`（500+ 模型）、`generation/`（生成）、`pipelines/`（任务流水线）、`quantizers/`（量化）、`exporters/`（导出）、`distributed/`（FSDP）、`integrations/`（第三方集成）、`loss/`（损失）、`data/`（数据整理）、`cli/`（命令行）、`utils/`（内部工具）。
- 库入口 `__init__.py` 里的 `_import_structure` 字典就是**整本库的目录索引**，它的 key = 子系统/模块路径。
- `models/` 下的内容**不需要手动登记**，入口会用 `define_import_structure` 自动扫描 `models/` 补全索引——这就是为什么 502 个模型目录也能被一键 `import`。
- 注意区分两个 `utils/`：仓库根目录的（CI 脚本）与 `src/transformers/utils/`（运行时工具），二者职责不同。

---

## 7. 下一步学习建议

- 下一讲 **`u1-l4` 库入口与惰性导入机制** 会深入本讲的 `__init__.py`，精读 `_LazyModule.__getattr__` 与 `is_*_available` 检测函数——本讲只点到为止的懒加载机制会在那里彻底讲透。
- 想直接体验「地图里的目录」如何协作，可跳读 **`u1-l5` pipeline 快速上手**，它串联了 `pipelines/`、`tokenization_utils_base.py`、`models/` 三个位置。
- 建议顺手在源码里把 `src/transformers/__init__.py` 完整扫一遍它的 `_import_structure` key 列表——这是巩固本讲地图最有效的一分钟练习。
