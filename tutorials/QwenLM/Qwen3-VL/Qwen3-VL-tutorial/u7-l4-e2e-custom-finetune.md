# 端到端实战：自定义数据集微调与评测闭环

## 1. 本讲目标

这是全手册的收官讲义。前面六个单元分别拆解了输入预处理、推理部署、数据格式、训练源码、评测脚手架与高阶应用；本讲把这些零件重新组装成一条**可复现的流水线**：

> 自定义任务 → 制作数据 → 注册数据集 → LoRA 微调 → 合并导出 → 复用评测脚本对比微调前后效果 → 输出实验报告。

学完本讲，你应该能够：

1. 独立完成「数据制作 → 注册 → 训练 → 合并 → 评测」的端到端全流程，不再依赖任何人贴出现成命令。
2. 根据任务类型（识别 / 图表问答 / OCR / 定位）设计训练超参：学习率、轮数、像素预算、LoRA 秩。
3. 定位闭环中最常见的训练侧与推理侧报错，知道每一类错误该去哪个文件、哪一行找原因。

本讲的自测标准只有一个：**换一个新任务，你能不翻讲义地把这条流水线再跑一遍。**

## 2. 前置知识

本讲是「缝合」讲义，默认你已掌握以下前置内容（均来自依赖讲义），此处只做一页速查，不再展开。

### 2.1 来自 u5-l7：LoRA 微调要点

- LoRA 路径先**全冻结**基座，再向注意力的 `q_proj/k_proj/v_proj/o_proj` 四个投影注入低秩适配器（增量 \( BA \)，缩放系数 \( \alpha/r \)）。
- **LoRA 分支下 `tune_mm_vision/tune_mm_mlp/tune_mm_llm` 三开关全部失效**——这就是本讲选 LoRA 做闭环的原因：单卡可跑、参数少、不需要纠结三开关组合。
- 代码里 `lora_dropout` 的 `or 0.05` 兜底写法使其实际恒为 0.05。
- 官方 LoRA 建议（README）：`lora_r=8`、`lora_alpha=16`。

### 2.2 来自 u6-l2：MMMU 评测要点

- 评测脚本拆成 `infer` / `eval` 两个子命令，以 **JSONL 文件**为唯一接口，GPU 推理与判分解耦。
- `prepare_inputs_for_vllm` 三件套（`prompt` / `multi_modal_data` / `mm_processor_kwargs`）是仓库级稳定惯用法，可逐字复用。
- 判分是「规则抽取 → LLM judge 兜底 → 随机保底」三级瀑布；但对单字母选择题，规则抽取一层通常就够。

### 2.3 贯穿全手册的两个口径事实

- **像素口径**：Qwen3-VL 的 patch 边长为 16、2×2 空间合并，故一张 \( H' \times W' \) 的图产生 \( \frac{H'}{32} \times \frac{W'}{32} \) 个视觉 token，即像素数 ÷ 1024。仓库里大量 `28*28` 常数是 Qwen2/2.5-VL 时代遗产（÷ 784）。
- **模型结构不在本仓库**：训练入口按模型类名从 transformers 导入模型类；评测侧用 vLLM 引擎加载，两个加载器互不嗅探对方。

### 2.4 实验方法论速查

| 概念 | 一句话说明 |
| --- | --- |
| 自测集 | 自己构造的小规模带真值集合，用于横向对比模型版本，不能替代公开基准 |
| 冒烟训练 | 用极小轮数（如 `num_train_epochs=0.01`）验证链路能跑通、loss 能反传 |
| 有效批量 | \( \text{GPU 数} \times \texttt{per\_device\_train\_batch\_size} \times \texttt{gradient\_accumulation\_steps} \) |
| 配对对比 | 微调前后跑**同一份**测试输入、同一套采样参数，只比较模型权重这一个变量 |
| adapter | LoRA 训练产物，只含低秩增量权重，须依附基座才能推理 |
| merge | 把 adapter 增量算进基座权重，导出独立完整模型目录的过程 |

## 3. 本讲源码地图

| 文件 | 在闭环中的角色 | 本讲关注点 |
| --- | --- | --- |
| [qwen-vl-finetune/demo/single_images.json](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/demo/single_images.json) | 数据格式的官方样例 | 图表问答样本长什么样 |
| [qwen-vl-finetune/qwenvl/data/\_\_init\_\_.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/__init__.py) | 数据集注册表 | 如何挂载自定义数据集 |
| [qwen-vl-finetune/qwenvl/train/train_qwen.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py) | 训练入口 | LoRA 分支与保存三件套 |
| [qwen-vl-finetune/qwenvl/train/argument.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/argument.py) | 参数定义 | LoRA 四件套字段 |
| [qwen-vl-finetune/scripts/sft_qwen3_4b.sh](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_qwen3_4b.sh) | 全参训练脚本模板 | 脚本三段式结构 |
| [qwen-vl-finetune/scripts/sft_30a3b_lora.sh](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_30a3b_lora.sh) | LoRA 训练脚本模板 | `--lora_enable True` 一行差异 |
| [qwen-vl-finetune/tools/check_image.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/check_image.py) | 数据校验工具 | 训练前的完整性体检 |
| [evaluation/mmmu/run_mmmu.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py) | 评测脚手架 | 哪些部分可逐字复用为自测集脚本 |

## 4. 核心概念与源码讲解

### 4.1 数据制作与注册

#### 4.1.1 概念说明

闭环的第一环是把「你的业务素材」翻译成框架认识的**标注格式**，再把标注文件「挂」进框架的**注册表**。

- **标注格式**（u4-l2 已精读）：每条样本由媒体字段（`image` / `video`，路径相对 `data_path` 解析）与 `conversations`（`human` / `gpt` 交替）构成；`<image>` 占位符只放在 human 轮，与媒体按出现顺序一一配对。
- **注册表**（u4-l3 已精读）：`data/__init__.py` 里的 `data_dict` 把一个代号映射为 `annotation_path`（标注文件）与 `data_path`（媒体基准目录）两个键。训练命令里的 `--dataset_use` 只认注册表里的代号。
- **为什么需要注册表**：训练脚本通过代号引用数据，可以把「用哪些数据、用多少比例」作为命令行参数自由组合（`--dataset_use a,b,c%50`），而无需改训练代码。注册表是框架预留给使用者的**配置点**——在自己的环境里编辑它是官方预期用法，不算改框架逻辑。

