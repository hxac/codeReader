# 微调框架总览：目录、依赖与训练流程

## 1. 本讲目标

本讲是微调单元（单元四、单元五）的第一篇，带你从「会用推理」跨入「能读懂并运行训练代码」。读完本讲你应该能够：

1. 说出 `qwen-vl-finetune/` 子项目的完整目录结构，以及 `qwenvl` 包内 `train/`、`data/` 两个子包各自的职责分工。
2. 理解 `tools/` 目录三个工具（`check_image.py`、`pack_data.py`、`process_bbox.ipynb`）分别在训练流水线的哪个环节发挥作用。
3. 按 README 的 Requirements 清单搭好一套可训练的依赖环境，并理解 torch、transformers、deepspeed、flash_attn、peft 等每个依赖为什么必须存在。
4. 跟踪 `train_qwen.py` 从参数解析、模型加载、模块冻结到训练循环与保存的完整主流程，并知道框架支持哪四类模型代际（Qwen2-VL / Qwen2.5-VL / Qwen3-VL / Qwen3-VL-MoE）以及它们是如何被自动判定的。

本讲不深入任何单个模块的实现细节——数据集注册（u4-l3）、标签构造（u5-l3）、MRoPE（u5-l4）、FlashAttention 替换（u5-l6）、LoRA（u5-l7）、DeepSpeed（u5-l8）都有专门讲义。本讲只解决一个问题：**整个微调框架长什么样、怎么搭起来、怎么跑起来**。

## 2. 前置知识

在开始读源码之前，先用人话建立几个概念。如果你已经熟悉 Hugging Face 生态，可以快速扫过本节。

### 2.1 什么是 SFT（有监督微调）

Qwen3-VL 这样的多模态大模型在发布前经历过「预训练」：在海量图文数据上学习下一个词预测。预训练后的模型知识很广，但不一定贴合你的业务。**SFT（Supervised Fine-Tuning，有监督微调）** 就是用你准备好的一批「问题 + 标准答案」样本，继续训练模型，让它学会按你期望的方式回答。本框架做的就是这件事：输入是 JSON 标注 + 图像/视频文件，输出是一个微调后的模型权重目录。

### 2.2 Hugging Face Trainer 是什么

Hugging Face 的 `transformers` 库提供了一个通用的训练循环类 `Trainer`：你把模型、数据集、参数交给它，它负责封装「取一个 batch → 前向计算 loss → 反向传播 → 优化器更新 → 记日志 → 存 checkpoint」这一整套循环，还顺带处理了分布式、混合精度、梯度累积等脏活。`qwen-vl-finetune` 并没有重写训练循环，而是**直接使用 `Trainer`，再用运行时补丁（monkey patch）往里面塞了多模态训练需要的定制能力**——这是理解本框架架构的第一把钥匙。

> 术语解释——**monkey patch（猴子补丁）**：在程序运行时动态替换已有类或函数的行为，而不修改其源码文件。本框架大量使用这种手法，后面会反复见到。

### 2.3 torchrun 与分布式训练

单张 GPU 的显存往往装不下几十 B 参数的模型加优化器状态，因此训练通常需要多卡甚至多机。`torchrun` 是 PyTorch 自带的分布式启动器：它会按 `--nproc_per_node` 在每张卡上各启动一份训练脚本，并自动建立进程间通信。你在本讲会看到所有训练脚本都以 `torchrun ... qwenvl/train/train_qwen.py ...` 的形式启动。

### 2.4 DeepSpeed ZeRO 一句话版

DeepSpeed 是微软的分布式训练加速库，核心卖点之一是 **ZeRO（Zero Redundancy Optimizer）**：把优化器状态、梯度、参数在多卡间切分存储，避免每张卡都存一份完整副本，从而大幅降低单卡显存。它分三档——ZeRO-1 切优化器状态、ZeRO-2 加切梯度、ZeRO-3 再加切参数。本框架在 `scripts/` 下提供了 `zero2.json`、`zero3.json`、`zero3_offload.json` 三份配置，详细对比留给 u5-l8，本讲只需要知道「启动脚本通过 `--deepspeed xxx.json` 选择一档」。

### 2.5 LoRA 一句话版

全量微调要更新模型全部权重，显存开销巨大。**LoRA（Low-Rank Adaptation）** 的思路是冻结原权重，只在旁边插入一对低秩小矩阵来学习增量，训练参数量可降到原来的百分之几。本框架通过 `--lora_enable True` 开启（实现在 [train_qwen.py:163-178](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L163-L178)，详解见 u5-l7）。

### 2.6 与前几讲的衔接

u1-l2 已经画过仓库地图：本仓库四大子项目各司其职，口诀是「输入找 utils、训练找 finetune、跑分找 evaluation、用法找 cookbooks」。从本讲起我们进入 `qwen-vl-finetune/`。另外请记住 u1-l1 的一个事实：**模型结构代码在 transformers 库里而不在本仓库**——所以你会看到训练脚本从 `transformers` 导入 `Qwen3VLForConditionalGeneration` 这些类，这毫不奇怪，本仓库提供的是围绕这些模型的「训练工程」。

## 3. 本讲源码地图

本讲涉及的文件及其作用一览（均在 `qwen-vl-finetune/` 之下）：

