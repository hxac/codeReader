# 参数体系：argument.py 三组 dataclass 与脚本对照

## 1. 本讲目标

上一讲（u5-l1）我们走通了 `train_qwen.py` 的训练主流程，但当时把命令行参数当成了黑盒。本讲把黑盒打开：`argument.py` 里只有 46 行代码，却定义了整个微调框架**所有**可配置项的入口。学完本讲，你应该能够：

1. 说出三组 dataclass（`ModelArguments` / `DataArguments` / `TrainingArguments`）各自的字段、默认值和消费位置。
2. 理解 DataArguments 中 8 个视觉预算参数（像素/帧数/采样）如何经 `update_processor_pixels` 一路注入 processor，并最终决定视觉 token 数。
3. 看懂 LoRA 四参数与分层学习率两参数的生效条件——包括几个「参数传了却不生效」的隐蔽陷阱。
4. 拿着 `scripts/sft_qwen3_4b.sh`，把其中每一个 `--` 参数准确归到某一组 dataclass（或 torchrun 层），并能独立改写出适配自己单卡环境的启动脚本。

## 2. 前置知识

### 2.1 dataclass 与 field

Python 的 `@dataclass` 装饰器会根据类属性自动生成 `__init__` 等方法。带默认值的字段用 `field(default=...)` 声明。本讲的核心认知是：**dataclass 的字段名就是命令行参数名，字段默认值就是不传参数时的取值**。

### 2.2 HfArgumentParser：dataclass 到 argparse 的自动桥梁

transformers 提供的 `HfArgumentParser` 接收若干 dataclass，自动为每个字段生成一个同名命令行参数（字段名中的下划线对应命令行的下划线，例如 `model_name_or_path` 对应 `--model_name_or_path`）。`train()` 中三行就完成解析：

- [qwen-vl-finetune/qwenvl/train/train_qwen.py:95-98](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L95-L98) —— 把三组 dataclass 注册进解析器，`parse_args_into_dataclasses()` 按命令行顺序返回三个实例 `model_args, data_args, training_args`。

布尔字段由 parser 内部的 `string_to_bool` 处理，所以脚本里两种写法都合法：`--bf16`（省略值，等价 True）与 `--tune_mm_vision False`（显式关）。

### 2.3 继承 transformers.TrainingArguments 意味着什么

本仓库的 `TrainingArguments` **继承自** `transformers.TrainingArguments`。这意味着 `--learning_rate`、`--bf16`、`--deepspeed`、`--per_device_train_batch_size` 等 HF 原生训练参数全部自动可用，仓库只需要「新增」多模态特有的字段。所以脚本里大量参数在 `argument.py` 中**找不到定义是正常的**——它们定义在 transformers 库里。

### 2.4 像素预算的两种口径（承接 u2-l2 / u3-l1）

- Qwen2/2.5-VL：patch=14，2×2 merge 后对齐因子 28，一个视觉 token 占 \(28 \times 28 = 784\) 像素。
- Qwen3-VL：patch=16，对齐因子 32，一个视觉 token 占 \(32 \times 32 = 1024\) 像素。

`argument.py` 的默认值都是按 28 口径写的（历史遗留），脚本里 `50176 = 28 \times 28 \times 64`。给 Qwen3-VL 配参数时换算 token 数要除以 1024，而不是 784。

### 2.5 参数注入链全景