本讲的示例任务选**图表问答**（chart QA）：输入一张柱状图/折线图，回答数值与比较类问题。选它有三个理由：官方 demo 本身就是图表数据、任务输出短（评测容易）、是真实业务中最常见的微调诉求之一。

#### 4.1.2 核心流程

数据制作到可训练状态的完整检查链：

```text
① 采集素材      20 张图表图片 + 为每张写 1 条问答
② 编写标注      每条样本 { "image": 路径, "conversations": [human, gpt] }
③ 静态校验      check_image.validate_data() → *_valid.json / *_problems.json
④ 注册          在 data_dict 中新增 {"my_chart": {"annotation_path": ..., "data_path": ...}}
⑤ 验证解析      python -m qwenvl.data 之外，单独调用 data_list(["my_chart"]) 确认能解析
```

注册表解析的伪代码（对应 `data_list` 的真实逻辑）：

```text
for name in dataset_use 按逗号切分:
    sampling_rate = 末尾 %X → X/100（无则 1.0）
    name = 去掉 %X 后缀
    if name 不在 data_dict: 抛 ValueError("do not find ...")
    config = data_dict[name].copy(); config["sampling_rate"] = sampling_rate
```

注意两个坑（承接 u4-l3）：逗号后不要带空格；`%X` 大于 100 会把抽样条数顶到全体之后崩溃。

#### 4.1.3 源码精读

**标注格式的官方样例**——第一条样本就是图表问答，`<image>` 占位符放在 human 轮的问题文本末尾：