| 文件 | 行数 | 作用 | 本讲角色 |
|---|---|---|---|
| [README.md](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md) | 318 | 框架使用说明：目录说明、依赖清单、数据格式、启动示例 | 依赖与约定的权威来源 |
| [qwenvl/train/train_qwen.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py) | 206 | 训练入口：参数解析→模型加载→冻结策略→训练→保存 | 本讲精读主角 |
| [qwenvl/train/argument.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/argument.py) | 45 | 三组 dataclass 参数定义（模型/数据/训练） | 结构认知，u5-l2 精读 |
| [qwenvl/train/trainer.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py) | 510 | 对 transformers 的运行时补丁：varlen 注意力、多参数组优化器 | 结构认知，u5-l6 精读 |
| [qwenvl/data/__init__.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/__init__.py) | 63 | 数据集注册表 `data_dict` 与 `data_list()` | 结构认知，u4-l3 精读 |
| [qwenvl/data/data_processor.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py) | 693 | Dataset 与 DataCollator：样本 JSON → 训练张量 | 结构认知，u5-l3 精读 |
| [qwenvl/data/rope2d.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/rope2d.py) | 526 | 三代 MRoPE 位置编码的位置 id 计算 | 结构认知，u5-l4 精读 |
| [tools/check_image.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/check_image.py) | 168 | 训练前数据完整性校验 | 本讲模块 4.3 |
| [tools/pack_data.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py) | 191 | 按 token 长度装箱打包数据 | 本讲模块 4.3，u4-l4 精读 |
| [tools/process_bbox.ipynb](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/process_bbox.ipynb) | — | grounding 标注的 bbox 格式转换笔记本 | 本讲仅定位，u7-l3 精读 |
| [scripts/](https://github.com/QwenLM/Qwen3-VL/tree/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts) | — | 6 个启动脚本 + 3 份 DeepSpeed 配置 | 本讲对照表 |
| [demo/single_images.json](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/demo/single_images.json)、[demo/video.json](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/demo/video.json) | — | 可直接用于训练的最小示例数据 | 实践素材 |

## 4. 核心概念与源码讲解

### 4.1 目录结构：一个核心包加三个外围目录

#### 4.1.1 概念说明

`qwen-vl-finetune/` 没有任何打包配置（没有 `pyproject.toml`、`setup.py`），它不是一个可 pip 安装的库，而是一个**「源码即工程」的训练项目**：核心代码全部在 `qwenvl` 这个 Python 包里，外围由「启动脚本（scripts）、数据工具（tools）、示例数据（demo）」三个目录包裹。使用方式是 `cd qwen-vl-finetune && torchrun qwenvl/train/train_qwen.py ...`，直接以源码方式运行。

`qwenvl` 包内部只有两个子包，分工非常干净：

- **`train/`（怎么学）**：训练入口 `train_qwen.py`、参数定义 `argument.py`、对 HF Trainer 的定制 `trainer.py`。
- **`data/`（学什么）**：数据集注册表 `__init__.py`、样本到张量的转换 `data_processor.py`、位置编码计算 `rope2d.py`。

#### 4.1.2 核心流程

完整目录树如下（`demo/` 下媒体文件已折叠）：

```text
qwen-vl-finetune/
├── README.md                  # 使用说明：结构、依赖、数据格式、启动示例
├── demo/                      # 开箱即用的最小训练数据
│   ├── images/                #   3 张示例图片（含 1 张 COCO 图）
│   ├── videos/                #   3 个示例视频
│   ├── single_images.json     #   单图问答标注
│   └── video.json             #   视频问答标注
├── qwenvl/                    # ★ 核心 Python 包
│   ├── train/                 #   「怎么学」
│   │   ├── train_qwen.py      #     训练入口（本讲主角）
│   │   ├── trainer.py         #     HF Trainer / 模型类的运行时补丁
│   │   └── argument.py        #     三组 dataclass 参数
│   └── data/                  #   「学什么」
│       ├── __init__.py        #     数据集注册表 data_dict + data_list()
│       ├── data_processor.py  #     Dataset / DataCollator（JSON→张量）
│       └── rope2d.py          #     三代 MRoPE 位置 id 计算
├── scripts/                   # 各规格模型的启动脚本
│   ├── sft.sh                 #   Qwen2.5-VL-3B + zero3
│   ├── sft_7b.sh              #   Qwen2.5-VL-7B + zero3
│   ├── sft_32b.sh             #   Qwen2.5-VL-32B + zero3
│   ├── sft_qwen3_4b.sh        #   Qwen3-VL-4B + zero3
│   ├── sft_30a3b.sh           #   Qwen3-VL-30B-A3B（MoE）+ zero2
│   ├── sft_30a3b_lora.sh      #   上者的 LoRA 版 + zero2
│   └── zero2/3/zero3_offload.json  # 三档 DeepSpeed 配置
└── tools/                     # 训练前的数据预处理工具
    ├── check_image.py         #   数据完整性校验
    ├── pack_data.py           #   按 token 长度装箱打包
    └── process_bbox.ipynb     #   grounding bbox 格式转换
```

一次完整的微调流水线在目录间的流转：

```text
原始标注(.json/.jsonl) + 媒体文件
        │
        ├──(可选) tools/check_image.py 校验 → _valid.json / _problems.json
        ├──(可选) tools/pack_data.py 打包 → _pack.json
        ├──(可选) tools/process_bbox.ipynb 转换 grounding 标注
        ▼
qwenvl/data/__init__.py 注册表里登记 annotation_path / data_path
        ▼
scripts/xxx.sh 组装参数 → torchrun 启动
        ▼
qwenvl/train/train_qwen.py 主流程（调 data/ 构建数据、调 Trainer 训练）
        ▼
output_dir/ 微调后的模型权重 + processor
```

#### 4.1.3 源码精读

README 用三个小节宣告了这套结构（注意 README 把 `tools` 写在 `qwenvl` 目录说明之后，但它实际位于 `qwen-vl-finetune/tools/`，与 `qwenvl` 平级——README 的缩进容易让人误会）：

[qwen-vl-finetune/README.md:8-24](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L8-L24)：README 的 Repository Structure 小节，逐条说明 `train/`（trainer、入口、参数）、`data/`（配置、处理、RoPE）、`tools`（bbox 转换、打包）的职责。

入口文件对包结构的自我介绍藏在 import 区。看 `train_qwen.py` 的开头：

[qwen-vl-finetune/qwenvl/train/train_qwen.py:17-42](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L17-L42)：这段 import 揭示了主流程的全部依赖方——`trainer` 模块（注意力替换函数）、transformers 的四类模型类、`qwenvl.data.data_processor` 的 `make_supervised_data_module`（数据模块工厂）、`argument` 的三组参数 dataclass、`AutoProcessor` 与 `Trainer`。

其中有两行值得特别停下：

```python
project_root = Path(__file__).parent.parent.parent
sys.path.append(str(project_root))
```

[qwen-vl-finetune/qwenvl/train/train_qwen.py:25-26](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L25-L26)：`__file__` 是 `train_qwen.py` 自身路径，连续三次 `parent` 依次得到 `train/`、`qwenvl/`、`qwen-vl-finetune/`，把这个目录追加进 `sys.path`，之后 `from qwenvl.data...` 这种以 `qwenvl` 开头的导入才能成立。

[qwen-vl-finetune/qwenvl/train/train_qwen.py:28](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L28)：注意这行不是 `from qwenvl.train.trainer import ...`，而是裸模块名 `from trainer import ...`。它能工作依赖 Python 的另一个行为：**直接运行脚本时，脚本所在目录（`qwenvl/train/`）会被自动加入 `sys.path`**。这也解释了为什么必须用 `torchrun qwenvl/train/train_qwen.py` 这种「以脚本为入口」的方式启动，而不是把它当安装包用。

`data/` 子包内部的依赖关系，可以从 `data_processor.py` 的 import 一眼看全：

[qwen-vl-finetune/qwenvl/data/data_processor.py:18-19](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L18-L19)：`from . import data_list` 说明数据处理器直接消费注册表提供的配置列表；`from .rope2d import get_rope_index_25, get_rope_index_2, get_rope_index_3` 说明三种代际的位置 id 计算都在 `rope2d.py` 里按代际各提供一个函数。

#### 4.1.4 代码实践

**实践目标**：亲手绘制目录地图，并验证「源码即工程」的启动前提（能从 `qwen-vl-finetune/` 目录 import `qwenvl` 包）。

**操作步骤**：

1. 在本地仓库执行 `find qwen-vl-finetune -type f -name "*.py" -o -type f -name "*.sh" -o -type f -name "*.json" | grep -v demo | sort`，对照 4.1.2 的目录树逐项确认。
2. 写一个 10 行以内的探针脚本 `probe.py`（示例代码，放在 `qwen-vl-finetune/` 目录下，验证完可删除）：

```python
# probe.py —— 验证包可导入、模块各就各位（示例代码）
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent))  # 模拟 train_qwen.py 的 sys.path 处理

import qwenvl.data as qd
from qwenvl.data import data_list, data_dict
from qwenvl.train.argument import ModelArguments, DataArguments, TrainingArguments

print("已注册数据集:", list(data_dict.keys()))
print("data_list(['cambrian_737k%50']) =>", data_list(["cambrian_737k%50"]))
```

3. 在 `qwen-vl-finetune/` 目录下运行 `python probe.py`（需要已安装 transformers，见 4.2 的环境实践）。

**需要观察的现象**：第一行打印出 `['cambrian_737k', 'cambrian_737k_pack', 'mp_doc', 'clevr_mc', 'videochatgpt']` 这样的注册表键列表；第二行打印出的配置字典里，`annotation_path` 是 `"PATH_TO_CAMBRIAN_737K_ANNOTATION"` 占位符，且多了 `"sampling_rate": 0.5` 字段。

**预期结果**：三行 import 全部不报错，说明包结构完好、模块依赖方向正确（`data/__init__.py` 不依赖其他 qwenvl 模块，是最底层）。若 `import qwenvl` 失败，多半是没在 `qwen-vl-finetune/` 目录下运行，或步骤 2 的 `sys.path` 处理有误。实际打印值待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果不看源码，仅凭 `scripts/sft_qwen3_4b.sh` 的哪一行就能判断「这个项目是以源码方式而非 pip 安装方式被使用的」？

**答案**：`entry_file=qwenvl/train/train_qwen.py` 这一行（[scripts/sft_qwen3_4b.sh:20](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_qwen3_4b.sh#L20)）。torchrun 直接以仓库内相对路径的脚本文件作为入口，而不是一个安装后的命令行工具，说明这是「源码即工程」。

**练习 2**：`train_qwen.py` 第 28 行写的是 `from trainer import replace_qwen2_vl_attention_class` 而不是 `from qwenvl.train.trainer import ...`。假如你把 `train_qwen.py` 复制到仓库根目录再运行，会发生什么？

**答案**：会 `ModuleNotFoundError: No module named 'trainer'`。裸导入 `trainer` 依赖「脚本所在目录自动进入 sys.path」这一机制，脚本必须留在 `qwenvl/train/` 目录里，与 `trainer.py` 同级。此外根目录下 `project_root` 的三级上溯也会指错位置，`from qwenvl.data...` 同样会失败。

**练习 3**：判断对错：「`data/__init__.py` 需要导入 `data_processor.py` 才能完成数据集注册。」

**答案**：错。依赖方向恰恰相反——`data_processor.py` 导入 `__init__.py` 里的 `data_list`（[data_processor.py:18](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L18)）。`__init__.py` 只含路径配置与解析逻辑，不 import 包内其他模块，是 `data/` 子包的最底层。

### 4.2 依赖版本清单：每个包为什么必须存在

#### 4.2.1 概念说明

README 的 Requirements 小节给出了一份**精确到版本号的依赖清单**。这与根仓库 README 那种「`transformers>=4.57.0`」的宽松写法完全不同——训练框架对版本极其敏感，原因有二：

1. `trainer.py` 直接 import 了 transformers 的**内部模块路径**（如 `transformers.models.qwen3_vl_moe.modeling_qwen3_vl_moe`），这些路径没有任何兼容性承诺，版本一变就可能整体搬家；
2. `flash_attn`、`deepspeed`、`torch` 三者存在编译层面的配套关系（flash-attn 按 CUDA/torch 版本出预编译包，triton 版本需与 torch 匹配）。

所以搭训练环境的第一原则是：**照抄清单，不要自由发挥**。

#### 4.2.2 核心流程

依赖清单及各包在框架中的角色（「使用者」一列全部可在源码里验证）：

| 包 | 锁定版本 | 为什么必须存在 | 谁在用它 |
|---|---|---|---|
| `torch` | 2.6.0 | 训练底座：张量、自动求导、分布式 | 所有模块 |
| `torchvision` | 0.21.0 | 图像 IO 与视频读取兜底后端 | 数据处理链路 |
| `transformers` | 4.57.0.dev0 | 四类 Qwen-VL 模型结构 + `Trainer` + `AutoProcessor`/`AutoTokenizer` | `train_qwen.py`、`trainer.py` |
| `deepspeed` | 0.17.1 | ZeRO 分布式显存优化 | 启动脚本经 `--deepspeed` 传入 |
| `flash_attn` | 2.7.4.post1 | varlen（变长拼接）注意力内核 | `trainer.py` 直接 import |
| `triton` | 3.2.0 | GPU 编译后端，flash-attn / deepspeed 编译依赖 | 隐式依赖 |
| `accelerate` | 1.7.0 | `Trainer` 的设备/分布式调度层 | `Trainer` 内部 |
| `torchcodec` | 0.2 | 高性能视频解码 | `tools/pack_data.py` |
| `peft` | 0.17.1 | LoRA 适配器 | `train_qwen.py` 的 LoRA 分支 |

另外有三个**README 没列但 tools 脚本实际需要**的隐含依赖：`binpacking`（`pack_data.py` 的装箱算法）、`datasets`（`check_image.py` 用它读 JSON）、`Pillow`/`numpy`/`tqdm`（图像与进度条，通常随 torch 生态已在环境中）。

一个值得对比的细节：根仓库推理只需 `transformers>=4.57.0` 正式版，而这里锁的是 `4.57.0.dev0` 开发版——Qwen3-VL 的模型类刚合入时的版本形态。如果你在装环境时发现 `4.57.0.dev0` 已不可得，装 `>=4.57.0` 的正式版通常也能满足 `trainer.py` 用到的导入路径，但需按 4.2.4 的实践实际验证，不要想当然。

#### 4.2.3 源码精读

[qwen-vl-finetune/README.md:26-38](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L26-L38)：Requirements 小节原文，九个包的锁定版本以列表给出，这就是搭环境的唯一权威依据。

「flash_attn 是硬依赖」这一点，看 `trainer.py` 的第一屏 import 即可证实——它不是可选加速项，而是模块加载阶段就顶层导入的：

[qwen-vl-finetune/qwenvl/train/trainer.py:1-27](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L1-L27)：`from flash_attn.flash_attn_interface import flash_attn_varlen_func` 在文件加载时立即执行（第 4 行），意味着**没装 flash-attn 连训练脚本都无法启动**；随后的连续四段 import 分别从 `transformers.models.qwen2_vl / qwen2_5_vl / qwen3_vl / qwen3_vl_moe` 四个内部模块取类——这就是清单必须锁 transformers 版本的直接证据。

torchcodec 与 binpacking 的隐含依赖证据在工具脚本里：

[qwen-vl-finetune/tools/pack_data.py:1-11](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py#L1-L11)：`from torchcodec.decoders import VideoDecoder` 与 `import binpacking` 都出现在顶层——想跑打包工具就必须额外安装这两个包，而它们都不在 README 清单里。

peft 的使用则是惰性导入，只在开 LoRA 时才需要：

[qwen-vl-finetune/qwenvl/train/train_qwen.py:163-164](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L163-L164)：`from peft import LoraConfig, get_peft_model, TaskType` 写在 `if training_args.lora_enable:` 分支内部——不开 LoRA 的全量微调理论上可以不装 peft（但按清单装齐最省心）。

#### 4.2.4 代码实践

**实践目标**：按 README 清单搭好训练环境，并用一个版本探针脚本核验每个关键包的版本与关键导入路径。

**操作步骤**：

1. 建议新建独立环境避免污染推理环境（示例命令）：

```bash
conda create -n qwen3vl-sft python=3.10 -y
conda activate qwen3vl-sft

# 先装 torch 全家（CUDA 版本按你的机器调整）
pip install torch==2.6.0 torchvision==0.21.0

# flash-attn 建议装预编译 wheel，源码编译非常耗时
pip install flash-attn==2.7.4.post1 --no-build-isolation

# 其余按清单
pip install transformers==4.57.0.dev0 deepspeed==0.17.1 triton==3.2.0 \
            accelerate==1.7.0 torchcodec==0.2 peft==0.17.1

# tools 脚本的隐含依赖
pip install binpacking datasets pillow numpy tqdm
```

   若 `transformers==4.57.0.dev0` 安装失败（开发版会从 PyPI 消失），改试 `pip install "transformers>=4.57.0"`，然后用步骤 2 的探针重点验证模型类导入。`torchcodec` 依赖系统级 ffmpeg 库，Linux 上可能还需 `apt install ffmpeg`。

2. 写版本探针脚本 `check_env.py`（示例代码）：

```python
# check_env.py —— 核验训练环境（示例代码）
import torch, transformers, accelerate
import deepspeed, peft, triton
import flash_attn, torchvision

print(f"torch          {torch.__version__}  cuda={torch.version.cuda}  可用GPU={torch.cuda.is_available()}")
print(f"torchvision    {torchvision.__version__}")
print(f"transformers   {transformers.__version__}")
print(f"deepspeed      {deepspeed.__version__}")
print(f"flash_attn     {flash_attn.__version__}")
print(f"triton         {triton.__version__}")
print(f"accelerate     {accelerate.__version__}")
print(f"peft           {peft.__version__}")

# 关键导入路径核验：与 trainer.py / train_qwen.py 逐字一致
from flash_attn.flash_attn_interface import flash_attn_varlen_func
from transformers import (
    Qwen2VLForConditionalGeneration,
    Qwen2_5_VLForConditionalGeneration,
    Qwen3VLForConditionalGeneration,
    Qwen3VLMoeForConditionalGeneration,
)
from transformers.models.qwen3_vl_moe.modeling_qwen3_vl_moe import Qwen3VLMoeVisionModel
print("全部关键导入路径 OK")
```

3. 运行 `python check_env.py`。

**需要观察的现象**：九行版本号逐行打印；最后一行 `全部关键导入路径 OK`。特别注意 `from transformers.models.qwen3_vl_moe...` 这一行——它模拟的正是 `trainer.py` 最容易因版本不符而崩溃的导入。

**预期结果**：在你按清单安装的前提下，所有导入成功、版本与 README 一致。若 `flash_attn` 导入报 CUDA 相关错误，通常是 wheel 与你的 CUDA 版本不匹配，需到 flash-attn 的 release 页找对应 `cu` 版本的 wheel。各包实际版本打印值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么推理环境装 `transformers>=4.57.0` 即可，而训练框架要锁 `transformers==4.57.0.dev0` 这样精确的开发版？

**答案**：推理只依赖 transformers 的公共 API（`AutoModelForImageTextToText` 等有兼容性承诺的入口）；而 `trainer.py` 顶层 import 了 `transformers.models.qwen2_vl.modeling_qwen2_vl` 这类**内部模块路径**并直接替换其中的类方法，这些内部结构在不同版本间可能改名或搬家，所以必须锁定与开发时一致的版本。

**练习 2**：小明说「我只是想跑全量微调，不打算用 LoRA，所以 peft 可以不装」。这个说法对吗？

**答案**：基本正确但不必冒险。`from peft import ...` 位于 `if training_args.lora_enable:` 分支内（[train_qwen.py:163-164](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L163-L164)），是惰性导入，不开 LoRA 就不会执行。但装齐清单成本很低，且后续实验很可能用到 LoRA，建议照清单装全。

**练习 3**：请从 `tools/pack_data.py` 的 import 中找出两个「README 清单里没有、但跑这个脚本必须装」的包。

**答案**：`binpacking`（装箱算法，第 8 行）和 `torchcodec`（视频解码，第 7 行）。严格说 torchcodec 在清单里有（`torchcodec==0.2`），binpacking 则完全没有；此外 `check_image.py` 顶层的 `from datasets import load_dataset` 说明它还需要 `datasets` 包。

### 4.3 工具脚本：tools/ 三件套在流水线中的位置

#### 4.3.1 概念说明

`tools/` 下的三个工具都服务于同一件事：**让「你手里的原始数据」变成「框架能高效消化的训练数据」**。三者按需选用，都不是必需的：

- **`check_image.py`——体检医生**：训练数据最常见的死法是「标注里写了 `images/001.jpg`，但文件根本不存在」或「问题里写了两个 `<image>` 占位符却只给了一张图」。这个脚本逐条检查这两类问题，把好数据与坏数据分拣到两个文件。
- **`pack_data.py`——装箱打包员**：训练时每个样本长短不一（一张图 200 token，一段视频 3000 token），直接组 batch 会造成大量 padding 浪费。此脚本先用模型处理器**真实估算**每条样本的 token 数，再用装箱算法把短样本拼成接近定长的「桶」，配合训练侧 `--data_packing True` 使用。
- **`process_bbox.ipynb`——翻译官**：如果你有目标检测（grounding）数据，bbox 坐标格式（如 COCO 的 `[x1,y1,x2,y2]`）需要转换成 Qwen-VL 期望的文本格式，这个笔记本演示转换方法（本讲只定位，细节留给 u7-l3）。

#### 4.3.2 核心流程

三个工具在整个流水线中的位置（全部发生在训练启动**之前**）：

```text
你的原始标注 my.json
   │
   ├─① check_image.py ──→ my_valid.json（可直接用于训练）
   │                  └─→ my_problems.json（带 validation_problems 字段的坏样本）
   │
   ├─② pack_data.py ──→ my_count.json（每条样本的 num_tokens 缓存）
   │                 └─→ my_pack.json（样本组成的"桶"列表，配 --data_packing True）
   │
   └─③ process_bbox.ipynb ──→ grounding 标注的 value 文本换成 Qwen-VL bbox 格式
```

`pack_data.py` 的单条样本估算思路（这是它的精髓）：

```text
token 数 ≈ 对话文本 token 数（用 tokenizer.apply_chat_template 逐轮编码累加）
        + 图像视觉 token 数（用 image processor 真实跑一遍，
          取 image_grid_thw 乘积再除以 4）
```

其中「除以 4」对应 ViT patch 的 2×2 空间合并——这正是 u2-l1 讲过的「视觉 token = (H/2p)×(W/2p)」在训练侧的再次出现。而视频 token 数则由解码出实际帧后用 processor 跑 `video_grid_thw.prod() // 4` 得到。

#### 4.3.3 源码精读

**check_image.py 的三查逻辑**：

[qwen-vl-finetune/tools/check_image.py:6-21](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/check_image.py#L6-L21)：`validate_data` 是唯一入口函数，接收标注文件与媒体根目录，输出 `<原名>_valid.json` 与 `<原名>_problems.json` 两个分拣结果文件——校验通过的标注可以直接拿去训练。

[qwen-vl-finetune/tools/check_image.py:91-123](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/check_image.py#L91-L123)：核心校验——统计 human 轮里 `<image>` / `<video>` 占位符的**实际出现次数**，与 `image` / `video` 字段的**媒体文件个数**逐一比对；不相等记为 `token_mismatch`。这正是 README 第 211-214 行「一个 `<image>` 必须对应恰好一张图、占位符不得出现在答案里」这两条硬约束的机器检查版。第 108-110 行还单独检查 gpt 回答里是否混入了媒体占位符。

[qwen-vl-finetune/tools/check_image.py:166-169](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/check_image.py#L166-L169)：`__main__` 入口把路径硬编码为 `example.json` 与 `media`——想用自己的数据，要么改这两行，要么在 Python 里 `from check_image import validate_data` 调用。

**pack_data.py 的估算与装箱**：

[qwen-vl-finetune/tools/pack_data.py:59-67](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py#L59-L67)：`process_image` 打开图片后交给真实 processor 预处理，返回 `image_grid_thw.prod() // 4` 作为视觉 token 数——不是拿像素面积除以常数来估算，而是让 processor 实际算出 patch 网格，精度与训练时完全一致。

[qwen-vl-finetune/tools/pack_data.py:86-103](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py#L86-L103)：`calculate_tokens` 汇总一条样本的总 token：初始值 21（对话特殊控制 token 的经验开销），逐轮 `apply_chat_template` 累加文本 token，再按 image/video 字段累加视觉 token。

[qwen-vl-finetune/tools/pack_data.py:106-122](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py#L106-L122)：`pack_data` 调用 `binpacking.to_constant_volume` 做「装箱到定容」：每条样本是一个带 `num_tokens` 权重的物品，目标是装进容量 `pack_length`（主流程里取 4096）的桶；输出把每个桶还原成原始样本组成的列表，`num_tokens` 辅助字段被弹出。

[qwen-vl-finetune/tools/pack_data.py:153-167](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py#L153-L167)：主流程的提速设计——首次估算结果写入 `_count.json` 缓存，重跑脚本时直接读缓存跳过昂贵的逐条 processor 预处理；估算用线程池并发执行。

一个需要留意的细节：[tools/pack_data.py:34-43](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py#L34-L43) 中 `DataArguments` 的像素预算系数硬编码为 `28 * 28`（Qwen2/2.5-VL 时代的 merge 尺寸，承接 u2-l1 的 28/32 之辨），且第 136 行加载的是 `Qwen2VLImageProcessor`。给 Qwen3-VL 数据打包时，这些系数宜按 32×32（对应 patch=16 的 2×2 merge）自行调整，估算才更贴近训练实况。

#### 4.3.4 代码实践

**实践目标**：用 `check_image.py` 体检框架自带的 demo 数据，亲眼看到一次完整的「校验报告」。

**操作步骤**：

1. 确认已在 4.2.4 的环境中补装 `datasets`：`pip install datasets`。
2. 在 `qwen-vl-finetune/` 目录下以 Python 方式调用（避免改源码；示例代码）：

```bash
cd qwen-vl-finetune
python -c "
from tools.check_image import validate_data
# demo 标注里的 image 路径形如 'demo/images/10095.png'（相对 qwen-vl-finetune/），
# 因此媒体根目录传 '.'，拼接后才指向真实文件
validate_data('demo/single_images.json', '.')
"
```

   注意：`from tools.check_image import ...` 要求当前目录在 `sys.path` 中，`python -c` 从当前目录启动时默认满足。
3. 查看生成的两个文件：`ls demo/single_images_valid.json demo/single_images_problems.json`，并打开 `_problems.json`（若存在）。

**需要观察的现象**：终端打印 Validation Summary，包含总条数、有效条数与百分比、媒体类型分布（Image only / Video only / Mixed）、缺失媒体条数、占位符不匹配条数、gpt 含媒体占位符条数。

**预期结果**：demo 数据是官方维护的完好样例，`Valid entries` 应等于总数、`missing_media` 与 `token_mismatches` 均为 0，`_problems.json` 为空数组。若你想看到非零的坏样本统计，可以复制一份 JSON，把其中一条的 `image` 路径改成一个不存在的文件再跑一次。具体数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`pack_data.py` 估算图像 token 数时为什么用 `image_grid_thw.prod() // 4` 而不是直接用 `像素数 / (28*28)` 来除？

**答案**：`image_grid_thw` 是 processor 真实预处理后输出的 patch 网格三元组 (t, h, w)，已经包含了 smart_resize 对齐、像素预算裁剪等全部效应；`prod() // 4` 中的 `// 4` 对应 2×2 空间合并，patch 数除以 4 才是送入 LLM 的视觉 token 数。用像素面积硬除则会忽略对齐取整等细节，估算会有偏差。

**练习 2**：`check_image.py` 会把哪三类问题分拣进 `_problems.json`？

**答案**：① `missing_files`——标注引用的图片/视频文件在磁盘上不存在（第 74-89 行检查）；② `token_mismatch`——human 轮中 `<image>`/`<video>` 占位符数量与 image/video 字段的文件数不相等（第 112-123 行检查）；③ `gpt_media_token`——assistant 回答文本里混入了 `<image>` 或 `<video>` 占位符（第 125-130 行检查）。

**练习 3**：为什么 `pack_data.py` 要把首次估算结果存成 `_count.json`？

**答案**：因为 token 估算必须用 processor 对每张图/每段视频真实跑一遍预处理（`process_image`/`process_video`），这是 CPU 密集的慢操作。装箱参数（如 `pack_length`）往往要反复调整，把与装箱无关的估算结果缓存下来，重跑时就能跳过最贵的一步（[pack_data.py:153-167](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py#L153-L167) 的缓存分支）。

### 4.4 模型支持范围与 train_qwen.py 训练主流程

#### 4.4.1 概念说明

`train_qwen.py` 是整个框架唯一的训练入口，206 行代码串起一次完整微调。它同时支持**四类模型代际**：Qwen2-VL、Qwen2.5-VL、Qwen3-VL（Dense）与 Qwen3-VL-MoE。你不需要用任何参数声明用的是哪一代——脚本会**根据模型路径名里的字符串自动判定**，再决定加载 transformers 里的哪个类。

另一件本讲必须建立认知的事：多模态模型由三大部件组成——**视觉编码器（`model.visual`）、视觉-语言投影（`model.visual.merger`）、语言模型（`model.language_model` + `lm_head`）**。框架用三个开关（`tune_mm_vision` / `tune_mm_mlp` / `tune_mm_llm`）分别控制这三个部件是否参与训练，这就是「模块级冻结策略」。README 还给了一条实践经验：图像视频混合训练时 `tune_mm_vision` 应设为 False（[README.md:310](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L310)）。

#### 4.4.2 核心流程

`train()` 函数（[train_qwen.py:92-202](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L92-L202)）的主流程共 11 步：

```text
① 解析参数    HfArgumentParser 把命令行装进三组 dataclass
                 （ModelArguments / DataArguments / TrainingArguments）
② 选模型类    按模型路径名判定代际 → from_pretrained 加载
                  同时给 data_args.model_type 赋值（qwen2vl/qwen2.5vl/qwen3vl）
③ 加载 processor  AutoProcessor.from_pretrained（图像/视频/文本三合一，见 u3-l1）
④ 打补丁      若 data_flatten 或 data_packing → replace_qwen2_vl_attention_class()
                  替换四代模型的注意力实现（为拼接序列做准备，u5-l6 详讲）
⑤ 训练姿态    use_cache=False（训练不需要 KV cache）；
                  若开梯度检查点 → enable_input_require_grads
⑥ 加载 tokenizer  padding_side="right"、use_fast=False（注意与推理的左填充相反，u3-l1）
⑦ 冻结策略    二选一：
                  LoRA 分支：全冻结后 get_peft_model 插入低秩适配器
                  全量分支：set_model 按三开关控制 visual / merger / llm
⑧ 构建数据    make_supervised_data_module(processor, data_args) → train_dataset + collator
⑨ 建 Trainer   model + tokenizer + args + data_module
⑩ 训练        trainer.train()；若 output_dir 已有 checkpoint-* 则自动断点续训
⑪ 保存        use_cache 恢复 True → save_state → safe_save_model_for_hf_trainer
                  → processor.save_pretrained
```

四分支模型判定逻辑（对 `model_name_or_path` 转小写后判断）：

| 判定条件 | 加载的类 | `data_args.model_type` | 对应典型模型 |
|---|---|---|---|
| 含 `qwen3` 且路径**末段目录名**含字母 `a` | `Qwen3VLMoeForConditionalGeneration` | `qwen3vl` | Qwen3-VL-30B-**A**3B-Instruct |
| 含 `qwen3` | `Qwen3VLForConditionalGeneration` | `qwen3vl` | Qwen3-VL-2B/4B/8B-Instruct |
| 含 `qwen2.5` | `Qwen2_5_VLForConditionalGeneration` | `qwen2.5vl` | Qwen2.5-VL-3B/7B/32B |
| 其余（兜底） | `Qwen2VLForConditionalGeneration` | `qwen2vl` | Qwen2-VL 系列 |

MoE 的判定依据很「朴素」：MoE 规格的命名是 `30B-A3B`——总参数 30B、每 token 激活（**A**ctive）3B（承接 u1-l1），字母 `A` 落进了目录名。这带来一个值得警惕的副作用：任何末段含 `a` 的 Qwen3 路径（例如本地目录起名 `qwen3vl-base`）都会被误判成 MoE，练习 3 会让你推演这个坑。

`model_type` 这个不起眼的赋值是后面多讲的伏笔：它一路传进 `data_processor.py`，决定样本预处理调用 `rope2d.py` 里的哪一代位置编码函数（u5-l3、u5-l4 展开）。

#### 4.4.3 源码精读

**四分支模型加载**：

[qwen-vl-finetune/qwenvl/train/train_qwen.py:103-134](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L103-L134)：完整的四分支 if-elif-else。每个分支做三件事——选定模型类、`from_pretrained`（统一传 `attn_implementation="flash_attention_2"`、按 `bf16` 决定 dtype、指定 cache_dir）、给 `data_args.model_type` 赋值。第一个分支的条件 `"a" in Path(...).name.lower()` 就是 MoE 判定。

[qwen-vl-finetune/qwenvl/train/train_qwen.py:136-139](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L136-L139)：加载完成立刻打印实际类名（`model.__class__.__name__`）——排查「MoE 误判」这类问题时，这行日志是第一现场；随后加载 processor。

**三开关冻结策略**：

[qwen-vl-finetune/qwenvl/train/train_qwen.py:67-89](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L67-L89)：`set_model` 是全量微调的冻结策略实现，三个开关与三大部件一一对应——`tune_mm_vision` 管 `model.visual`（整个视觉编码器），`tune_mm_mlp` 管 `model.visual.merger`（投影/合并层），`tune_mm_llm` 管 `model.language_model` 加 `lm_head`。每个开关只做一件事：把对应部件全部参数的 `requires_grad` 置 True 或 False。

**LoRA 与全量微调的分岔**：

[qwen-vl-finetune/qwenvl/train/train_qwen.py:163-184](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L163-L184)：两条互斥路径——LoRA 分支先把全模型 `requires_grad = False`，再用 `LoraConfig`（目标层为注意力投影 `q/k/v/o_proj`）包一层；全量分支调用 `set_model`，随后在 0 号进程打印各部件可训练参数量（`print_trainable_parameters` 系列函数来自 `trainer.py` 的 monkey patch，见 [trainer.py:497-510](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L497-L510)）。

**训练循环与保存**：

[qwen-vl-finetune/qwenvl/train/train_qwen.py:186-195](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L186-L195)：`make_supervised_data_module` 产出数据模块塞进 `Trainer`；训练前用 `glob("checkpoint-*")` 探测 `output_dir`——只要存在历史 checkpoint 就自动 `resume_from_checkpoint=True`，这是「训崩了原地重启即可续训」的机制来源。

[qwen-vl-finetune/qwenvl/train/train_qwen.py:52-64](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L52-L64)：`safe_save_model_for_hf_trainer` 的双路径保存——DeepSpeed 模式下走 `trainer.save_model`（由 DeepSpeed 收集分片状态）；普通模式下手工把 state_dict 逐项搬到 CPU 再落盘，避免 GPU 显存峰值翻倍。

[qwen-vl-finetune/qwenvl/train/train_qwen.py:196-202](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L196-L202)：保存三连——先把 `use_cache` 恢复为 True（保证导出的权重默认可用于推理）、存 trainer 状态、存模型，最后把 processor 也存进输出目录。**processor 跟着权重一起保存**意味着微调产物是一个「自成一体」的模型目录，直接指向推理。

**启动脚本与模型规格的对应关系**（印证 MoE 的 ZeRO-3 限制）：

[qwen-vl-finetune/scripts/sft_qwen3_4b.sh:9-27](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_qwen3_4b.sh#L9-L27)：Qwen3-VL-4B 模板，DeepSpeed 用 `zero3.json`。结合各脚本可整理出对照表：

| 脚本 | 模型 | DeepSpeed 配置 |
|---|---|---|
| `sft.sh` | Qwen2.5-VL-3B-Instruct | zero3.json |
| `sft_7b.sh` | Qwen2.5-VL-7B-Instruct | zero3.json |
| `sft_32b.sh` | Qwen2.5-VL-32B-Instruct | zero3.json |
| `sft_qwen3_4b.sh` | Qwen3-VL-4B-Instruct | zero3.json |
| `sft_30a3b.sh` | Qwen3-VL-30B-A3B-Instruct（MoE） | **zero2.json** |
| `sft_30a3b_lora.sh` | Qwen3-VL-30B-A3B-Instruct（MoE）+ LoRA | **zero2.json** |

注意规律：**唯独两个 MoE 脚本用的是 zero2**。这与 README 的明确警告一致——[README.md:317](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L317)：Qwen3-VL MoE 模型不支持 DeepSpeed ZeRO-3，且 HF 官方实现暂未包含 load balancing loss。README 第 315 行还给了显存参考：32B 模型训练需要 8 张 80G GPU。

最后看整个框架的最末端一行：

[qwen-vl-finetune/qwenvl/train/train_qwen.py:205-206](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L205-L206)：`__main__` 入口调用 `train(attn_implementation="flash_attention_2")`——注意力实现在这里被硬编码为 flash_attention_2（所以 4.2 的 flash-attn 是硬依赖），不随命令行参数改变。

#### 4.4.4 代码实践

**实践目标**：把 4.1.3、4.4.3 读到的 import 关系整理成一张 `qwenvl` 包的**模块依赖图**，并用 Python 探针验证 MoE 判定逻辑。

**操作步骤**：

1. 基于本讲读过的源码，手绘（或用任意画图工具）如下依赖图，箭头表示「import 了」：

```text
torchrun 启动
   │
   ▼
train_qwen.py（入口）
   ├──→ trainer（裸模块导入，靠脚本目录入 sys.path）
   │      └──→ transformers 内部模型模块 ×4 + flash_attn
   │      └──→ （文件尾）monkey patch：Trainer.create_optimizer、
   │             四代 Vision/Model 类的 print_trainable_parameters
   ├──→ qwenvl.data.data_processor.make_supervised_data_module
   │      ├──→ qwenvl.data.__init__ 的 data_list（注册表）
   │      └──→ qwenvl.data.rope2d 的 get_rope_index_2/25/3
   ├──→ qwenvl.train.argument（三组 dataclass）
   └──→ transformers（AutoProcessor、Trainer、四类模型类）

scripts/*.sh ──（torchrun + 命令行参数）──→ train_qwen.py
tools/*.py  ──（产出的 json）──→ data/__init__.py 注册的 annotation_path
```

2. 用探针验证 MoE 判定条件（示例代码，不需要 GPU）：

```python
# check_moe_judge.py —— 复现 train_qwen.py 的代际判定逻辑（示例代码）
from pathlib import Path

def judge(model_name_or_path: str) -> str:
    p = model_name_or_path.lower()
    last = Path(model_name_or_path.rstrip("/")).name.lower()
    if "qwen3" in p and "a" in last:
        return "Qwen3VLMoeForConditionalGeneration"
    if "qwen3" in p:
        return "Qwen3VLForConditionalGeneration"
    if "qwen2.5" in p:
        return "Qwen2_5_VLForConditionalGeneration"
    return "Qwen2VLForConditionalGeneration"

for path in [
    "Qwen/Qwen3-VL-2B-Instruct",       # 期望: Dense Qwen3-VL
    "Qwen/Qwen3-VL-30B-A3B-Instruct",  # 期望: MoE
    "Qwen/Qwen2.5-VL-7B-Instruct",     # 期望: Qwen2.5-VL
    "/models/qwen3vl-base",            # 陷阱: 名字含 a，会被判成 MoE!
]:
    print(f"{path:42s} -> {judge(path)}")
```

3. 运行 `python check_moe_judge.py`，对照 4.4.2 的表格检查每行输出。

**需要观察的现象**：前三个路径的判定与预期一致；第四个 `/models/qwen3vl-base` 明明是 Dense 模型，却被判成 `Qwen3VLMoeForConditionalGeneration`。

**预期结果**：复现出 MoE 判定的字符串启发式缺陷——给本地模型目录起名时必须避开「末段含字母 a」（比如不要叫 `my-qwen3vl-finetune-a`），或干脆沿用官方命名 `Qwen3-VL-8B-Instruct`（不含 a）。真实训练日志里，[train_qwen.py:136](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L136) 打印的类名可用来终审判定是否正确。输出待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`--tune_mm_llm True --tune_mm_mlp True --tune_mm_vision False` 这一组合（正是 `sft_qwen3_4b.sh` 的默认设置）训练的是模型的哪些部分？

**答案**：语言模型全部参数（含 `lm_head`）和视觉-语言投影层 `model.visual.merger` 参与训练，视觉编码器 `model.visual` 的其余部分被冻结。对应 `set_model` 中三个 if 块的 `requires_grad` 赋值（[train_qwen.py:67-89](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L67-L89)）。这是最常用的组合：既让模型学会「按你的格式说话」，又通过训练 merger 适配视觉特征，同时省下整个 ViT 的训练开销。

**练习 2**：训练结束后，`output_dir` 里除了模型权重还有什么？为什么推理时直接把这个目录当 `model_name_or_path` 用就行？

**答案**：还有 processor 的完整保存（[train_qwen.py:202](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L202) 的 `processor.save_pretrained`）以及 trainer 状态（`save_state`）；且保存前 `use_cache` 已恢复 True（第 198 行）。权重 + processor 齐备、config 处于可推理状态，所以它是一个自洽的模型目录，可直接喂给 `AutoModelForImageTextToText.from_pretrained` 或 vLLM。

**练习 3**：假设你把一个 Qwen3-VL-8B（Dense）模型下载到本地并重命名目录为 `/data/qwen3vl-8b-a`，启动训练会发生什么？如何避免？

**答案**：路径含 `qwen3` 且末段 `qwen3vl-8b-a` 含字母 `a`，第一个分支命中，模型会被当成 MoE 用 `Qwen3VLMoeForConditionalGeneration` 加载——权重结构与类不匹配，轻则加载报错，重则结构错乱。避免方法：目录名不含 `a`（如 `/data/qwen3vl-8b`），并养成看启动日志 `the initlized model is ... the class is ...` 的习惯（[train_qwen.py:136](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L136)）来终审类名。

**练习 4**：`scripts/` 下为什么唯独两个 30B-A3B 脚本用 `zero2.json` 而不是 `zero3.json`？

**答案**：30B-A3B 是 MoE 模型，而 README 第 317 行明确说明 Qwen3-VL MoE 不支持 DeepSpeed ZeRO-3（另有「HF 官方实现暂无 load balancing loss」的限制说明），所以 MoE 只能退到 ZeRO-2。这也是 README 强调按模型规格选用脚本模板的原因之一。

## 5. 综合实践

把本讲四个模块串成一个任务：**从零搭好训练环境，并产出一份《环境与架构核查报告》**。

任务背景：假设你要在单机 8 卡 A100-80G 上对 `Qwen/Qwen3-VL-4B-Instruct` 做微调。完成以下步骤：

1. **搭环境（模块 4.2）**：按 README Requirements 建独立 conda 环境，运行 4.2.4 的 `check_env.py`，把九个包的实际版本截图/粘贴进报告。
2. **画地图（模块 4.1、4.4）**：运行 4.1.4 的 `probe.py` 确认包结构，再把 4.4.4 的模块依赖图抄录进报告，并在图上标注：哪条边依赖「脚本目录自动入 sys.path」、哪个模块会被 monkey patch。
3. **数据体检（模块 4.3）**：对 `demo/single_images.json` 跑 4.3.4 的校验，把 Validation Summary 全文粘进报告；再故意制造一条坏样本（改一个不存在的图片路径、删掉一个 `<image>` 占位符各试一次），记录两次的 `missing_media` / `token_mismatches` 变化。
4. **脚本会诊（模块 4.4）**：打开 `scripts/sft_qwen3_4b.sh`，逐行回答：它选了哪档 DeepSpeed、传给 `train_qwen.py` 的参数里哪些属于 ModelArguments / DataArguments / TrainingArguments（对照 [argument.py:6-45](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/argument.py#L6-L45)，具体参数详解是 u5-l2 的任务，本讲只需归类）。
5. **（可选，需 GPU）冒烟训练**：把 `sft_qwen3_4b.sh` 中的 `datasets` 换成 4.3 校验产出的 `demo/single_images_valid.json` 对应的注册数据集（注册方法预习 [README.md:157-204](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L157-L204) 与 [data/__init__.py:29-56](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/__init__.py#L29-L56)，正式讲解在 u4-l3），`num_train_epochs` 改为 0.01 跑一次，确认日志出现 `the initlized model is ...` 与 loss 数值。⚠️ 提醒：README「Usage」小节里那份带行内注释的 bash 示例中，`\` 之后跟随注释会导致续行中断，不能直接复制运行，请以 `scripts/*.sh` 的干净写法为准。训练是否成功待本地验证。

报告验收标准：能只凭这份报告回答出「框架分几个目录、各放什么」「九个依赖各自为什么存在」「四类模型怎么自动判定」「三开关各管哪个部件」四个问题。

## 6. 本讲小结

- `qwen-vl-finetune/` 是「源码即工程」的训练项目：核心包 `qwenvl` 分 `train/`（怎么学：入口、参数、Trainer 补丁）与 `data/`（学什么：注册表、样本处理、位置编码）两个子包，外围由 `scripts/`（启动模板 + DeepSpeed 配置）、`tools/`（数据校验/打包/bbox 转换）、`demo/`（示例数据）构成；必须 `cd qwen-vl-finetune` 后以 `torchrun qwenvl/train/train_qwen.py` 方式启动。
- 依赖必须照抄 README 的九项锁定版本，因为 `trainer.py` 顶层 import 了 transformers 内部模块路径并替换其类方法；`flash_attn` 是硬依赖（入口硬编码 `attn_implementation="flash_attention_2"`）；tools 脚本还隐含需要 `binpacking`、`datasets`。
- `tools/` 三件套都工作在训练启动之前：`check_image.py` 查媒体存在性与 `<image>/<video>` 占位符一致性并分拣好坏样本；`pack_data.py` 用 processor 真实估算 token 数（`grid_thw.prod()//4` 对应 2×2 merge）再做定容装箱；`process_bbox.ipynb` 做 grounding 标注格式转换。
- `train_qwen.py` 按模型路径名四分支自动选择 Qwen2-VL / Qwen2.5-VL / Qwen3-VL / Qwen3-VL-MoE 类并设置 `model_type`；MoE 判定依赖「末段目录名含字母 a」的字符串启发式，本地目录命名要避开。
- 全量微调的冻结策略由三开关控制三大部件（`tune_mm_vision`→visual、`tune_mm_mlp`→merger、`tune_mm_llm`→language_model+lm_head）；LoRA 分支则全冻结后经 peft 注入 q/k/v/o_proj 适配器。
- 训练主流程 11 步：参数解析→模型加载→processor→注意力补丁→训练姿态→tokenizer→冻结策略→数据模块→Trainer→自动断点续训→保存（权重 + 状态 + processor，产物自洽可直接推理）；MoE 模型不支持 ZeRO-3，故 30B-A3B 脚本一律用 zero2。

## 7. 下一步学习建议

本讲只搭好了「骨架认知」，下一篇进入血肉——**训练数据**：

- **下一讲 u4-l2「训练数据格式精读」**：逐字段精读 `conversations` 的 human/gpt 结构、`<image>`/`<video>` 占位符匹配规则、grounding 与 packed 样本格式，并动手制作自己的第一份训练 JSON（素材就是本讲 4.3.4 用过的 `demo/single_images.json`）。随后 u4-l3 讲解如何把你的 JSON 注册进 `data_dict`。
- 若你想先弄清「参数到底有哪些」，可跳读 [argument.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/argument.py)（仅 45 行，三组 dataclass），正式的参数逐条对照在 u5-l2。
- 建议顺手重读本讲 4.4.2 的 11 步主流程直到能默画——单元五的全部讲义（标签构造、rope2d、collator、FlashAttention、LoRA、DeepSpeed）本质上都是在这 11 步中的某一步上向下钻探。