```
命令行 / shell 脚本
   │  torchrun 启动 train_qwen.py
   ▼
HfArgumentParser ──► model_args / data_args / training_args（三个 dataclass 实例）
   │                     │                      │
   │                     │                      ├─► from_pretrained(dtype=bf16)、tokenizer(model_max_length)
   │                     │                      ├─► LoRA 分支（lora_* 四参数）
   │                     │                      └─► Trainer.create_optimizer（mm_projector_lr / vision_tower_lr）
   │                     ├─► set_model（tune_mm_* 三开关）
   │                     └─► update_processor_pixels（像素/帧数/fps 八参数 → processor）
   ▼
Trainer 训练循环
```

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [qwen-vl-finetune/qwenvl/train/argument.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/argument.py#L1-L46) | 本讲主角：三组 dataclass 的全部定义，仅 46 行 |
| [qwen-vl-finetune/qwenvl/train/train_qwen.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L92-L98) | 参数的解析与主要消费点（模型加载、LoRA、tokenizer） |
| [qwen-vl-finetune/qwenvl/data/data_processor.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L44-L137) | `update_processor_pixels`：把 DataArguments 的视觉预算注入 processor |
| [qwen-vl-finetune/qwenvl/train/trainer.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L316-L491) | `create_optimizer`：分层学习率两参数的真正消费点 |
| [qwen-vl-finetune/scripts/sft_qwen3_4b.sh](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_qwen3_4b.sh#L1-L66) | 对照对象一：Dense 4B 全量微调脚本 |
| [qwen-vl-finetune/scripts/sft_30a3b_lora.sh](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_30a3b_lora.sh#L1-L69) | 对照对象二：MoE LoRA 脚本（展示了 `--lora_enable` 的用法） |
| [qwen-vl-finetune/tools/pack_data.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py#L34-L43) | 佐证：工具链自带一份同名 `DataArguments`，与训练侧的并非同一个类 |

## 4. 核心概念与源码讲解

### 4.1 ModelArguments：模型身份与可训练范围

#### 4.1.1 概念说明

`ModelArguments` 回答两个问题：**训练哪个模型**、**放开哪部分参数**。它只有 4 个字段，是三组里最小的，但它牵动的逻辑最重——`model_name_or_path` 不仅决定加载哪个权重，还是 u5-l1 讲过的「路径字符串嗅探式代际判定」的唯一依据；三个 `tune_mm_*` 开关则通过 `set_model` 决定梯度能流到哪些模块。

#### 4.1.2 核心流程

```
model_name_or_path ──► 代际嗅探（含 "qwen3"？末段含 "a"？）
        │                   │
        │                   ├─ "qwen3"+含"a" ─► Qwen3VLMoeForConditionalGeneration
        │                   ├─ "qwen3"       ─► Qwen3VLForConditionalGeneration
        │                   ├─ "qwen2.5"     ─► Qwen2_5_VLForConditionalGeneration
        │                   └─ 其余           ─► Qwen2VLForConditionalGeneration
        │
        └─ 同时用于加载 processor 与 tokenizer

tune_mm_vision ─┐
tune_mm_mlp   ──┼─► set_model()：按模块 requires_grad 逐个开关
tune_mm_llm   ─┘     （merger 是 visual 的子模块，最终状态由 tune_mm_mlp 决定）
```

注意互斥关系：若 `lora_enable=True`，训练走 LoRA 分支，`set_model` 根本不会被调用——此时 `tune_mm_*` 传什么都无效（详见 4.3）。

#### 4.1.3 源码精读

完整定义只有 6 行：

- [qwen-vl-finetune/qwenvl/train/argument.py:6-11](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/argument.py#L6-L11) —— `model_name_or_path` 默认 `Qwen/Qwen2.5-VL-3B-Instruct`；三个 `tune_mm_*` 布尔开关默认全 False（即默认什么都不训练，必须显式指定要放开什么）。

消费点一：代际判定。`train()` 用路径字符串做两次匹配：

- [qwen-vl-finetune/qwenvl/train/train_qwen.py:103-112](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L103-L112) —— 先看路径是否含 `qwen3`，再看**末段目录名**是否含字母 `a` 来猜 MoE（如 `Qwen3-VL-30B-A3B-Instruct`）。`Qwen/Qwen3-VL-2B-Instruct` 的末段不含 `a`，会正确落到 Dense 分支；但若你的本地目录名恰好带 `a`（例如 `mybackup`），就会被误判成 MoE。这是 u5-l1 强调过的坑，根源就在这个字段。

消费点二：可训练范围。

- [qwen-vl-finetune/qwenvl/train/train_qwen.py:67-89](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L67-L89) —— `set_model` 先按 `tune_mm_vision` 处理整个 `model.visual`，再按 `tune_mm_mlp` 处理 `model.visual.merger`。由于 merger 是 visual 的子模块且后处理覆盖先处理，`vision=False + mlp=True` 时 merger 依然可训练；`tune_mm_llm` 则同时控制 `language_model` 与 `lm_head`。

#### 4.1.4 代码实践

**实践目标**：验证 `model_name_or_path` 的默认值与代际嗅探的输入来源。

**操作步骤**（不加载模型，纯参数层面，安装 transformers 后即可本地验证）：

```python
# 示例代码：保存为 qwen-vl-finetune 目录外的 peek_args.py，用 python -I peek_args.py 运行
import sys
sys.path.append("qwen-vl-finetune")
from transformers import HfArgumentParser
from qwenvl.train.argument import ModelArguments, DataArguments, TrainingArguments

parser = HfArgumentParser((ModelArguments, DataArguments, TrainingArguments))
m, d, t = parser.parse_args_into_dataclasses([
    "--model_name_or_path", "Qwen/Qwen3-VL-2B-Instruct",
    "--output_dir", "/tmp/out",          # HF 原生必填字段
    "--tune_mm_mlp", "True",
])
print("model:", m.model_name_or_path)
print("tune:", m.tune_mm_llm, m.tune_mm_mlp, m.tune_mm_vision)  # False True False
```

**需要观察的现象**：不传 `tune_mm_llm/tune_mm_vision` 时输出为 `False True False`，证明默认值生效；`training_args.output_dir` 若缺失会直接报错，说明 HF 原生字段同样参与解析。

**预期结果**：三个 dataclass 实例被正确构造，`m.tune_mm_mlp == True`。（待本地验证：依赖你环境中 transformers 的版本。）

#### 4.1.5 小练习与答案

**练习 1**：把模型路径设为本地目录 `/data/models/qwen3vl_2b_backup`，代际判定会走向哪个分支？
**答案**：末段 `qwen3vl_2b_backup` 小写后含字母 `a`（`backup` 里的 a），且路径含 `qwen3`，会被误判为 MoE，加载 `Qwen3VLMoeForConditionalGeneration` 并报权重不匹配。本地部署请避免目录名中出现字母 `a`。

**练习 2**：为什么脚本普遍写 `--tune_mm_vision False --tune_mm_mlp True --tune_mm_llm True`？
**答案**：视觉编码器参数量大、预训练已充分，冻结它（vision=False）可省显存并降低过拟合风险；merger（mlp=True）作为 ViT 与 LLM 的衔接层和 LLM（llm=True）一起微调，是多模态 SFT 的常见组合。

**练习 3**：`tune_mm_vision=True` 但 `tune_mm_mlp=False` 时，merger 还会被训练吗？
**答案**：不会。`set_model` 先按 `tune_mm_vision` 解冻整个 `model.visual`（此时 merger 也被解冻），但 merger 分支在后执行，`tune_mm_mlp=False` 会把 `visual.merger` 重新冻结——merger 的最终状态**只由 `tune_mm_mlp` 决定**。所以此时被训练的是 visual 中除 merger 外的其余部分。

---

### 4.2 DataArguments：数据组织与「视觉预算八参数」联动

#### 4.2.1 概念说明

`DataArguments` 字段最多（11 个），按职责分两族：

- **数据组织族（3 个）**：`dataset_use`（用哪些数据集，逗号分隔，支持 u4-l3 讲过的 `%X` 采样语法）、`data_flatten` / `data_packing`（两种样本拼接策略，u5-l5 详讲）。
- **视觉预算族（8 个）**：图像 2 个 + 视频 5 个 + 采样间隔 1 个。它们是本模块的主角——**命令行是训练侧控制视觉 token 数的唯一入口**，而控制手段就是像素预算与帧数。

#### 4.2.2 核心流程

8 个视觉预算参数的注入路径：

```
DataArguments（8 个视觉预算参数）
├── 图像族（2）
│   ├── min_pixels ──┐
│   └── max_pixels ──┴─► update_processor_pixels ─► image_processor
│                          写入 min_pixels / max_pixels 与 size 字典
└── 视频族（6）
    ├── video_min_pixels ─┐
    ├── video_max_pixels ─┤
    ├── video_min_frames ─┼─► update_processor_pixels ─► video_processor
    ├── video_max_frames ─┤    写入 min/max_pixels、min/max_frames、fps 与 size 字典
    ├── video_fps ────────┘
    └── base_interval ──► 训练主链路未消费（见 4.2.3 末尾）
```

注入之后，预算如何变成 token 数（承接 u2-l2 / u3-l1 的因果链「预算 → 缩放后尺寸 → 视觉 token 数 → 序列长度」）：

- 图像 token 数：\[ N_{image} = \frac{H'}{32} \times \frac{W'}{32} \]，其中 \(H' \times W'\) 被 `min_pixels / max_pixels` 夹紧后再对齐到 32 的倍数。
- 视频 token 数（时间维 2× 压缩）：
  \[ N_{video} = \frac{T}{2} \times \frac{H' \times W'}{32 \times 32} \]
  其中帧数 \(T\) 由采样决定，单帧（以及总）分辨率由 `video_*_pixels` 夹紧。
- 帧数联动：processor 采样帧数由 `video_fps × 时长` 估算，再被 `[video_min_frames, video_max_frames]` 夹紧（processor 内部还会做偶数对齐，呼应 u2-l3 的 FRAME_FACTOR=2）。**所以单独调大 `video_fps` 不一定增加帧数——一旦触到 `video_max_frames` 上限就封顶了**。
- `video_max_pixels` 注入的是 video_processor 的总像素语义（u3-l1 已建立：video 侧 size 约束的是全部帧像素总和），因此「帧数 × 单帧分辨率」与它是一对权衡：想加帧就得降单帧分辨率，否则总预算会强行缩小每帧。

#### 4.2.3 源码精读

完整定义：

- [qwen-vl-finetune/qwenvl/train/argument.py:13-25](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/argument.py#L13-L25) —— `DataArguments` 的 11 个字段。注意所有像素默认值都是 28 的倍数（`28*28*576`、`28*28*16`、`1024*28*28`、`256*28*28`），是 Qwen2.5-VL 口径的历史遗留；`video_max_frames` 默认 8、`video_min_frames` 默认 4、`video_fps` 默认 2。

图像族注入点：

- [qwen-vl-finetune/qwenvl/data/data_processor.py:56-70](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L56-L70) —— 把 `data_args.min_pixels/max_pixels` 同时写入 `image_processor.min_pixels/max_pixels` 与 `size` 字典的 `shortest_edge/longest_edge`（两处都写是为了兼容不同版本 processor 读取入口）。

视频族注入点：

- [qwen-vl-finetune/qwenvl/data/data_processor.py:92-118](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L92-L118) —— 依次写入 `vp.min_pixels = video_min_pixels`、`vp.max_pixels = video_max_pixels`、`vp.min_frames/max_frames = video_min/max_frames`、`vp.fps = video_fps`，以及 `vp.size` 字典。函数开头与结尾的 `rank0_print` 会把修改前后的值全部打印出来，是训练日志里核对预算是否生效的第一现场。

三个值得注意的「参数考古」事实：

1. **`base_interval` 在训练主链路未被消费**。全仓库检索显示它只在 `argument.py:18` 定义；工具脚本 `pack_data.py` 用的其实是**自带的另一份同名类**（[tools/pack_data.py:34-43](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py#L34-L43)，其中 `base_interval=4`，在 [tools/pack_data.py:76-78](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/pack_data.py#L76-L78) 以「时长 ÷ interval = 采样帧数」的方式使用）。也就是说：训练侧命令行传 `--base_interval` 不会影响训练本身，而 pack_data 的长度预估与训练采样要一致只能靠人工对齐。
2. **幽灵参数**：数据集初始化处用 `getattr` 读取 `video_max_total_pixels / video_min_total_pixels`（[qwen-vl-finetune/qwenvl/data/data_processor.py:253-258](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L253-L258)），但 `DataArguments` 根本没定义这两个字段，`getattr` 永远返回 fallback 默认值（`1664*28*28` / `256*28*28`），且读进来之后也没有任何后续代码使用。想从命令行控制「视频总像素」目前没有入口。
3. **`model_type` 不在 dataclass 里**：它由 `train()` 在代际判定后动态挂到 `data_args` 上（[train_qwen.py:110](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L110) 等），不能也不需要从命令行传。

`data_flatten / data_packing` 的消费点：

- [qwen-vl-finetune/qwenvl/train/train_qwen.py:141-142](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L141-L142) —— 任一为 True 就触发 varlen 注意力替换（u5-l6 主题）；两者还共同决定 collator 选型（[data_processor.py:681-689](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L681-L689)），`data_packing` 另外让数据集整桶取样。

#### 4.2.4 代码实践

**实践目标**：亲手算一遍脚本默认像素预算在 Qwen3-VL 口径下的 token 含义，并核对注入是否生效。

**操作步骤**：

1. 换算：脚本 `--max_pixels 50176`。因为 \(50176 = 28 \times 28 \times 64\)，在 Qwen3-VL 下单 token 占 \(32 \times 32 = 1024\) 像素，所以上限约 \(50176 / 1024 = 49\) 个视觉 token；`--min_pixels 784` 换算约 0.77 个 token，实际会被 smart_resize 的「至少对齐到 32×32」兜底成 1 个 token。
2. 观察注入日志：以任意小配置启动训练（或复用 u5-l1 的冒烟训练），在日志中搜索 `=== BEFORE IMAGE PROCESSOR PARAMETERS ===` 与 `Updated image_processor max_pixels`。
3. 修改 `--max_pixels 131072`（即 \(128 \times 32 \times 32\)，Qwen3-VL 口径 128 token）再跑一次，对比同一张图样本的 `input_ids` 长度。

**需要观察的现象**：BEFORE 段打印的是模型 processor 配置文件里的原始预算；AFTER 段应变成你命令行传入的值；增大 max_pixels 后同图的序列长度上升。

**预期结果**：AFTER 日志与命令行一致；`input_ids` 长度增长幅度与 token 换算大致吻合（对齐导致略有出入）。（待本地验证：需要 GPU 环境启动训练。）

#### 4.2.5 小练习与答案

**练习 1**：想让每个视频最多约 256 个 token（Qwen3-VL 口径），帧数上限 16，该怎么设？
**答案**：总预算 \(256 \times 1024 \times 2 = 524288\) 像素（时间 2× 压缩所以乘 2），设 `--video_max_pixels 524288 --video_max_frames 16`。若不同时给足总预算，16 帧会被压成低分辨率。

**练习 2**：为什么调大 `video_fps` 到 8，帧数却停在 8？
**答案**：帧数估算为「时长 × fps」后被 `[video_min_frames, video_max_frames]` 夹紧，默认 `video_max_frames=8` 是瓶颈；需同时调高 `video_max_frames`（并注意偶数对齐）。

**练习 3**：`--data_flatten True` 与 `--data_packing True` 有什么共同后果？
**答案**：两者都会触发 `replace_qwen2_vl_attention_class()`（varlen 注意力补丁）并选用 `FlattenedDataCollatorForSupervisedDataset`；区别是 flatten 在 collator 阶段拼接 batch、packing 则以 pack_data.py 产出的桶为训练单元（u4-l4）。

---

### 4.3 TrainingArguments 与 LoRA 扩展：继承、覆盖与生效条件

#### 4.3.1 概念说明

这组 dataclass 是「三层叠加」：最底层是 transformers 的上百个原生训练参数；中间层是仓库**覆盖默认值**的 2 个字段（`cache_dir`、`optim`）；最上层是**新增**的 6 个字段——序列长度 `model_max_length`、分层学习率 `mm_projector_lr / vision_tower_lr`，以及 LoRA 四件套 `lora_enable / lora_r / lora_alpha / lora_dropout`。本模块的重点不是罗列，而是搞清**每个新增参数的生效条件**——其中有两个很容易踩的坑。

#### 4.3.2 核心流程

```
TrainingArguments(transformers.TrainingArguments)
│  覆盖默认: cache_dir=None, optim="adamw_torch", model_max_length=512(必须显式调大!)
│
├─ model_max_length ─► tokenizer 加载参数 ─► 常规 collator 截断（Flattened collator 不截断）
│
├─ lora_enable=True ─► peft 分支: 全冻结 + 注入 q/k/v/o_proj 低秩适配器
│      （与 set_model 互斥 → 此时 tune_mm_* 全部失效）
│      lora_r/alpha/dropout 经 "or 兜底" 后生效（见 4.3.3）
│
└─ mm_projector_lr ≠ None 且 ≠ 0 ─► create_optimizer 分组
       ├─ 名称含 "merger" 的参数 → lr = mm_projector_lr
       └─ （嵌套）vision_tower_lr ≠ None 且 ≠ 0 时
              名称含 "visual" 的参数 → lr = vision_tower_lr
       否则 → 常规两组（decay / no-decay）
```

#### 4.3.3 源码精读

定义与覆盖：

- [qwen-vl-finetune/qwenvl/train/argument.py:28-45](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/argument.py#L28-L45) —— 继承 `transformers.TrainingArguments`；`model_max_length` 默认仅 512，多模态样本几乎必然超限，所以脚本必须显式传（4B 脚本传 8192）；`mm_projector_lr / vision_tower_lr` 默认 None；LoRA 四参数默认 `False / 64 / 128 / 0.0`。

`model_max_length` 的三处消费：

- [qwen-vl-finetune/qwenvl/train/train_qwen.py:155-161](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L155-L161) —— 传入 tokenizer 构造，tokenizer 的截断/警告阈值随之改变。
- [qwen-vl-finetune/qwenvl/data/data_processor.py:554-556](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L554-L556) —— 常规 collator 把 `input_ids / labels / position_ids` 硬截到该长度（超过部分直接丢，视觉 token 超预算时相当于静默截尾）。
- 对照 [data_processor.py:610-635](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L610-L635) —— `FlattenedDataCollator` **没有**任何截断逻辑。因此 `data_flatten=True` 时序列可以超过 `model_max_length`，该参数此时主要作为 tokenizer 属性存在。这解释了为什么用 flatten/packing 时更要在 pack_data 侧控制桶长（u4-l4：桶容量不得超过 model_max_length）。

LoRA 分支与 `or` 兜底的三个细节：

- [qwen-vl-finetune/qwenvl/train/train_qwen.py:163-180](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L163-L180) —— ① `lora_enable=True` 时先冻结全部参数再 `get_peft_model`，`set_model` 位于 `else` 分支，**不执行**，所以 LoRA 脚本里的 `--tune_mm_*` 是「被解析但不生效」的摆设参数（对照 [sft_30a3b_lora.sh:36-41](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_30a3b_lora.sh#L36-L41)，注意它既传 `--tune_mm_mlp True` 又传 `--lora_enable True`）。② 构造 `LoraConfig` 用的是 `training_args.lora_dropout or 0.05`：`or` 兜底遇到 falsy 值会回退，而 dataclass 默认 `lora_dropout=0.0` 正是 falsy——**不显式传时实际 dropout 是 0.05，不是字段表面写的 0.0**；同理传 `--lora_r 0` 会静默回落 64。③ `target_modules=["q_proj","k_proj","v_proj","o_proj"]` 硬编码在代码里，命令行改不了。

分层学习率的嵌套条件（本讲最大的坑）：

- [qwen-vl-finetune/qwenvl/train/trainer.py:316-330](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L316-L330) —— `create_optimizer`（monkey patch 到 HF Trainer，见 [trainer.py:495](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L495)）的分组开关是外层 `if self.args.mm_projector_lr is not None and != 0`；`vision_tower_lr` 的判断**嵌套在内层**。
- [qwen-vl-finetune/qwenvl/train/trainer.py:466-484](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L466-L484) —— 外层不成立时走 else：只有常规 decay / no-decay 两组。**结论：只设 `--vision_tower_lr` 而不设 `--mm_projector_lr` 时，vision_tower_lr 完全不会生效**。想给视觉塔单独设学习率，必须两个都传（mm_projector_lr 可设成与全局学习率相同的值）。
- [qwen-vl-finetune/qwenvl/train/trainer.py:321-322](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L321-L322) —— 分组前先从 decay 名单里剔除名字含 `bias` 的参数（bias 一律进 no-decay 组）；分组匹配靠参数名子串：含 `"merger"` 进 projector 组、含 `"visual"` 进视觉塔组（注意 merger 参数同时含 `visual` 前缀，所以代码里用 `not in projector_parameters` 排除，见 [trainer.py:331-358](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L331-L358)）。

#### 4.3.4 代码实践

**实践目标**：用「干跑解析」验证 LoRA 参数兜底行为，不加载任何模型。

**操作步骤**：

```python
# 示例代码：peek_lora.py，接 4.1.4 的解析框架
argv = [
    "--model_name_or_path", "Qwen/Qwen3-VL-2B-Instruct",
    "--output_dir", "/tmp/out",
    "--lora_enable", "True", "--lora_r", "0", "--lora_dropout", "0.0",
]
m, d, t = parser.parse_args_into_dataclasses(argv)
# 模拟 train_qwen.py L171-173 的 or 兜底
print(t.lora_r or 64)          # 预期 64（0 是 falsy，被兜底吞掉）
print(t.lora_dropout or 0.05)  # 预期 0.05（0.0 是 falsy）
print(t.mm_projector_lr, t.vision_tower_lr)  # 预期 None None
```

**需要观察的现象**：dataclass 里存的仍是 `0` 和 `0.0`，但经 `or` 表达式后变成 `64` 和 `0.05`。

**预期结果**：与上面注释一致，证明「字段默认值」与「实际生效值」可能不同。（待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：为什么说 `model_max_length=8192` 在 `data_flatten=True` 时「管不住」序列长度？
**答案**：截断只发生在常规 `DataCollatorForSupervisedDataset`（L554-556）；Flattened collator 不截断，拼接后的序列可以超过 8192，需要靠 pack_data 的桶容量或样本本身长度控制。

**练习 2**：想让视觉塔用 2e-6、其余用 1e-5，正确的命令行是什么？
**答案**：`--learning_rate 1e-5 --vision_tower_lr 2e-6 --mm_projector_lr 1e-5`。最后一个必须传——它是分组逻辑的总开关，不传则 vision_tower_lr 无效。

**练习 3**：`--lora_enable True` 时把 `--tune_mm_llm True` 一并传了，会发生什么？
**答案**：参数被解析进 `model_args`，但 LoRA 分支先全冻结再注入 adapter，`set_model` 不执行，`tune_mm_llm` 无任何效果——训练日志里可训练参数只有 LoRA 适配器。

---

### 4.4 脚本参数对照：sft_qwen3_4b.sh 逐条归档

#### 4.4.1 概念说明

启动脚本是三组 dataclass 的「实例化文档」。`sft_qwen3_4b.sh` 的结构是：上半部分用 bash 变量集中管理可调项，下半部分拼成 `args` 字符串交给 torchrun。读懂它的标志是：对任何一个 `--` 参数，能立刻说出它属于哪组 dataclass（还是 torchrun/HF 原生）、值是什么含义、被哪段代码消费。

#### 4.4.2 核心流程

```
bash 变量区（deepspeed/llm/lr/batch_size/...）
   ▼ 拼接
args 字符串（30+ 个 -- 参数）
   ▼ torchrun --nproc_per_node=... 启动
train_qwen.py → HfArgumentParser 解析（参数归属三组 dataclass）
   ▼
torchrun 自身消费 3 个：--nproc_per_node / --master_addr / --master_port
（这三个不进 dataclass，torchrun 在 Python 进程拉起前就处理完了）
```

注意脚本的一个细节：`--nproc_per_node=${NPROC_PER_NODE}` 引用的环境变量**没有默认值**（[sft_qwen3_4b.sh:63-66](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_qwen3_4b.sh#L63-L66)），运行前必须 `export NPROC_PER_NODE=<GPU数>`，否则 torchrun 拿到空值报错。

#### 4.4.3 源码精读（逐条对照表）

脚本本体：[qwen-vl-finetune/scripts/sft_qwen3_4b.sh:30-60](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_qwen3_4b.sh#L30-L60)。逐参数归档：

| 参数 | 值 | 归属 | 含义与消费点 |
| --- | --- | --- | --- |
| `--nproc_per_node` | `${NPROC_PER_NODE}` | torchrun 层 | 每节点进程数＝GPU 数，需预先 export（L63） |
| `--master_addr / --master_port` | 127.0.0.1 / 随机 | torchrun 层 | 分布式通信地址，端口用 `shuf` 随机避冲突（L4-5） |
| `--deepspeed` | `./scripts/zero3.json` | HF 原生 TrainingArguments | DeepSpeed 配置文件路径；Trainer 据此初始化 ZeRO-3（u5-l8） |
| `--model_name_or_path` | `Qwen/Qwen3-VL-4B-Instruct` | **ModelArguments** | 模型与代际判定依据（train_qwen.py L103-139） |
| `--dataset_use` | `public_dataset1,public_dataset2` | **DataArguments** | 数据集代号，逗号分隔，支持 `%X` 采样（u4-l3） |
| `--data_flatten` | `True` | **DataArguments** | collator 拼接 + varlen 注意力（L141-142） |
| `--tune_mm_vision` | `False` | **ModelArguments** | 冻结视觉编码器（set_model） |
| `--tune_mm_mlp` | `True` | **ModelArguments** | 训练 merger |
| `--tune_mm_llm` | `True` | **ModelArguments** | 训练语言模型与 lm_head |
| `--bf16` | （无值＝True） | HF 原生 | 训练用 bf16；同时决定 `from_pretrained(dtype=bfloat16)`（L108） |
| `--output_dir` | `./output` | HF 原生 | 产物目录；存在 `checkpoint-*` 会自动续训（L191-195） |
| `--num_train_epochs` | `0.5` | HF 原生 | 训练轮数（半轮，正式任务请调满） |
| `--per_device_train_batch_size` | `4` | HF 原生 | 单卡每步样本数 |
| `--per_device_eval_batch_size` | `8` | HF 原生 | 评测批大小（本脚本 `eval_strategy no` 用不到） |
| `--gradient_accumulation_steps` | `4` | HF 原生 | 梯度累积，与批大小共同决定有效 batch |
| `--max_pixels / --min_pixels` | `50176 / 784` | **DataArguments** | 图像像素预算，28 口径遗产；Qwen3-VL 下约 49 / 1 个视觉 token |
| `--eval_strategy` | `no` | HF 原生 | 不做训练中评测（数据模块也返回 eval_dataset=None） |
| `--save_strategy / --save_steps / --save_total_limit` | `steps / 1000 / 1` | HF 原生 | 每 1000 步存档且只留 1 份 |
| `--learning_rate` | `1e-5` | HF 原生 | 全局学习率（全量微调常用量级；7B 脚本降到 2e-7，见 [sft_7b.sh:15](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_7b.sh#L15)） |
| `--weight_decay` | `0` | HF 原生 | 权重衰减 |
| `--warmup_ratio` | `0.03` | HF 原生 | 前 3% 步数线性升温 |
| `--max_grad_norm` | `1` | HF 原生 | 梯度裁剪阈值 |
| `--lr_scheduler_type` | `cosine` | HF 原生 | 余弦退火 |
| `--logging_steps` | `1` | HF 原生 | 每步打日志 |
| `--model_max_length` | `8192` | **TrainingArguments（本仓库新增）** | tokenizer 上限与常规 collator 截断长度（默认仅 512） |
| `--gradient_checkpointing` | `True` | HF 原生 | 换算力省显存；配套 `enable_input_require_grads`（L145-153） |
| `--dataloader_num_workers` | `4` | HF 原生 | 数据加载子进程数 |
| `--run_name / --report_to` | `qwen3vl / wandb` | HF 原生 | 实验追踪；不打算用 wandb 时改成 `none` |

**脚本里没出现、但你会用到的参数**：`--lora_enable/--lora_r/--lora_alpha/--lora_dropout`（见 [sft_30a3b_lora.sh:41](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_30a3b_lora.sh#L41)）、`--mm_projector_lr/--vision_tower_lr`、`--data_packing`、`--cache_dir/--optim`（有默认值）、视频五参（默认值生效）。另一个脚本间差异：MoE 模型只能用 zero2（[sft_30a3b_lora.sh:9-11](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_30a3b_lora.sh#L9-L11) 注释明说），这是 u5-l8 的主题，此处先记住结论。

**有效 batch size**：\( \text{有效batch} = \text{GPU数} \times \text{per\_device\_batch} \times \text{grad\_accum} \)。假设 8 卡跑 4B 脚本：\(8 \times 4 \times 4 = 128\)。

#### 4.4.4 代码实践

**实践目标**：制作一张自己的参数标注表，并改写出单卡 2B 启动脚本。

**操作步骤**：

1. **标注作业**：把 4.4.3 的表格抄成三列作业（参数/归属/一句话解释），遮住后两列自测，重点覆盖 ModelArguments 4 项、DataArguments 4 项、本仓库新增的 `--model_max_length`。
2. **改写脚本**：在 `qwen-vl-finetune/scripts/` 之外（例如 `Qwen3-VL-tutorial/` 或你自己的工作目录）新建 `sft_qwen3_2b_single.sh`，以下给出参考版本（示例代码）：

```bash
#!/bin/bash
# 单卡 24G 训练 Qwen3-VL-2B：基于 sft_qwen3_4b.sh 改写
export NPROC_PER_NODE=1                     # 理由：原脚本无默认值，单卡必须显式给 1
MASTER_ADDR=${MASTER_ADDR:-"127.0.0.1"}
MASTER_PORT=${MASTER_PORT:-$(shuf -i 20001-29999 -n 1)}

deepspeed=./scripts/zero2.json              # 理由：单卡 2B 无需 ZeRO-3 分片，
                                            # zero2 只切优化器状态，通信开销更小
llm=Qwen/Qwen3-VL-2B-Instruct               # 末段不含字母 a，代际嗅探正确落到 Dense 分支

lr=1e-5
batch_size=1                                # 理由：24G 显存下 8192 长度多模态样本
                                            # 每卡 batch=4 极易 OOM，降为 1
grad_accum_steps=16                         # 理由：把累积步调大，维持有效 batch
                                            # 1x1x16=16，接近小规模实验的合理下限

entry_file=qwenvl/train/train_qwen.py
datasets=public_dataset1                    # 换成你在 u4-l3 注册的数据集代号
run_name="qwen3vl-2b-single"
output_dir=./output_2b

args="
    --deepspeed ${deepspeed} \
    --model_name_or_path "${llm}" \
    --dataset_use ${datasets} \
    --data_flatten True \
    --tune_mm_vision False \
    --tune_mm_mlp True \
    --tune_mm_llm True \
    --bf16 \
    --output_dir ${output_dir} \
    --num_train_epochs 1 \
    --per_device_train_batch_size ${batch_size} \
    --per_device_eval_batch_size $((batch_size*2)) \
    --gradient_accumulation_steps ${grad_accum_steps} \
    --max_pixels 131072 \
    --min_pixels 1024 \
    --eval_strategy no \
    --save_strategy steps \
    --save_steps 500 \
    --save_total_limit 1 \
    --learning_rate ${lr} \
    --weight_decay 0 \
    --warmup_ratio 0.03 \
    --max_grad_norm 1 \
    --lr_scheduler_type cosine \
    --logging_steps 1 \
    --model_max_length 4096 \
    --gradient_checkpointing True \
    --dataloader_num_workers 2 \
    --run_name ${run_name} \
    --report_to none"

torchrun --nproc_per_node=${NPROC_PER_NODE} \
         --master_addr=${MASTER_ADDR} \
         --master_port=${MASTER_PORT} \
         ${entry_file} ${args}
```

改动理由集中在四处：
- `NPROC_PER_NODE=1`：原脚本该变量无默认值（L63），不 export 直接跑会失败；
- `batch_size=1 / grad_accum=16`：单卡显存远小于 4B 脚本假设的多卡环境，用「小批大累积」保住有效 batch；
- `max_pixels 131072 = 128×32×32 / min_pixels 1024 = 1×32×32`：把 28 口径遗产换成 Qwen3-VL 的 32 口径整数表达，预算即 128 个视觉 token 上限、1 个 token 下限，一眼可换算；
- `model_max_length 4096`：单卡 24G 下 8192 长度样本 + 梯度检查点仍偏紧，先降半做冒烟，稳定后再调回。

**需要观察的现象**：启动后日志先出现 `the initlized model is ... the class is Qwen3VLForConditionalGeneration`（确认 Dense 分支没被嗅探错），随后 `update_processor_pixels` 的 AFTER 段显示 `max_pixels: 131072`。

**预期结果**：训练正常出 loss 并反传。（待本地验证：需要 GPU 与 u4-l1 的依赖环境；先以 `num_train_epochs 0.01` 做冒烟。）

#### 4.4.5 小练习与答案

**练习 1**：8 卡跑原版 4B 脚本的有效 batch 是多少？改成 2 卡后想保持不变该怎么调？
**答案**：\(8 \times 4 \times 4 = 128\)；2 卡时\(2 \times 4 \times 16 = 128\)，把 `grad_accum_steps` 从 4 调到 16。

**练习 2**：`--deepspeed`、`--model_name_or_path`、`--max_pixels`、`--nproc_per_node` 四个参数分别归谁管？
**答案**：依次是 HF 原生 TrainingArguments（Trainer 消费）、ModelArguments（代际判定+模型加载）、DataArguments（update_processor_pixels 注入 processor）、torchrun（不进任何 dataclass，进程拉起前消费）。

**练习 3**：为什么 `sft_7b.sh` 的学习率是 2e-7，而 4B 脚本是 1e-5？
**答案**：模型越大，微调越接近收敛点，过大的学习率容易破坏预训练能力（灾难性遗忘），所以更大规格的脚本配更小的学习率；这是全量微调的常见经验，LoRA 场景则通常可以用更大的学习率。

---

## 5. 综合实践

**任务：给「图文问答小数据集」配一套完整训练参数并论证。**

假设你有 500 条单图问答样本（已在 u4-l3 注册为 `my_dataset`），在 1 张 24G GPU 上微调 Qwen3-VL-2B-Instruct。请产出三样东西：

1. **参数决策表**：从本讲 4.4.3 的表格出发，逐行决定沿用/修改/删除，每处修改附一句理由（提示需要决策的点：LoRA 还是全量？`data_flatten` 开不开？`max_pixels` 给多少？`model_max_length` 与最长样本的关系？）。
2. **预算核算**：设 `max_pixels=131072`，估算最坏情况下单条样本的图像 token 数（答：128），再加上对话文本 token，验证 `model_max_length` 取 4096 是否留有余量。
3. **干跑验证**：用 4.1.4 / 4.3.4 的 HfArgumentParser 干跑代码，把你决策表里的参数原样传入，确认解析无报错、关键字段值与预期一致——这一步不需要 GPU，是把「参数写错」扼杀在训练启动前最低成本的手段。

最后照 4.4.4 的参考脚本落地为 `.sh` 文件，先以 `--num_train_epochs 0.01` 冒烟（待本地验证），再正式训练。

## 6. 本讲小结

- 全部可配置项收敛在 `argument.py` 的三组 dataclass：`ModelArguments`（模型身份 + 三开关可训练范围）、`DataArguments`（数据组织 3 项 + 视觉预算 8 项）、`TrainingArguments`（继承 HF 后新增 6 项：model_max_length、分层学习率 2 项、LoRA 4 项）。
- 视觉预算八参数经 `update_processor_pixels` 注入 image/video processor 的 `min/max_pixels`、`min/max_frames`、`fps` 与 `size` 字典，沿「预算 → 缩放尺寸 → 视觉 token 数 → 序列长度」因果链生效；像素默认值是 28 口径遗产，Qwen3-VL 换算要除以 1024。
- 三个「传了也不生效/没入口」的陷阱：LoRA 模式下 `tune_mm_*` 全失效；只设 `vision_tower_lr` 不设 `mm_projector_lr` 分层不生效；`base_interval` 在训练主链路无消费、视频总像素参数无命令行入口。
- `model_max_length` 只在常规 collator 截断，flatten/packing 路径不限长；LoRA 的 `or` 兜底使默认 dropout 实际为 0.05。
- 读脚本的正确姿势：每个 `--` 参数先归到 torchrun 层 / 三组 dataclass 之一 / HF 原生，再找消费点；有效 batch ＝ GPU 数 × per_device 批 × 梯度累积。

## 7. 下一步学习建议

参数体系就位后，下一步深入**参数如何被数据侧消费**：先读 u5-l3（`LazySupervisedDataset` 与标签掩码构造），看 `dataset_use` 解析出的样本如何变成 `input_ids/labels`；再读 u5-l4（rope2d.py），理解 `position_ids` 如何随 `model_type` 选择三代实现。若你更关心 LoRA 实跑，可直接跳 u5-l7（LoRA 微调与分层学习率实战），把本讲 4.3 的生效条件在真实训练里复现一遍。