- [qwen-vl-finetune/demo/single_images.json:L1-L14](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/demo/single_images.json#L1-L14) —— 样本结构：`image` 键给相对路径 `demo/images/10095.png`，`conversations` 里 human 轮是「问题文本 + 换行 + `<image>`」，gpt 轮是极简答案 `"Yes"`。注意答案是**纯答案**，不带任何解释——这是标注纯净性要求（gpt 轮出现媒体占位符会被判为坏样本）。

**注册表本体**：

- [qwen-vl-finetune/qwenvl/data/\_\_init\_\_.py:L29-L35](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/__init__.py#L29-L35) —— `data_dict` 把五个代号映射到各自的配置字典。你的自定义数据集就要在这里新增一个键，形如：

  ```python
  # 示例代码：注册自定义图表问答数据集（加在 data_dict 里）
  MY_CHART = {
      "annotation_path": "/abs/path/my_chart_valid.json",  # 校验后的干净标注
      "data_path": "/abs/path/images",                     # image 路径的基准目录
  }
  ```

  `annotation_path` 建议用绝对路径或相对仓库根的稳定路径；`data_path` 是媒体基准目录，标注里的 `image` 相对它解析。

**采样率解析**：

- [qwen-vl-finetune/qwenvl/data/\_\_init\_\_.py:L38-L42](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/__init__.py#L38-L42) —— `parse_sampling_rate` 用正则 `%(\d+)$` 从代号末尾抽百分比，除以 100 得采样率；没有后缀就返回 1.0（全量）。
- [qwen-vl-finetune/qwenvl/data/\_\_init\_\_.py:L45-L56](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/__init__.py#L45-L56) —— `data_list` 逐个解析代号：先抽采样率、剥后缀、查注册表、`copy()` 后注入 `sampling_rate`；查不到就 `raise ValueError(f"do not find {dataset_name}")`——**训练一启动就报 `do not find xxx`，九成是 `--dataset_use` 的代号与注册表键不一致（大小写、多余空格）**。

**静态校验工具**：

- [qwen-vl-finetune/tools/check_image.py:L6-L28](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/check_image.py#L6-L28) —— `validate_data(json_file_path, media_folder_path)` 是入口函数（该脚本没有命令行参数，从 Python 里导入调用）：校验媒体存在性、human 轮占位符与媒体数配对、gpt 轮纯净性，然后写出一对结果文件。
- [qwen-vl-finetune/tools/check_image.py:L19-L21](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/tools/check_image.py#L19-L21) —— 输出文件名规则：`xxx.json` → `xxx_valid.json`（可训练）与 `xxx_problems.json`（问题样本）。**为什么要先校验**：`__getitem__` 对坏样本会静默重试换成相邻样本（u5-l3），不校验的话你永远不知道有样本被丢掉。
- [qwen-vl-finetune/README.md:L215](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L215) —— README 明确建议：对可能缺图的开源数据，训练前用 `tools/check_image.py` 校验完整性。自建数据同样适用——手滑写错路径是最高频的错误。

#### 4.1.4 代码实践

**实践目标**：制作 20 条图表问答样本，完成校验与注册，让训练侧「看得见」这份数据。

**操作步骤**（以下脚本均为示例代码，放在仓库外的自有目录更稳妥）：

1. 准备 20 张图表图片存入 `/data/my_chart/images/`（可用 matplotlib 批量生成，也可以找公开图表图集）。
2. 编写标注 `/data/my_chart/my_chart.json`，逐条模仿 `single_images.json` 的结构：

   ```json
   [
     {
       "image": "chart_001.png",
       "conversations": [
         {"from": "human", "value": "Which year has the highest revenue?\n<image>"},
         {"from": "gpt", "value": "2023"}
       ]
     }
   ]
   ```

   20 条里建议混排两类问题：「读数」（某年值是多少）与「比较」（哪年最高 / 多少个值低于 40，后者正是官方 demo 的问法）。
3. 校验（示例代码）：

   ```python
   import sys; sys.path.insert(0, "qwen-vl-finetune")
   from tools.check_image import validate_data
   validate_data("/data/my_chart/my_chart.json", "/data/my_chart/images")
   ```

4. 在 `qwen-vl-finetune/qwenvl/data/__init__.py` 的 `data_dict` 中注册 `my_chart`（`annotation_path` 指向 `my_chart_valid.json`）。
5. 验证注册（示例代码）：

   ```python
   import sys; sys.path.insert(0, "qwen-vl-finetune")
   from qwenvl.data import data_list
   print(data_list(["my_chart", "my_chart%50"]))
   ```

**需要观察的现象**：第 3 步打印的统计里 `valid_entries` 是否等于 20；第 5 步打印出两个 config 字典，且第二个的 `sampling_rate` 是 0.5。

**预期结果**：`*_problems.json` 为空列表；`data_list` 输出含 `annotation_path`、`data_path`、`sampling_rate` 三键。若第 5 步抛 `do not find my_chart`，回到注册表检查键名。

**待本地验证**：`validate_data` 内部统计字段的具体打印格式以实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `annotation_path` 写成了未校验的原始 `my_chart.json`，且其中第 7 条样本的图片路径写错了，训练时会发生什么？
**答案**：不会报错中断。`LazySupervisedDataset.__getitem__` 的容错重试机制会静默丢弃该样本并换相邻样本（u5-l3），表面上训练正常，实际有效样本变成 19 条。这正是必须先跑 `check_image.py` 的原因。

**练习 2**：`--dataset_use my_chart%50` 在 20 条样本上会抽多少条？为什么不建议对小数据集用采样率？
**答案**：`20 × 0.5 = 10` 条（条数向下取整，u4-l3）。小数据集本就稀缺，再抽样会让每 epoch 的有效梯度信号更少；采样率是为「从百万级开源数据里按比例混合」设计的，自建小数据一律用 100%。

**练习 3**：gpt 轮答案写成 `"The answer is 2023\n<image>"` 会触发哪类校验失败？
**答案**：两类：gpt 轮出现媒体占位符（`gpt_media_tokens` 统计项），且占位符总数与媒体数不配对（`token_mismatches` 统计项）。正确写法是纯答案 `"2023"`。

### 4.2 训练脚本定制

#### 4.2.1 概念说明

仓库的启动脚本是**声明式的参数文档**：三段式结构（分布式环境变量 → 关键变量 → `args` 长串），最后用 `torchrun` 拉起训练入口。定制脚本不是写新代码，而是回答四个问题：

| 问题 | 依据 |
| --- | --- |
| 用全参还是 LoRA | 显存与数据量。20 条样本只够 LoRA（全参在极小数据上必过拟合） |
| 学习率多大 | LoRA 常规 1e-4 量级；全参 1e-5 量级（官方脚本即 1e-5） |
| 像素预算多大 | 按任务定：图表问答要读小字与刻度，预算应高于脚本默认 |
| 轮数多少 | 小数据 LoRA 训得快也忘得快，几轮足够，靠 eval 挑 checkpoint |

**像素预算的口径换算**是定制时最容易踩的坑：官方脚本的 `--max_pixels 50176` 是 28 口径遗产，对 Qwen3-VL 意味着

\[ N_{\text{img}} = \frac{50176}{32 \times 32} = 49 \text{ 个视觉 token} \]

对图表问答这通常**不够**（49 个 token 装不下坐标轴刻度），建议提到 `4608*32*32`（约 4608 视觉 token）这一档——注意 `max_pixels`/`min_pixels` 会被 `update_processor_pixels` 注入 processor（u5-l3），是训练侧真实生效的预算。

另一个必须记住的事实：**MoE 代际判定靠路径嗅探**，本地目录名最后一节含字母 `a` 且路径含 `qwen3` 就会被判成 MoE。自建模型目录命名要避开（详见 4.2.3）。

#### 4.2.2 核心流程

`train()` 从参数到落盘的七步（对应源码顺序）：

```text
① HfArgumentParser 解析三组 dataclass（模型/数据/训练）
② 按模型路径嗅探代际 → 从四个类中选一个 from_pretrained
③ 加载 AutoProcessor
④ data_flatten / data_packing 任一为真 → 替换注意力实现（varlen）
⑤ LoRA：全冻结 + 注入适配器；否则 set_model 三开关
⑥ make_supervised_data_module → Trainer → train（有 checkpoint-* 自动续训）
⑦ 三段式落盘：训练状态 / 权重 / processor 配置
```

启动侧一条铁律（承接 u5-l1）：**单卡也必须用 `torchrun`**，因为代码里出现了 `torch.distributed.get_rank()`。

#### 4.2.3 源码精读

**脚本模板的三段式**：

- [qwen-vl-finetune/scripts/sft_qwen3_4b.sh:L9-L23](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_qwen3_4b.sh#L9-L23) —— 关键变量区：`deepspeed=./scripts/zero3.json`、`llm=Qwen/Qwen3-VL-4B-Instruct`、`lr=1e-5`、`batch_size=4`、`grad_accum_steps=4`、`datasets=public_dataset1,public_dataset2`（占位符，**定制脚本第一个要改的就是它**，换成 `my_chart`）。有效批量 = GPU 数 × 4 × 4。
- [qwen-vl-finetune/scripts/sft_qwen3_4b.sh:L30-L60](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_qwen3_4b.sh#L30-L60) —— `args` 长串：`--data_flatten True`（L34）要求 varlen 注意力补丁配套；三开关（L35-L37）在 LoRA 下无效；`--max_pixels 50176`（L44）与 `--min_pixels 784`（L45）是 28 口径遗产；`--model_max_length 8192`（L56）；`--report_to wandb`（L60）——**本地没有 wandb 账号时会卡登录，改成 `none`**。
- [qwen-vl-finetune/scripts/sft_qwen3_4b.sh:L63-L66](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_qwen3_4b.sh#L63-L66) —— `torchrun` 拉起入口文件；`${NPROC_PER_NODE}` 需在环境里提供（如 `NPROC_PER_NODE=1`）。

**LoRA 模板只差一行**：

- [qwen-vl-finetune/scripts/sft_30a3b_lora.sh:L41](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_30a3b_lora.sh#L41) —— 与全参脚本的实质差异就是 `--lora_enable True`。注意该脚本头部注释（L9-L11）说明 MoE 只支持 ZeRO-2、需 32×80G——那是 30B-A3B 的门槛，我们换 2B 模型后单卡即可。

**入口源码的关键行**：

- [qwen-vl-finetune/qwenvl/train/train_qwen.py:L103-L118](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L103-L118) —— 代际嗅探：路径含 `qwen3` 且**末段目录名含字母 `a`** → 加载 MoE 类；否则含 `qwen3` → `Qwen3VLForConditionalGeneration`。陷阱示例：本地目录 `/models/qwen3-vl-2b-base` 含 `a`（`base`），会被误判为 MoE 而加载错误的类，随后在结构不匹配处炸出难懂的报错。**合并导出目录命名同样要避开 `a`**（如 `merged_2b` 就安全，`merged_adapter` 就危险）。评测侧 `run_mmmu.py` 不做嗅探，不受影响。
- [qwen-vl-finetune/qwenvl/train/train_qwen.py:L141-L142](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L141-L142) —— `data_flatten or data_packing` 为真时调用 `replace_qwen2_vl_attention_class()`：拼接训练必须整层替换注意力防样本间泄漏（u5-l6）。所以脚本里 `--data_flatten True` 与 flash_attn 依赖是绑定的，不能为省事关掉。
- [qwen-vl-finetune/qwenvl/train/train_qwen.py:L163-L178](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L163-L178) —— LoRA 分支全貌：L167-L168 先把**所有**参数 `requires_grad = False`；L170-L177 构造 `LoraConfig`，注意 `r=training_args.lora_r or 64` 这类 `or` 兜底——不传参时用 64/128，且 dropout 恒 0.05；`target_modules=["q_proj","k_proj","v_proj","o_proj"]`；L178 `get_peft_model` 包裹模型。
- [qwen-vl-finetune/qwenvl/train/argument.py:L42-L45](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/argument.py#L42-L45) —— LoRA 四件套字段定义：`lora_enable`（默认 False）、`lora_r`（64）、`lora_alpha`（128）、`lora_dropout`（0.0，但被 `or 0.05` 兜底覆盖）。README 的推荐组合是 `lora_r=8, lora_alpha=16`（[qwen-vl-finetune/README.md:L299-L302](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L299-L302)）。
- [qwen-vl-finetune/qwenvl/train/train_qwen.py:L191-L195](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L191-L195) —— 断点续训：`output_dir` 下存在 `checkpoint-*` 就自动 `resume_from_checkpoint=True`。**想重新训练必须先清空或换 output_dir**，否则你以为在训新模型，其实在续旧 checkpoint。
- [qwen-vl-finetune/qwenvl/train/train_qwen.py:L196-L202](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L196-L202) —— 训练结束的三段式落盘：`trainer.save_state()`（优化器等状态）、`safe_save_model_for_hf_trainer`（权重，DeepSpeed 与否走不同分支）、`processor.save_pretrained`（processor 配置）。LoRA 下保存的是 PEFT 包裹后的模型，产物是 adapter 而非完整权重——这是下一模块的出发点。

**训练侧常见报错速查表**：

| 症状 | 原因 | 处置 |
| --- | --- | --- |
| 启动即 `ValueError: do not find xxx` | `--dataset_use` 代号未注册 / 拼写不一致 | 对照 `data_dict` 键名，逗号后不留空格 |
| 卡在 wandb 登录提示 | `--report_to wandb` 而本机未配置 | 改 `--report_to none` |
| `torch.distributed...` 相关报错 | 直接 `python train_qwen.py` 未用 torchrun | 换 `torchrun --nproc_per_node=1` |
| 加载模型即报结构不匹配 / 参数缺失 | 本地模型目录末段含 `a` 被误判 MoE | 目录改名避开字母 `a`（如 `qwen3vl2b`） |
| 训练「莫名」从头没变 | output_dir 有旧 `checkpoint-*` 触发续训 | 清空 output_dir |
| attention 相关 shape 报错 | 关了 `--data_flatten` 或缺 flash_attn | 保持 `True` 并确认 flash_attn 已装（硬依赖） |

#### 4.2.4 代码实践

**实践目标**：把 4B 全参模板改写成单卡 2B LoRA 脚本，并完成一次冒烟训练。

**操作步骤**：新建 `scripts/sft_my_chart_2b_lora.sh`（示例代码，基于两份官方模板改写），关键差异处均已注释：

```bash
#!/bin/bash
# 示例代码：单卡 2B 图表问答 LoRA 脚本
MASTER_ADDR=${MASTER_ADDR:-"127.0.0.1"}
MASTER_PORT=${MASTER_PORT:-$(shuf -i 20001-29999 -n 1)}
NNODES=${WORLD_SIZE:-1}

deepspeed=./scripts/zero2.json          # 单卡 LoRA 用 zero2 足够
llm=Qwen/Qwen3-VL-2B-Instruct           # 2B 小模型，单卡可训
lr=1e-4                                  # LoRA 学习率抬到 1e-4 量级
batch_size=1
grad_accum_steps=4                       # 20 条样本，有效批量 4 即可
entry_file=qwenvl/train/train_qwen.py
datasets=my_chart                        # 4.1 注册的数据集代号

output_dir=./output/my_chart_lora        # 末段不含字母 a
args="
    --deepspeed ${deepspeed} \
    --model_name_or_path "${llm}" \
    --dataset_use ${datasets} \
    --data_flatten True \
    --tune_mm_vision False \
    --tune_mm_mlp True \
    --tune_mm_llm True \
    --bf16 \
    --lora_enable True \
    --lora_r 8 --lora_alpha 16 \
    --output_dir ${output_dir} \
    --num_train_epochs 5 \
    --per_device_train_batch_size ${batch_size} \
    --gradient_accumulation_steps ${grad_accum_steps} \
    --max_pixels 4718592 \
    --min_pixels 262144 \
    --eval_strategy "no" \
    --save_strategy "steps" --save_steps 20 --save_total_limit 2 \
    --learning_rate ${lr} \
    --weight_decay 0 --warmup_ratio 0.03 --max_grad_norm 1 \
    --lr_scheduler_type "cosine" --logging_steps 1 \
    --model_max_length 8192 \
    --gradient_checkpointing True \
    --dataloader_num_workers 2 \
    --run_name my-chart-lora \
    --report_to none"

NPROC_PER_NODE=1 torchrun --nproc_per_node=1 \
    --master_addr ${MASTER_ADDR} --master_port ${MASTER_PORT} \
    ${entry_file} ${args}
```

先做冒烟：把 `--num_train_epochs` 临时改成 `0.01` 跑一次，确认链路通后再改回。

**需要观察的现象**：启动后打印 `the initlized model is ... the class is Qwen3VLForConditionalGeneration`（类名不是 MoE）；LoRA 分支打印 `LoRA enabled`；每个 logging step 的 loss 从正常量级（几左右）开始缓慢下降；`print_trainable_parameters` 风格的可训练参数占比应远小于 1%（LoRA 只训练低秩增量）。

**预期结果**：`output/my_chart_lora/` 下出现 `checkpoint-*`、adapter 权重文件与 processor 配置文件。

**待本地验证**：20 条样本 × 5 epoch 的具体 step 数与最终 loss 值，以本地日志为准。

#### 4.2.5 小练习与答案

**练习 1**：同样 20 条数据，把 `--lora_r 8 --lora_alpha 16` 改成 `--lora_r 64 --lora_alpha 128`（即不传参时的兜底默认），风险是什么？
**答案**：可训练参数量增大约一个数量级（秩从 8 到 64），在 20 条样本上过拟合风险显著升高，且每个 step 更慢。小数据小任务用小秩即可；`r` 与 `alpha` 同比例放大（保持 \( \alpha/r=2 \)）是常用经验。

**练习 2**：为什么这个脚本用 `zero2.json` 而不是官方 4B 脚本的 `zero3.json`？
**答案**：2B 模型 + LoRA 的可训练状态很小（adapter 参数量仅 MB 级），ZeRO-2 已绰绰有余；ZeRO-3 的参数分片会引入通信与保存时的跨卡聚合开销，单卡场景毫无收益（u5-l8：N=1 时无分摊收益）。

**练习 3**：`--max_pixels 4718592` 对 Qwen3-VL 意味着每张图最多多少视觉 token？对齐取整后实际会不会恰好等于它？
**答案**：\( 4718592 / 1024 = 4608 \) 个视觉 token 上限。实际通常**低于**该值：smart_resize 超上限时按 \( \beta \) 等比缩小并配 floor 取整（u2-l2），所以它是「不超过」的预算而非精确目标。

### 4.3 LoRA 合并与导出

#### 4.3.1 概念说明

上一模块的产物目录里躺着的是 **adapter**：只有低秩矩阵 \( B, A \) 的权重，推理时必须先加载基座再叠加增量。两条使用路线：

| 路线 | 做法 | 适用 |
| --- | --- | --- |
| 直接挂 adapter | PEFT `PeftModel` 或 vLLM 的 LoRA 请求 | 需要一托多 adapter、灵活切换 |
| **合并导出**（本讲采用） | `merge_and_unload()` 把 \( W' = W + \frac{\alpha}{r}BA \) 算死进权重，导出普通完整模型目录 | 单一部署、下游工具（vLLM/评测脚本）零改动 |

闭环选**合并导出**，理由很实际：`run_mmmu.py` 的 `--model-path` 只接受一个自洽的模型目录，合并后基座与 adapter 的差异对评测侧完全透明，before/after 对比才只剩「权重」这一个变量。

合并的数学本质：

\[ h = Wx + \frac{\alpha}{r} B(Ax) \;\xrightarrow{\text{merge}}\; h = \left(W + \frac{\alpha}{r}BA\right)x \]

两个口径一致的细节必须检查：

1. **基座版本一致**：合并用的基座必须与训练时 `--model_name_or_path` 完全同源（同 checkpoint），否则增量叠加在错位的权重上，输出会「看起来正常但答案全错」。
2. **processor 一并保存**：训练脚本已把 processor 存进 output_dir（L202），合并目录里也要有同样的配置文件，vLLM 加载时需要 chat template 与像素配置。

#### 4.3.2 核心流程

```text
① AutoProcessor + 基座模型 from_pretrained（与训练同源）
② PeftModel.from_pretrained(base, adapter_dir)
③ merged = model.merge_and_unload()        # 增量算入权重，返回普通模型
④ merged.save_pretrained(out_dir)          # 完整权重
⑤ processor.save_pretrained(out_dir)       # chat template / 像素配置
⑥ 清点文件清单：config.json / safetensors / preprocessor_config.json ...
```

#### 4.3.3 源码精读

**训练侧到底保存了什么**：

- [qwen-vl-finetune/qwenvl/train/train_qwen.py:L200-L202](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L200-L202) —— `safe_save_model_for_hf_trainer` 保存（DeepSpeed 走 `trainer.save_model` 分支，否则 state_dict 搬 CPU 落盘），随后 `processor.save_pretrained` 单独保存 processor。LoRA 下模型仍被 PEFT 包裹，所以这里落盘的是 adapter 格式（`adapter_model` 权重 + `adapter_config.json`），**不含基座权重**。
- [qwen-vl-finetune/qwenvl/train/train_qwen.py:L52-L64](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L52-L64) —— 保存函数本体：`trainer.deepspeed` 为真时同步 CUDA 后交给 Trainer 统一保存；否则手工把 state_dict 逐张量搬 CPU 再 `_save`。看懂这段就知道 DeepSpeed 与否的产物组织不同——合并脚本从 `output_dir`（而非某个 `checkpoint-*`）读最终 adapter 即可。

**合并脚本本体（仓库未提供，以下为示例代码）**：

```python
# 示例代码：merge_lora.py —— 把 adapter 合并进基座并导出完整模型目录
import torch
from transformers import AutoProcessor, Qwen3VLForConditionalGeneration
from peft import PeftModel

BASE   = "Qwen/Qwen3-VL-2B-Instruct"      # 必须与训练时同源
ADPTR = "./output/my_chart_lora"           # 训练 output_dir（含 adapter 与配置）
OUT    = "./output/merged_2b"              # 末段不含字母 a，避免再训练时被误判 MoE

processor = AutoProcessor.from_pretrained(BASE)
model = Qwen3VLForConditionalGeneration.from_pretrained(
    BASE, dtype=torch.bfloat16,
)
model = PeftModel.from_pretrained(model, ADPTR)
merged = model.merge_and_unload()          # W' = W + (alpha/r)·B·A
merged.save_pretrained(OUT)
processor.save_pretrained(OUT)             # 让导出目录自洽，vLLM 直接可用
```

这一步对应 u5-l7 综合实践里提过的 `merge_and_unload`——本讲把它纳入正式流水线并补齐 processor 落盘。仓库本身没有现成的合并脚本，故标注为示例代码。

#### 4.3.4 代码实践

**实践目标**：导出可被 vLLM 直接加载的合并模型目录，并验证其自洽性。

**操作步骤**：

1. 运行上面的 `merge_lora.py`（示例代码）。
2. 列出三个目录的文件清单做对比：基座缓存目录、`output/my_chart_lora`（adapter）、`output/merged_2b`（合并）。
3. 冒烟验证（示例代码）：

   ```python
   from transformers import AutoProcessor, AutoModelForImageTextToText
   processor = AutoProcessor.from_pretrained("./output/merged_2b")
   model = AutoModelForImageTextToText.from_pretrained("./output/merged_2b", dtype="auto")
   # 用 4.1 里的任意一张图表走一次 u1-l3 的三步推理链
   ```

**需要观察的现象**：adapter 目录里有 `adapter_config.json` 与体量很小的权重文件（MB 级）；合并目录的 `safetensors` 体量与 2B 基座同级（约 4-5 GB，bf16），且含 `preprocessor_config.json`、`chat_template` 相关文件。

**预期结果**：第 3 步能正常出文本。另做一次「基座直出」对照：同一张图同一问题，合并模型与原基座的输出应有可感知差异（这正是微调生效的证据；若完全一样，优先怀疑合并时基座不同源或 adapter 路径指错）。

**待本地验证**：不同硬件上文件体量与生成文本的具体内容。

#### 4.3.5 小练习与答案

**练习 1**：合并导出后想回退到基座，需要「反合并」吗？
**答案**：不需要。合并写进的是新目录，基座原文件未动，把 `--model-path` 指回基座即回退。这也是合并导出比「原地训练」安全的工程优点。

**练习 2**：为什么合并目录命名 `merged_2b` 而不是 `merged_lora_adapter`？
**答案**：`train_qwen.py` 的代际嗅探规则是「路径含 `qwen3` 且末段目录名含字母 `a` → MoE」。`adapter` 含 `a`，若日后用该目录再训练会被误判为 MoE 加载错误类。评测侧 `run_mmmu.py` 不嗅探，但统一规避最省心。

**练习 3**：如果合并时漏了 `processor.save_pretrained(OUT)`，下游最先在哪里炸？
**答案**：评测脚本加载 processor 阶段（如 `run_mmmu.py` 的 `AutoProcessor.from_pretrained(args.model_path)`）会因目录缺 `preprocessor_config.json` 等文件而报错；即便手工补 tokenizer，缺失的像素配置也会让视觉 token 数与训练时不一致，导致质量劣化而不报错——后者更危险。

### 4.4 微调效果评测（评测脚本复用）

#### 4.4.1 概念说明

最后一环是回答「微调到底有没有用」。u6-l2 已经精读了 MMMU 评测的四件套；本讲的增量是学会**裁剪复用**：分清哪些是通用骨架（搬走）、哪些是 MMMU 专属（换掉）。

| 组件 | 处置 | 理由 |
| --- | --- | --- |
| `prepare_inputs_for_vllm` 三件套 | **逐字复用** | 与数据集无关，只依赖 messages + processor |
| infer/eval 两段式 + JSONL 接口 | 复用结构 | GPU 与判分解耦，可独立重跑 |
| TSV / LMUData 下载与图像导出 | 换成自有 JSONL 标注 | 自测集不需要下载逻辑 |
| `build_mmmu_prompt` 的题目拼接 | 换成自测集 prompt 构造 | 但**保留 min/max_pixels 写进 image 元素**的手法 |
| LLM judge 三级瀑布 | 单字母题可降级为纯规则抽取 | 无 API 依赖、可复现 |

评测公平性三原则：

1. **同一输入**：before/after 跑同一份测试 messages（JSONL 里的 `messages` 字段原样落盘正是为此留的审计线索）。
2. **同一采样参数**：`SamplingParams` 完全一致（建议 `temperature=0` 贪心，消除采样随机性；vLLM 侧另有 `seed=42`）。
3. **同一样本对齐**：按 `question_id` 逐条配对比较，而不是只看两个总数。

还要正视统计现实：自测集通常只有几十条，**一条样本就是 5 个百分点**（20 条时）。所以报告里必须给配对明细（哪些题从错变对 / 从对变错），而不是只报一个准确率。

#### 4.4.2 核心流程

```text
my_chart_test.json（20 条带真值）
        │  构造 messages（含像素预算）+ prepare_inputs_for_vllm
        ▼
   infer：LLM(args.model_path) → llm.generate 批量推理
        │  逐条写 JSONL：{question_id, annotation, result:{gen,gen_raw}, messages}
        ▼
   JSONL 文件（before.jsonl / after.jsonl，各跑一遍）
        │  抽取选项字母 / 数值 → 与 GT 比对
        ▼
   eval：逐条 hit → 总准确率 + 配对差异表
```

与 u6-l1 的通用数据流同构，只是数据源从 TSV/LMUData 换成了自有 JSONL。

#### 4.4.3 源码精读

**可逐字复用的输入准备函数**：

- [evaluation/mmmu/run_mmmu.py:L76-L107](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L76-L107) —— `prepare_inputs_for_vllm`：L87 用 `apply_chat_template(tokenize=False)` 渲染文本；L90-L95 调 `process_vision_info`，注意三个关键实参——`image_patch_size=processor.image_processor.patch_size`（动态读取而非硬编码 16，u2-l1 的最佳实践）、`return_video_kwargs=True`、`return_video_metadata=True`；L97-L101 把图像/视频装进 `multi_modal_data`；L103-L107 返回三字段字典。这一函数与数据集完全解耦，**你的自测集脚本应原封不动抄走它**。

**prompt 构造中值得保留的手法**：

- [evaluation/mmmu/run_mmmu.py:L27-L47](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L27-L47) —— `build_mmmu_prompt` 前半段：`MIN_PIXELS/MAX_PIXELS` 是 28 口径遗产（L30-L31），但它演示了「按数据集定预算」的思想；题目拼接以 `Please select the correct answer from the options above.` 收口（L46），把输出格式钉死在单字母上——**自测集 prompt 也应这样把答案格式钉死**，评测抽取才有据可依。
- [evaluation/mmmu/run_mmmu.py:L50-L66](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L50-L66) —— 视觉元素与文本元素按「先图后文」的顺序组装进 content，`min_pixels/max_pixels` 直接写在 image 元素里（元素级预算，u2-l2）。自测图表题可把这里换成 32 口径的更高预算。

**推理主循环与落盘**：

- [evaluation/mmmu/run_mmmu.py:L189-L212](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L189-L212) —— 先在 CPU 侧全量构建输入（prompt 构造 → `prepare_inputs_for_vllm` → 追加 `line_dict` 与 `messages` 存档），把「准备」与「生成」两个阶段分开，便于前者随意调试而不用反复初始化引擎。
- [evaluation/mmmu/run_mmmu.py:L222](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L222) —— 单次 `llm.generate(all_inputs, sampling_params=...)` 批量推理，vLLM 内部自动做连续批处理。
- [evaluation/mmmu/run_mmmu.py:L235-L246](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L235-L246) —— L235 取 `output.outputs[0].text`（vLLM 输出只含新生成，无需裁剪前缀，u3-l2）；L238 `str(response).split("</think>")[-1].strip()` 兼容 Thinking 档剥推理链；L240-L246 的结果 schema——`question_id`（配对键）、`annotation`（真值随行携带）、`result: {gen, gen_raw}`（处理后与原始输出都留）、`messages`（输入审计）——**自测集脚本照抄这个 schema**，你未来的任何评测都能读它。
- [evaluation/mmmu/run_mmmu.py:L250-L252](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L250-L252) —— JSONL 逐行写盘，这就是 infer 与 eval 之间的唯一接口。

**判分侧要复用的骨架**：

- [evaluation/mmmu/run_mmmu.py:L260-L270](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L260-L270) —— eval 读回 JSONL：把 `job["result"]["gen"]` 写回 `annotation["prediction"]`，再 `pd.DataFrame.from_records`。自测集脚本同构。
- [evaluation/mmmu/run_mmmu.py:L334-L348](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L334-L348) —— 准确率汇总：总体 = `hit` 均值；按 split 分组再算一层。自测集可把 split 换成题目类型（读数 / 比较），观察微调对哪类问题增益最大。
- [evaluation/mmmu/run_mmmu.py:L351-L359](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L351-L359) —— 逐条结果落 CSV + 汇总指标落 `_acc.json`，两个文件分别支撑定性抽查与定量对比。

**子命令与采样参数**：

- [evaluation/mmmu/run_mmmu.py:L372-L402](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L372-L402) —— infer 子命令参数：`--model-path`（你的 before/after 各跑一次）、`--output-file`、`--temperature`（默认 0.7）等。**对比实验建议显式传 `--temperature 0`**，让两侧都用贪心解码。
- [evaluation/mmmu/run_mmmu.py:L405-L414](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L405-L414) —— eval 子命令参数：`--input-file` / `--output-file` / `--eval-model`（judge 模型）。自测集若用纯规则抽取，judge 相关参数整体省去。

**推理侧常见报错速查表**：

| 症状 | 原因 | 处置 |
| --- | --- | --- |
| 加载 merged 目录即报缺文件 | 合并时漏存 processor | 补 `processor.save_pretrained` 重导 |
| `KeyError: 'LMUData'` | 直接跑 mmmu 脚本未给 `--data-dir` | 自测集脚本不用 LMUData；跑 mmmu 则传 `--data-dir` |
| 视觉 token 数与训练不符 | 评测侧像素预算与训练不一致 | 训练与评测用同一档预算，统一 32 口径 |
| 输出复读 / 空转 | Instruct 档缺存在惩罚 | 参照 mmmu 默认 `presence_penalty=1.5`，before/after 保持一致 |
| before/after 输出一模一样 | 合并基座不同源 / 模型路径指错 | 核对 `--model-path` 与合并脚本 |

#### 4.4.4 代码实践

**实践目标**：写一个两段式自测脚本，对基座与合并模型各跑一遍，输出配对差异。

**操作步骤**：新建 `evaluation/my_chart/run_myset.py`（示例代码，骨架复用自 run_mmmu.py，仅保留必要部分）：

```python
# 示例代码：run_myset.py —— 自测集两段式评测（infer / eval）
import os, json, argparse, torch
from vllm import LLM, SamplingParams
from qwen_vl_utils import process_vision_info
from transformers import AutoProcessor

MIN_PIXELS, MAX_PIXELS = 262144, 4718592      # 32 口径：256~4608 视觉 token

def prepare_inputs_for_vllm(messages, processor):     # ← 逐字复用 run_mmmu.py L76-L107
    text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    image_inputs, video_inputs, video_kwargs = process_vision_info(
        messages, image_patch_size=processor.image_processor.patch_size,
        return_video_kwargs=True, return_video_metadata=True)
    mm_data = {}
    if image_inputs is not None: mm_data['image'] = image_inputs
    if video_inputs is not None: mm_data['video'] = video_inputs
    return {'prompt': text, 'multi_modal_data': mm_data, 'mm_processor_kwargs': video_kwargs}

def build_prompt(item):                               # ← 自测集专属 prompt
    content = [{"type": "image", "image": item["image"],
                "min_pixels": MIN_PIXELS, "max_pixels": MAX_PIXELS},
               {"type": "text", "text":
                f"{item['question']}\nAnswer with the option letter only."}]
    return [{"role": "user", "content": content}]

def run_inference(args):
    data = [json.loads(l) for l in open(args.test_file)]       # 自有 JSONL 标注
    processor = AutoProcessor.from_pretrained(args.model_path)
    llm = LLM(model=args.model_path, tensor_parallel_size=1,
              gpu_memory_utilization=0.85, max_model_len=8192, seed=42)
    sp = SamplingParams(temperature=0, max_tokens=64)          # 贪心，短答案
    inputs = [prepare_inputs_for_vllm(build_prompt(it), processor) for it in data]
    outputs = llm.generate(inputs, sampling_params=sp)
    with open(args.output_file, 'w') as f:                     # 复用 MMMU 结果 schema
        for it, out in zip(data, outputs):
            gen = out.outputs[0].text.split("</think>")[-1].strip()
            f.write(json.dumps({"question_id": it["question_id"],
                                "annotation": it, "result": {"gen": gen}}) + '\n')

def run_eval(args):
    rows = [json.loads(l) for l in open(args.input_file)]
    hits = []
    for r in rows:                                     # 规则抽取：首个选项字母
        pred = next((ch for ch in r["result"]["gen"] if ch in "ABCD"), None)
        hits.append(pred == r["annotation"]["answer"])
    acc = sum(hits) / len(hits)
    print(f"accuracy: {acc:.4f} ({sum(hits)}/{len(hits)})")
    json.dump({"accuracy": acc}, open(args.output_file, 'w'), indent=2)

if __name__ == "__main__":
    p = argparse.ArgumentParser(); sub = p.add_subparsers(dest='cmd')
    i = sub.add_parser('infer'); i.add_argument('--model-path', required=True)
    i.add_argument('--test-file', required=True); i.add_argument('--output-file', required=True)
    e = sub.add_parser('eval'); e.add_argument('--input-file', required=True)
    e.add_argument('--output-file', required=True)
    a = p.parse_args()
    run_inference(a) if a.cmd == 'infer' else run_eval(a)
```

对基座与合并目录各执行一次 infer，再对两个 JSONL 各执行一次 eval：

```bash
# 示例命令
python run_myset.py infer --model-path Qwen/Qwen3-VL-2B-Instruct \
    --test-file my_chart_test.jsonl --output-file before.jsonl
python run_myset.py infer --model-path ./output/merged_2b \
    --test-file my_chart_test.jsonl --output-file after.jsonl
python run_myset.py eval --input-file before.jsonl --output-file before_acc.json
python run_myset.py eval --input-file after.jsonl  --output-file after_acc.json
```

**需要观察的现象**：infer 阶段两个模型跑的是完全相同的 inputs（可在脚本里断言 `messages` 逐条相等）；eval 输出的两行准确率；抽查 3 条「从错变对」与（若有）「从对变错」的样本原文。

**预期结果**：合并模型在图表读数类问题上准确率不低于基座；若出现下降，优先检查过拟合（训练轮数减半重训）与两侧像素预算是否一致。

**待本地验证**：20 条自测集上的具体准确率数值。

#### 4.4.5 小练习与答案

**练习 1**：为什么自测集脚本里把 judge 模型整个删掉了，而 mmmu 还要保留？
**答案**：MMMU 的模型输出是自然语言句子，需要判断「句子里隐含哪个选项」，规则抽取覆盖不了所有表述，所以要 LLM judge 兜底（三级瀑布）。自测集 prompt 已把答案钉死为单个字母（`Answer with the option letter only.`），首字母抽取即可覆盖，规则一层就够，删掉 judge 还消除了 API 依赖与不确定性，可复现性更好。

**练习 2**：before 准确率 55%、after 准确率 70%（20 条），能直接宣布微调成功吗？
**答案**：不能只看总数。20 条下 3 条的差异（15 个百分点）完全可能是噪声；必须看配对明细——多少条从错变对、多少条从对变错、变对的题是否恰好是训练分布内的题型。若增益集中在「与训练集高度同构」的题上而其余持平，说明学到的是任务格式而非泛化能力；想下结论需要扩测试集或多次重复实验。

**练习 3**：如果自测题目改成开放式数值问答（如「2023 年营收是多少」），评测脚本的哪一部分必须重写？
**答案**：判分侧的抽取与比对——单字母规则抽取换成数值解析（如正则抽数字后做等值/容差比较），结果 schema 里 `annotation` 需带数值真值；infer 侧的 `build_prompt` 也要把收口语从「选项字母」改成「只回答数值」。`prepare_inputs_for_vllm` 与两段式结构仍然不动。

## 5. 综合实践

**任务**：完整闭环——为「图表问答」制作数据、微调、合并、评测、写报告。以下是把四个模块串起来的总控清单（全部命令需本地具备 GPU 环境后方可运行，本讲义不假设已执行）。

1. **备料（模块 4.1）**
   - 训练集：20 条图表问答（读数 10 条 + 比较 10 条），过 `validate_data` 得 `my_chart_valid.json` 并注册为 `my_chart`。
   - 测试集：另做 **20 条不与训练集重复**的样本，写成 `my_chart_test.jsonl`（含 `question_id`、`image`、`question`、`answer` 四字段）。训练与测试必须分离，否则测的是记忆不是能力。
2. **基线（模块 4.4）**：先用基座跑 `run_myset.py infer`，落盘 `before.jsonl`——**先测基线再训练**，避免环境变化污染对照。
3. **训练（模块 4.2）**：跑 `sft_my_chart_2b_lora.sh`（先 0.01 轮冒烟再正式 5 轮），记录 loss 曲线与耗时。
4. **合并（模块 4.3）**：`merge_lora.py` 导出 `merged_2b`，清点文件清单并做 transformers 冒烟。
5. **对照（模块 4.4）**：合并模型跑 `infer` 得 `after.jsonl`；两侧 eval 出准确率与配对差异。
6. **实验报告**：按下面模板写 `report.md`（放在自己的目录，不放讲义目录）：

```text
# 图表问答 LoRA 微调实验报告
1. 任务定义与数据：20 训练 + 20 测试，题型构成，示例各 1 条
2. 训练配置：模型/LoRA 参数/像素预算/有效批量/lr/轮数（贴脚本关键行）
3. 训练过程：loss 曲线描述、耗时、异常记录
4. 评测设置：采样参数、评测脚本的复用与裁剪点
5. 结果：before/after 总准确率 + 按题型分解 + 配对差异明细（错→对 / 对→错 各列 3 条原文）
6. 结论与归因：增益来自哪类题；是否过拟合；下一步（扩数据/调预算/换秩）
7. 复现清单：数据文件、两个模型目录、四条命令
```

**验收标准**：报告第 5 节能回答「微调在哪些题上有效、为什么」；第 7 节任何人按清单可复现。

## 6. 本讲小结

- 闭环五环——**制作 → 注册 → 训练 → 合并 → 评测**——每一环都有仓库内的真实挂点：标注格式抄 `single_images.json`、注册改 `data_dict`、训练套官方 shell 模板、合并用 `merge_and_unload` 补齐 processor、评测逐字复用 `prepare_inputs_for_vllm` 与 JSONL schema。
- LoRA 是小数据微调的正确起点：单卡可跑、三开关失效带来的复杂度归零；产物是 adapter，合并导出后才成为 vLLM 可直接加载的自洽目录。
- 训练侧三大隐形雷区：模型目录末段含字母 `a` 被误判 MoE、output_dir 残留 `checkpoint-*` 触发续训、28 口径像素遗产导致视觉 token 预算算错。
- 评测公平性三原则：同一输入、同一采样参数（建议贪心）、按 `question_id` 配对比较；小样本下配对明细比总准确率更有信息量。
- 评测脚本的复用判断标准是「与数据集是否耦合」：三件套输入准备与两段式 JSONL 接口是通用骨架，数据下载与 judge 判分按需裁剪。

## 7. 下一步学习建议

本手册至此收官。沿着本讲闭环向外延伸的三条路：

1. **扩数据上强度**：把 20 条换成真实规模的图表数据集（可参考 u4-l4 的 `pack_data.py` 打包提效），体验 `data_packing=True` 与 varlen 注意力在大批量下的收益；此时应重读 u5-l5/u5-l6 巩固拼接训练的注意力隔离机制。
2. **接回公开基准**：用本讲导出的合并模型跑一遍完整 MMMU（u6-l2 的三个坑：MD5 校验、`--data-dir`、subject 子集在 JSONL 层过滤），确认微调没有「按下葫芦浮起瓢」地损伤通用能力——这是上线前的必要回归。
3. **纵深到模型内部**：本手册始终把模型当黑盒；想打开它，从 `rope2d.py`（u5-l4）回溯到 transformers 里的 `Qwen3VLForConditionalGeneration` 实现，结合 Interleaved-MRoPE 论文细节，理解位置编码与 DeepStack 如何在代码里落地——那是从「会用」走向「能改」的分水岭。
