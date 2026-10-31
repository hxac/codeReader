# u6-l4 ODinW-13 开放域检测评测与 bbox 解析

## 1. 本讲目标

前两讲（u6-l2 MMMU、u6-l3 VideoMME）评测的都是「选择题」：模型输出一段文字，判分侧抽出选项字母，算准确率。本讲进入评测体系中**性质完全不同**的一类任务——**开放域目标检测**（Object Detection in the Wild, ODinW-13）：

- 模型输出不再是选项字母，而是一串 JSON 格式的检测框列表；
- 判分不再是「规则抽取 → LLM judge → 随机保底」三级瀑布，而是**几何计算**：框与框的交并比（IoU）、COCO 风格的 mAP 指标族；
- 多出一个前两讲都没有的环节——**坐标归一化**：模型的 bbox 是画在「送进模型的缩放图」上的，而真值标注在原图上，两者必须先换算到同一坐标系才能比较。

学完本讲，你应当能够：

1. 读懂检测任务 prompt 的构造方式（类别清单注入 + JSON 输出约定）；
2. 独立复述 bbox JSON 的解析规则与两条坐标归一化分支的数学含义；
3. 说清 COCO mAP / mAP_50 / mAP_75 / mAP_s / mAP_m / mAP_l 各自衡量什么；
4. 横向对比五个评测目录的结构异同，理解「为什么 ODinW-13 没有 common_utils.py、没有 LLM judge」。

## 2. 前置知识

### 2.1 ODinW-13 是什么

ODinW（Object Detection in the Wild）是把 13 个来源各异的小型检测数据集（无人机航拍、水族馆、兔子、手、蘑菇、包裹、Pascal VOC、手枪、路面坑洞、浣熊、贝类、热成像、车辆）拼在一起的基准，用于考察检测模型**跨域泛化**能力。每个子数据集都是 COCO 标注格式，类别数从 1 到几十不等。与 MMMU 不同，它不托管在 HF/LMU 的 TSV 里——**用户需要自备数据目录**，目录里必须有一个 `odinw13_config.py` 配置文件（该文件随数据集发布，不在本仓库中，用 Glob 全仓搜索可确认）。

### 2.2 COCO 标注格式的最小知识

COCO 格式用 JSON 描述三类对象：

- `images`：每张图一条记录，含 `id`、`file_name`、`width`、`height`；
- `annotations`：每个真值框一条记录，含 `image_id`、`category_id`、`bbox`（**xywh** 格式：左上角坐标 + 宽高）、`area`、`id`；
- `categories`：类别表，含 `id` 与 `name`。

`pycocotools.coco.COCO` 类加载后提供按图取标注（`imgToAnns`）、取类别 id（`getCatIds`）等索引，本讲源码大量使用。

### 2.3 IoU 与 mAP

两个框 A、B 的交并比（Intersection over Union）：

\[ \mathrm{IoU}(A, B) = \frac{|A \cap B|}{|A \cup B|} \]

取值 0 到 1，越接近 1 两框重叠越好。检测判分的规则是：**预测框与某个同类真值框的 IoU 超过阈值 τ，且类别一致，才算命中**。

COCO 的主指标 mAP 是在 10 个 IoU 阈值（0.50、0.55、…、0.95）上分别画出每类的精确率-召回率（P-R）曲线、取曲线下面积得到 AP，再对类别取平均；mAP_50 只用 τ=0.5（宽松档），mAP_75 只用 τ=0.75（严格档）。mAP_s / mAP_m / mAP_l 按真值框面积分桶：面积 < 32² 像素为小物体，32² ~ 96² 为中物体，> 96² 为大物体。直觉上：**mAP_50 衡量「找得到、类别对」，mAP_75 衡量「框得准」**，mAP 是两者的折中综合。

### 2.4 两种坐标口径（本讲的关键背景）

Qwen-VL 各代模型的 grounding 输出共用同一 JSON schema（`{"bbox_2d": [x1, y1, x2, y2], "label": "..."}`），但**坐标口径**随代际演变：

| 模型代际 | 坐标口径 |
| --- | --- |
| Qwen2-VL | 0-1000 相对坐标（相对原图宽高归一化到 0~1000） |
| Qwen2.5-VL | 绝对坐标（画在送入模型的**缩放后**图像上） |
| Qwen3-VL | 默认改回 0-1000 相对坐标 |

Qwen3-VL 的口径声明见官方 grounding cookbook：[cookbooks/2d_grounding.ipynb 第 13 行](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L13) 写明「Qwen3-VL 的默认坐标系已从 Qwen2.5-VL 的绝对坐标改为 0 到 1000 的相对坐标（无需计算 resized_w）」，且其配套换算代码 [cookbooks/2d_grounding.ipynb:189-192](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/cookbooks/2d_grounding.ipynb#L189-L192) 正是 `bbox / 1000 * 原图宽高`。记住这个背景，4.3 节会看到评测脚本为此准备了两条换算分支。

### 2.5 与前置讲义的衔接

- u6-l1 建立的「run / dataset_utils / eval_utils 三件套」架构与 `prepare_inputs_for_vllm` 三件套输入模式（prompt / multi_modal_data / mm_processor_kwargs），本讲不再重复推导，直接复用；
- u2-l2 精读过的 `smart_resize` 像素预算函数，本讲会看到它在 dataset_utils.py 里的一份**本地副本**；
- u4-l2 讲过的微调 grounding 标注（`bbox_2d` JSON）与本讲评测解析的输出 schema 是同一套，训练与评测在此闭环。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [evaluation/ODinW-13/run_odinw.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/run_odinw.py) | 主脚本：`infer` / `eval` 两个子命令。推理侧组装任务、调 vLLM 批量生成并落盘 JSONL；评测侧负责 bbox 解析、坐标归一化、按数据集汇总 |
| [evaluation/ODinW-13/dataset_utils.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/dataset_utils.py) | 数据侧：加载 `odinw13_config.py`、遍历 COCO 标注生成检测任务（prompt + messages）、计算每张图的缩放尺寸 |
| [evaluation/ODinW-13/eval_utils.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/eval_utils.py) | 判分侧：xyxy→xywh 转换、预测结果转 COCO 结果 JSON、封装 COCOeval 计算 mAP 指标族 |
| [evaluation/ODinW-13/infer_instruct.sh](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/infer_instruct.sh) / [infer_think.sh](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/infer_think.sh) | Instruct / Thinking 两档官方采样参数 |
| [evaluation/ODinW-13/eval_instruct.sh](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/eval_instruct.sh) / [eval_think.sh](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/eval_think.sh) | 判分入口（无 GPU 依赖） |
| [evaluation/ODinW-13/README.md](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/README.md) | 数据目录结构、参数说明、输出格式与排障 |

注意一个结构事实：ODinW-13 目录**没有** `common_utils.py`（mmmu / MathVision / RealWorldQA 都有）——因为它不下载 TSV、不处理 base64 图像，数据完全来自本地 COCO 文件。

## 4. 核心概念与源码讲解

本讲的四个最小模块：**① 检测任务 prompt 与任务生成 → ② bbox 解析与坐标归一化 → ③ 检测指标计算 → ④ 同构目录横向对比**。模块 ①②③ 恰好对应数据从「COCO 标注 → prompt → 模型输出 → 换算回原图 → mAP」的一次完整旅行。

### 4.1 检测任务 prompt 与任务生成

#### 4.1.1 概念说明

与 MMMU「每题一个选择题」不同，ODinW 的一个「任务」= **一张图**。对每张图，脚本把该子数据集的**全部类别名**拼进一句固定指令，要求模型找出所有属于这些类别的实例并以 JSON 报告 bbox。所以 ODinW 的 prompt 是**模板 + 动态类别清单**生成的，随子数据集变化。

这里有个隐藏设计：prompt 只约定了「JSON 格式」，**没有显式约定坐标口径**（0-1000 还是绝对像素），模型按自身训练时的默认口径输出。这为 4.3 节的两条换算分支埋下伏笔。

#### 4.1.2 核心流程

`generate_odinw_jobs(data_dir, args)` 的执行过程：

1. 拼接 `data_dir/odinw13_config.py`，用 `runpy` 执行该配置文件，取出 `datasets`（配置列表）与 `dataset_prefixes`（数据集名列表），按位置配对成字典；
2. 按公式 `pixels_per_token = patch_size² × merge_base²` 计算像素预算（16²×2²=1024 像素/token），得 `min_pixels = 768 token`、`max_pixels = 12800 token` 对应的像素数；
3. 对每个子数据集：把配置里的 `data_root`（形如 `data/odinw/xxx`）**重定基**到本地 `--data-dir`，拼出标注 JSON 与图片目录路径，用 `COCO()` 建索引；
4. 对每张图：读取宽高 → 用 `smart_resize(factor=32, min/max_pixels)` 算出**送入模型时的缩放尺寸** `resized_h/resized_w` → 取该图真值标注 → 拼 prompt 与 messages → 打包成带 `extra_info` 的任务条目；
5. 汇总所有任务，打印每个子数据集的题目数。

伪代码：

```text
for 子数据集 in 配置:
    类别表 = 子数据集.metainfo.classes
    for 图 in COCO(标注文件).imgs:
        resized_h, resized_w = smart_resize(原图宽高, factor=32, 像素预算)
        prompt = "Locate every instance ... categories: '{类别名逗号列表}' ... JSON format."
        messages = [ {image 元素(带 min/max_pixels), text 元素(prompt)} ]
        任务列表.append({question_id, annotation=真值, messages, extra_info={缩放尺寸, 原图尺寸, 路径...}})
```

#### 4.1.3 源码精读

**像素预算：从第一性原理算出 32 口径**。[evaluation/ODinW-13/dataset_utils.py:111-116](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/dataset_utils.py#L111-L116)：这段代码先设 `patch_size=16`、`merge_base=2`，则每视觉 token 对应 \(16 \times 16 \times 2 \times 2 = 1024\) 像素（对应 u2-l1 讲过的 patch 边长 16 + 2×2 空间合并，即 32 口径）；随后 `min_pixels = 1024×768`、`max_pixels = 1024×12800`。对照 u6-l2：MMMU 用的还是 28 口径遗产常数、每图约 980~3920 视觉 token；ODinW 这里直接按 Qwen3-VL 的 32 口径从公式算出，预算上限高达 **12800 token/图**——检测要框准小物体，分辨率预算给得远比 VQA 阔绰。

**路径重定基**。[evaluation/ODinW-13/dataset_utils.py:124-127](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/dataset_utils.py#L124-L127)：配置文件里的 `data_root` 含 `data/odinw/` 前缀（数据集原始发布路径），代码用 `find('data/odinw/') + len('data/odinw/')` 切掉前缀，再拼到命令行传入的 `data_dir` 下，从而把任意位置的本地数据目录接到配置上。

**为坐标换算预存缩放尺寸**。[evaluation/ODinW-13/dataset_utils.py:140-147](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/dataset_utils.py#L140-L147)：对每张图调用本文件的 `smart_resize(img_h, img_w, factor=32, min_pixels, max_pixels, max_long_side=50000)`。注意这是 smart_resize 的**本地副本**（[L25-L66](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/dataset_utils.py#L25-L66)，函数签名与 qwen-vl-utils 2.x 版本略有出入：副本带 `max_long_side` 参数、默认 `factor=28` 是旧口径遗产，这里显式传 32 并把长边上限放到 50000 使其基本不生效）。算出的 `resized_h/resized_w` 被存进 `extra_info`，用途在 4.3 节揭晓——它是坐标归一化的分母。

**检测 prompt 模板**。[evaluation/ODinW-13/dataset_utils.py:150-156](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/dataset_utils.py#L150-L156)：`obj_names = ", ".join(classes)` 把类别表串成逗号清单，套进模板 `Locate every instance that belongs to the following categories: '{obj_names}'. Report bbox coordinates in JSON format.`——一句指令同时完成三件事：给定类别闭集（开放域检测退化为「在这些类里找」）、要求全实例（every instance）、约定 JSON 输出。

**messages 与任务条目**。[evaluation/ODinW-13/dataset_utils.py:159-190](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/dataset_utils.py#L159-L190)：messages 的 image 元素用 `file://` 协议指向本地图片，且**把 min_pixels/max_pixels 写进元素本身**（L165-167）——这是 u2-l2 讲过的元素级预算通道，保证 qwen-vl-utils 的 `fetch_image` 在推理时按同样的预算缩放。`extra_info`（L179-189）打包了后续评测需要的全部上下文：`dataset_name`（分组键）、`img_id`（对回 COCO 真值）、`resized_h/w`（坐标换算分母）、`img_h/w`（原图尺寸）、`anno_path`（真值文件）、`img_path`。

**推理侧：完全复用 u6-l1 的标准链路**。[evaluation/ODinW-13/run_odinw.py:107-121](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/run_odinw.py#L107-L121) 先在 CPU 侧对全部任务逐条执行 `prepare_inputs_for_vllm`（该函数定义在 [run_odinw.py:28-50](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/run_odinw.py#L28-L50)，与 mmmu/VideoMME/MathVision/RealWorldQA 五份 run 脚本逐字同构：`apply_chat_template(tokenize=False)` + `process_vision_info(image_patch_size=..., return_video_metadata=True)` + 三字段字典），再一次 `llm.generate` 批量推理。结果落盘见 [run_odinw.py:133-151](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/run_odinw.py#L133-L151)：每行一个 JSON，含 `question_id`、`annotation`（整张图的真值框，仅作记录）、`extra_info`、`result.gen`（剥掉 `</think>` 后的回答，L137）与 `result.gen_raw`（原始输出），以及完整 `messages`。JSONL 是 infer 与 eval 两阶段的唯一接口——这是三件套模式的固定打法（u6-l1）。

一个值得知道的细节：[run_odinw.py:157-169](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/run_odinw.py#L157-L169) 把数据集配置序列化存成 `*_datasets.json`，但后文 `run_evaluation` 并不读取它，而是从 `--data-dir` 重新加载 `odinw13_config.py`（见 4.3 节）——该文件目前只是一份「落盘但未被消费」的副产物。

#### 4.1.4 代码实践

**实践目标**：不依赖 GPU，验证「类别清单 → prompt」的拼接逻辑与像素预算公式的正确性。

**操作步骤**（示例代码）：

```python
# practice_41_prompt.py —— 示例代码：复现 prompt 拼接与像素预算计算
classes = ["immediate environment", "person", "boat"]
obj_names = ", ".join(classes)
prompt = (f"Locate every instance that belongs to the following categories: "
          f"'{obj_names}'. Report bbox coordinates in JSON format.")
print(prompt)

patch_size, merge_base = 16, 2
pixels_per_token = patch_size * patch_size * merge_base * merge_base
print("pixels_per_token =", pixels_per_token)
print("min_pixels (768 token) =", pixels_per_token * 768)
print("max_pixels (12800 token) =", pixels_per_token * 12800)
```

**需要观察的现象**：打印出的 prompt 中类别清单被单引号包裹、以逗号分隔；三个像素数值分别是 1024、786432、13107200。

**预期结果**：`max_pixels/min_pixels = 12800/768 ≈ 16.7`，即检测任务允许同一张图在 768~12800 个视觉 token 之间浮动——对比 u6-l2 中 MMMU 每图约 980~3920 token，检测预算高出数倍。若你的输出与上述数值不符，先检查是否误用了 28 口径（`14×14×2×2=784`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 ODinW 的 prompt 要把全部类别名列出来，而不是让模型「检测图中所有物体」？
**答案**：因为评测必须与 COCO 真值类别表对齐。mAP 按类别计算，类别闭集使模型输出的 `label` 字符串能映射回类别索引（4.3 节的 `class_names.index`）；开放词汇检测会产生真值表中不存在的类别，无法计入任何 AP 分子。

**练习 2**：`generate_odinw_jobs` 里 `resized_h/resized_w` 为什么要在这里（数据准备阶段）就算好，而不是评测时再算？
**答案**：坐标换算需要「模型实际看到的图像尺寸」作分母。这个尺寸由推理时的 `min_pixels/max_pixels` 元素预算 + smart_resize 共同决定；在任务生成阶段用同一套参数预计算并随 JSONL 持久化，保证 infer 与 eval 分离运行时（甚至换机器）换算仍自洽，无需在评测侧重新推断缩放参数。

**练习 3**：本地副本 `smart_resize` 的默认参数 `factor=28` 与调用处 `factor=32` 不一致，这会在什么情况下出问题？
**答案**：只有「调用时忘记显式传 factor」才会踩坑——显式传 32 时默认值不生效。这属于 u5-l3 讲过的「28 口径遗产」现象：代码从旧项目复制而来，默认值是 Qwen2/2.5-VL 时代残留下来的，实际行为以调用点为准。

### 4.2 bbox 解析与坐标归一化

#### 4.2.1 概念说明

模型对一张图的典型输出长这样（README 示例）：

```json
[{"bbox_2d": [x1, y1, x2, y2], "label": "boat"}, {"bbox_2d": [x1, y1, x2, y2], "label": "boat"}]
```

评测要把它变成「原图坐标系下的 (bbox, 类别索引) 对」。两件事必须做对：

1. **文本解析**：模型输出可能裹着 ```` ```json ```` 围栏、用单引号、混入长度异常的框——需要容错解析；
2. **坐标归一化**：模型的 bbox 画在它「看到」的坐标系里（缩放图绝对坐标或 0-1000 相对坐标），真值在原图上。必须先归一到 0-1 再放大到原图，否则框会整体偏移或比例失调。

这一模块是全脚本中与「选择题评测」差异最大的部分，也是最容易因坐标系搞错而白跑一次推理的部分。

#### 4.2.2 核心流程

`run_evaluation` 中每个子数据集的处理流程：

1. 从 JSONL 读回全部推理结果，按 `extra_info.dataset_name` 分组；
2. 对每个子数据集：从 `anno_path` 重新加载 `COCO()` 真值索引、取类别表 `classes`；
3. 对每条结果：剥 markdown 围栏 → `ast.literal_eval` 解析 → 逐条校验 `bbox_2d` 长度为 4 → 收集 (bbox, label)；
4. **坐标换算**（二选一，由环境变量 `is_rel` 决定，默认 `"0"`）；
5. 按类别索引重组：label 小写化后若不在类别表（同样小写化）中则**静默丢弃**，否则映射为索引；
6. 同一张图的预测按 `(图元信息, {bboxes, scores, labels})` 元组组装，交给 4.3 节的指标计算。

坐标换算的数学。设模型输出框坐标 \( p \)，原图宽高 \( (W, H) \)，模型实际看到的缩放图宽高 \( (W_r, H_r) \)：

分支一（默认，`is_rel=0`，**缩放图绝对坐标**，Qwen2.5-VL 口径）：

\[ x_{\text{orig}} = \frac{p_x}{W_r} \cdot W, \qquad y_{\text{orig}} = \frac{p_y}{H_r} \cdot H \]

分支二（`is_rel=1`，**0-1000 相对坐标**，Qwen2-VL / Qwen3-VL cookbook 口径）：

\[ x_{\text{orig}} = \frac{p_x}{1000} \cdot W, \qquad y_{\text{orig}} = \frac{p_y}{1000} \cdot H \]

两条分支对 x2/y2 用同样的分母（宽用 \( W_r \)（或 1000），高用 \( H_r \)（或 1000）），四个分量按 `[w, h, w, h]` 模式广播。若选错分支，误差是系统性的：比如实际是 0-1000 相对坐标却按分支一换算，当缩放图宽高比与原图一致时误差为 \( 1000/W_r \) 倍的均匀缩放——框的形状对、位置和大小全错，mAP_50 也会接近 0。

#### 4.2.3 源码精读

**分组与真值索引**。[evaluation/ODinW-13/run_odinw.py:190-206](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/run_odinw.py#L190-L206)：`all_outputs` 以 `dataset_name` 为键把任务分组；随后从该组第一条的 `extra_info["anno_path"]` 取路径重建 `COCO()`（真值不依赖 JSONL 里内嵌的 `annotation` 字段），并从重新加载的配置中取 `classes` 类别表。注意这里与 4.1 节结尾呼应：类别表来自 `odinw13_config.py` 而非 `*_datasets.json`。

**剥围栏 + 容错解析**。[evaluation/ODinW-13/run_odinw.py:216-236](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/run_odinw.py#L216-L236)：先 `answer.replace("```json", "")`、`replace("```", "")` 去掉 markdown 代码围栏，再用 `ast.literal_eval`（而非 `json.loads`）解析——模型常输出单引号风格的「JSON」，`literal_eval` 对单/双引号都接受，这是比 `json.loads` 更宽容的选择（代价是遇到 `true/false/null` 关键字会失败，不过 bbox/label 场景基本不会出现）。解析循环里两道校验：`len(data.get("bbox_2d", [])) != 4` 的条目被逐条跳过；而 `data["label"]` 用下标直接取。**注意异常粒度**：try 块包住了整张图的解析循环，任何一条记录缺 `label` 键抛 KeyError，整张图的全部预测都会被 except 清空（L233-236 兜底为空列表），等价于这张图「零预测」——召回率受损但不崩溃。顺带一提，L222 的 `import re` 从未被使用，是个无害的残留。

**两条坐标换算分支**。[evaluation/ODinW-13/run_odinw.py:238-247](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/run_odinw.py#L238-L247)：开关是环境变量 `is_rel`（`os.getenv("is_rel", "0") == "1"` 才走相对分支），README 未记载该变量，属于隐藏开关。`is_rel=1` 分支（L240）直接 `/1000 * [img_w, img_h, img_w, img_h]`，与 2.3 节 cookbook 的换算完全一致；默认分支（L241-245）先除以 `[resized_w, resized_h, resized_w, resized_h]`（归一到 0-1）再乘原图宽高，且仅在 `len(pred_bboxes) > 0` 时才做除法（空预测直接 reshape 为空数组，避免除零）。`reshape(-1, 4)` 把列表规整为 (N,4) 矩阵便于向量化广播。

**类别名归一与映射**。[evaluation/ODinW-13/run_odinw.py:249-261](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/run_odinw.py#L249-L261)：预测按 label 分桶（同一 label 的多个框聚成一 list）；类别匹配是**大小写不敏感**的——`class_names = [name.lower() for name in classes]` 后用 `k.lower()` 查表，命中则 `class_names.index(k.lower())` 得到类别索引，未命中（模型编造的类别名）`continue` 静默丢弃。这里的索引有一个隐含假设：`class_names` 的顺序必须与 COCO 标注文件中类别 id 升序一一对应——4.3 节会看到这个索引随后被用作 `cat_ids[label]` 的下标。

**预测打包**。[evaluation/ODinW-13/run_odinw.py:263-287](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/run_odinw.py#L263-L287)：每张图组装成 `(图元信息 dict, 预测 dict)` 二元组，其中图元信息含原图宽高与 `img_id`；预测 dict 含 `bboxes`（numpy 数组）、`scores`、`labels`。**关键点在 L280：`scores` 被统一置为 1.0**——生成式模型不输出置信度，所有预测同分。这会让 COCOeval 的按分数排序退化（并列分数的顺序由数组顺序决定），mAP 实际衡量的是「把预测当作一个集合」的匹配质量，而非「按置信度排序」的检索质量。最后 L287 调用 `compute_metrics` 进入下一模块。

#### 4.2.4 代码实践

**实践目标**：编写「bbox 解析 + 双分支坐标换算 + IoU 对比 + 画框可视化」脚本，亲眼验证两条换算分支的差异，并判断哪个分支符合你手上模型的实际输出。

**操作步骤**：

1. 准备一张本地图片（记原图宽高 \( W, H \)），手写一个真值框（原图绝对坐标）；
2. 手工构造一条「模拟模型输出」字符串（示例代码）；
3. 按 run_odinw.py 的逻辑解析并分别用两条分支换算；
4. 用 IoU 判断哪条分支正确，并把两条分支的框都画到原图上对比。

示例代码（骨架，需补全图片路径后本地运行）：

```python
# practice_42_parse.py —— 示例代码：解析 + 双分支换算 + IoU + 可视化
import ast
import numpy as np
from PIL import Image, ImageDraw

W, H = 1920, 1080                      # 原图宽高（按你的图片改）
Wr, Hr = 1280, 720                     # 模拟 resized 尺寸（按 4.1 的 smart_resize 结果改）
gt_bbox = [500, 300, 900, 800]         # 原图坐标系的真值框

# 模拟「模型按 0-1000 相对坐标输出」：把真值归一到 0-1000
rel = [round(v / (W if i % 2 == 0 else H) * 1000) for i, v in enumerate(gt_bbox)]
answer = f"```json\n[{{\"bbox_2d\": {rel}, \"label\": \"boat\"}}]\n```"

# --- 复刻 run_odinw.py:216-236 的解析 ---
answer = answer.replace("```json", "").replace("```", "")
try:
    data = ast.literal_eval(answer.strip())
    pred_bboxes = [d["bbox_2d"] for d in data if len(d.get("bbox_2d", [])) == 4]
except Exception:
    pred_bboxes = []
pred = np.array(pred_bboxes).reshape(-1, 4)

# --- 复刻 run_odinw.py:239-245 的两条分支 ---
branch_abs  = pred / np.array([Wr, Hr, Wr, Hr]) * np.array([W, H, W, H])   # is_rel=0（默认）
branch_rel  = pred / 1000 * np.array([W, H, W, H])                          # is_rel=1

def iou(a, b):
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[2], b[2])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    union = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - inter
    return inter / union

for name, box in [("is_rel=0(默认)", branch_abs[0]), ("is_rel=1(相对)", branch_rel[0])]:
    print(f"{name}: 桢={box.tolist()}, 与真值 IoU={iou(box, gt_bbox):.3f}")

img = Image.open("your_image.jpg").convert("RGB")
draw = ImageDraw.Draw(img)
draw.rectangle(gt_bbox, outline="green", width=4)        # 真值：绿
draw.rectangle(list(map(int, branch_abs[0])), outline="red", width=4)    # 分支一：红
draw.rectangle(list(map(int, branch_rel[0])), outline="blue", width=4)   # 分支二：蓝
img.save("vis_compare.jpg")
```

**需要观察的现象**：由于模拟输出按 0-1000 相对坐标生成，`is_rel=1` 分支的 IoU 应接近 1.0（框与绿框重合），`is_rel=0` 分支的框被按 \( 1000/W_r \) 的比例错误缩放（红框明显偏小或偏大），IoU 低。

**预期结果**：蓝框 ≈ 绿框、IoU ≈ 1；红框与绿框大小失配。结论：对输出 0-1000 相对坐标的模型，评测应设 `is_rel=1`。把模拟输出改成「缩放图绝对坐标」再跑一遍，结论反转。**真实 Qwen3-VL 输出到底落哪个口径、两条分支下实际 mAP 差多少，待本地验证**——这正是本实践留给你的实验位：跑完官方 infer 后，分别在 `is_rel=0/1` 下跑 `eval` 并对比 mAP_50。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 `ast.literal_eval` 而不是 `json.loads`？各自的失效场景是什么？
**答案**：`literal_eval` 接受 Python 字面量，模型常见的大括号/单引号风格（如 `{'bbox_2d': [...]}`）也能解析，`json.loads` 会直接抛异常。失效场景互补：`literal_eval` 解析不了 `true/false/null`（Python 字面量是 `True/False/None`），`json.loads` 解析不了单引号。检测输出里几乎只有数字和字符串，所以 `literal_eval` 的宽容更实用。

**练习 2**：某条预测的 `bbox_2d` 只有 3 个数，和某条预测**缺 `label` 键**，两者的后果有何不同？
**答案**：前者只跳过那一条（`continue` 在循环体内逐条生效）；后者抛 KeyError，被外层 try/except 捕获后**整张图的全部预测被清空**。一个是「丢一个框」，一个是「丢整张图」。

**练习 3**：设 `img_w=1920, img_h=1080, resized_w=1280, resized_h=720`。模型输出 `[640, 360, 960, 540]`，分别在两条分支下换算成原图坐标。
**答案**：分支一：`[640/1280*1920, 360/720*1080, 960/1280*1920, 540/720*1080] = [960, 540, 1440, 810]`；分支二：`[640/1000*1920, 360/1000*1080, 960/1000*1920, 540/1000*1080] = [1228.8, 388.8, 1843.2, 583.2]`。两个结果差异巨大，正说明选错分支会系统性毁掉评测。

### 4.3 检测指标计算（eval_utils.py 与 COCOeval）

#### 4.3.1 概念说明

eval_utils.py 只做一件事：把 4.2 节产出的「每图 (bboxes, scores, labels)」翻译成 COCO 官方评测器能吃的格式，再驱动 `pycocotools.cocoeval.COCOeval` 算出 mAP 指标族。三步翻译：

1. **坐标系格式**：模型输出与脚本内部都用 **xyxy**（左上、右下角点），COCO 结果格式要求 **xywh**（左上角点 + 宽高）；
2. **类别索引 → category_id**：内部 label 是「类别表下标」，COCO 要的是标注文件里的 `category_id`；
3. **落盘 JSON**：COCOeval 的 `loadRes` 只接受 JSON 文件路径，所以预测要先写临时文件再读回。

对照 u6-l1 的三级判分瀑布：这里**没有任何 LLM judge**——检测的「判卷」完全是确定性的几何计算，同样的预测永远得到同样的分数。

#### 4.3.2 核心流程

`compute_metrics(results, outfile_prefix=None, _coco_api)` 的执行过程：

1. 从 results 中拆出预测元组（真值已在 `_coco_api` 里，不入参）；
2. `cat_ids = _coco_api.getCatIds()`、`img_ids = _coco_api.getImgIds()` 取全集；
3. `results2json` 把每个 (图, 框) 翻译成 `{"image_id", "bbox"(xywh), "score", "category_id"}` 写入 `{prefix}.bbox.json`；
4. `loadRes` 加载预测、构造 `COCOeval(coco_gt, coco_dt, 'bbox')`，设置 `maxDets=(100,300,1000)`、`iouThrs = linspace(0.5, 0.95, 10)`；
5. `evaluate → accumulate → summarize` 三连，从 `coco_eval.stats` 数组按固定下标取 6 个指标；
6. 无 `outfile_prefix` 时用 `tempfile.TemporaryDirectory` 中转，用完清理。

run_odinw.py 侧的汇总逻辑：每个子数据集得到一个 6 指标 dict，按名称排序后用 tabulate 打表，最后计算 13 个子数据集 mAP 列的均值作为 `Average`（总主指标）。

#### 4.3.3 源码精读

**xyxy → xywh**。[evaluation/ODinW-13/eval_utils.py:14-29](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/eval_utils.py#L14-L29)：`xyxy2xywh` 保留前两个分量，后两个换算为 `x2-x1`、`y2-y1`。一行就能写对，但方向搞反（xywh 当 xyxy 用）是新手做 COCO 评测最经典的错误——框会向右下「拉长」成宽高为坐标的巨型框。

**预测转 COCO 结果 JSON**。[evaluation/ODinW-13/eval_utils.py:32-63](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/eval_utils.py#L32-L63)：遍历每图结果，`image_id` 缺省用循环下标兜底；每框一条记录，`bbox` 经 `xyxy2xywh` 转换（L53）、`score` 转 float、`category_id = cat_ids[label]`（L55）。**这里呼应 4.2 的隐含假设**：`label` 是「配置类别表的下标」，`cat_ids` 是 `getCatIds()` 返回的类别 id 列表，两者按下标对齐——只有当配置 `metainfo.classes` 的顺序与标注文件类别 id 升序一致时映射才正确。官方 ODinW 配置满足此约定；自制数据集时必须自查。

**COCOeval 驱动**。[evaluation/ODinW-13/eval_utils.py:66-117](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/eval_utils.py#L66-L117)：L78-79 用 `np.linspace(0.5, 0.95, 10)` 生成 10 个 IoU 阈值；L87-90 无前缀时建临时目录；L92-96 取类别/图片全集并落盘翻译结果；L107 `loadRes` 读回；L112-117 构造 COCOeval 并设置 `catIds`、`imgIds`、`maxDets=(100,300,1000)`、`iouThrs`。两个细节值得注意：其一，`imgIds` 是**全集**——4.2 节中「解析失败被清空预测的图」不会从评测中消失，而是以「全漏检」身份进入召回率分母；其二，L108-110 捕获 `IndexError`，整个子数据集零有效预测时打印提示并跳出，此时该数据集指标缺失（后续汇总 L297 取 `result[k]` 会暴露问题）。

**stats 下标表**。[evaluation/ODinW-13/eval_utils.py:120-145](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/eval_utils.py#L120-L145)：`coco_metric_names` 字典给出 12 个统计量在 `coco_eval.stats` 数组中的固定下标（mAP=0、mAP_50=1、mAP_75=2、mAP_s/m/l=3/4/5、AR 系列=6~11）；L135-137 执行 evaluate/accumulate/summarize 三连；L139-145 只取 6 个 mAP 指标、保留 3 位小数。run_odinw.py 侧的打表与求均见 [run_odinw.py:292-336](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/run_odinw.py#L292-L336)：`metric_items` 顺序取值拼行（L293-297），对 13 行的 mAP 列求均得 `Average`（L300-303、L327），最终打印 `Final Average mAP`（L335）。tabulate 缺失时退化为逐行 print（L318-324）。

**双档采样参数对照**。检测任务输出长（一张图可能几十个框），Instruct 与 Thinking 两档脚本同样只差三个采样参数：[infer_instruct.sh:6-19](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/infer_instruct.sh#L6-L19) 为 `temperature 0.7 / top-p 0.8 / presence-penalty 1.5`，[infer_think.sh:6-19](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/infer_think.sh#L6-L19) 为 `temperature 0.6 / top-p 0.95 / presence-penalty 0.0`——与 u6-l3 在 VideoMME 上归纳的规律一致：Instruct 靠存在惩罚压制复读，Thinking 因长推理链需容忍重复而关掉惩罚；两档 `max-new-tokens` 同为 32768。`</think>` 剥离在推理侧完成（4.1 节 L137），eval 阶段无需关心模型档位，[eval_instruct.sh:6-9](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/eval_instruct.sh#L6-L9) 与 eval_think.sh 仅输入输出路径不同。

#### 4.3.4 代码实践

**实践目标**：不跑推理，用一个 2 图 × 2 类的玩具 COCO 数据集直接驱动 `compute_metrics`，从「已知 IoU 的预测」反推 mAP，建立对指标的量化手感。

**操作步骤**：

1. 构造玩具真值：两张 100×100 的图，每图 1 个真值框（类别 1、2），写成合法 COCO JSON；
2. 构造预测：类别正确但 IoU 分别为 ≈1.0、≈0.65 的两个框（手工算好坐标）；
3. 用 pycocotools 的 COCO 加载真值，按 4.2/4.3 的数据结构组装 results，调 `compute_metrics`。

示例代码：

```python
# practice_43_metrics.py —— 示例代码：玩具数据集驱动 compute_metrics
import json, numpy as np
from pycocotools.coco import COCO
import sys; sys.path.insert(0, "evaluation/ODinW-13")
from eval_utils import compute_metrics

gt = {"images": [{"id": 1, "width": 100, "height": 100, "file_name": "a.jpg"},
                 {"id": 2, "width": 100, "height": 100, "file_name": "b.jpg"}],
      "annotations": [
          {"id": 1, "image_id": 1, "category_id": 1, "bbox": [10, 10, 20, 20], "area": 400, "iscrowd": 0},
          {"id": 2, "image_id": 2, "category_id": 2, "bbox": [30, 30, 40, 40], "area": 1600, "iscrowd": 0}],
      "categories": [{"id": 1, "name": "cat"}, {"id": 2, "name": "dog"}]}
json.dump(gt, open("toy_gt.json", "w"))

coco = COCO("toy_gt.json")
results = [
    ({"width": 100, "height": 100, "img_id": 1},
     {"img_id": 1, "bboxes": np.array([[10, 10, 30, 30]]),      # IoU=1.0，命中
      "scores": np.array([1.0]), "labels": np.array([0])}),
    ({"width": 100, "height": 100, "img_id": 2},
     {"img_id": 2, "bboxes": np.array([[30, 30, 55, 55]]),      # 与真值 [30,30,70,70] IoU≈0.54
      "scores": np.array([1.0]), "labels": np.array([1])}),
]
print(compute_metrics(results, _coco_api=coco))
```

**需要观察的现象**：终端先打印 COCOeval 自己的 summarize 表格，随后返回 dict。mAP_50 应为 1.0（两框 IoU 都 ≥0.5 且类别正确），mAP（0.5:0.95 十阈值均值）低于 1.0（第二个框在高阈值下失配），mAP_75 介于两者之间。

**预期结果**：`{'mAP': 约0.7~0.8, 'mAP_50': 1.0, 'mAP_75': 1.0 或 0.5 左右, ...}`——具体数值取决于第二个框在各阈值下的命中情况（其 IoU≈0.54，故 0.55 及以上阈值全部失配）。手工验证：`mAP = (1.0 + 0.5×5/10) / 类别数` 的量级。精确数值**待本地验证**；若 `compute_metrics` 抛 KeyError，说明你某张图没进 `img_ids` 全集或类别映射错位。注意玩具真值的 `bbox` 是 xywh（COCO 惯例），而 results 里的预测是 xyxy（脚本内部惯例）——两种格式在同一实践里同时出现，正是 4.3.1 翻译步骤存在的原因。

#### 4.3.5 小练习与答案

**练习 1**：为什么所有预测 score 都置 1.0 会让 mAP 偏保守？
**答案**：COCO AP 按 score 降序累积 P-R 曲线，全部同分时排序退化为输入顺序，随机顺序可能把假阳性排在真阳性前面，压低曲线下面积。有真实置信度时模型排序更优，AP 更高。所以该脚本的 mAP 衡量的是「集合级检测质量」的下界近似。

**练习 2**：某子数据集全部图片的模型输出都解析失败，`compute_metrics` 会怎样？最终的 Average 还能算出来吗？
**答案**：`loadRes` 对空结果抛 IndexError，被 [eval_utils.py:108-110](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/eval_utils.py#L108-L110) 捕获后 break，返回空 dict；run_odinw.py 的汇总在 [L297](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/run_odinw.py#L297) 取 `result[k]` 时会 KeyError 崩溃——整表打不出来，需要先修推理输出格式再重跑 eval（好在 eval 不需要 GPU，成本低）。

**练习 3**：mAP_50 = 0.9 但 mAP_75 = 0.1，说明模型的问题出在哪？哪个数据集特性会放大这个问题？
**答案**：物体「找到了、类别对了」但框不准（IoU 集中在 0.5~0.75 之间）。小物体数据集会放大它——同样的像素级偏移在小框上造成的 IoU 下降远大于大框（面积基数小），对应 mAP_s 显著低于 mAP_l。

### 4.4 同构目录横向对比：五个基准的一张表

#### 4.4.1 概念说明

u6-l1 讲过 evaluation 下五个目录「结构同构」，本讲读完 ODinW-13 后，你已经具备把「同构」与「变异」都说清楚的条件。横向对比的价值在于：拿到第六个新基准时，你能立刻判断哪些件可以照抄、哪些件必须重写。

#### 4.4.2 核心流程

对比的四个维度：① 数据从哪来（TSV 下载 / HF datasets / 本地 COCO）；② 判分靠什么（LLM judge 瀑布 / 几何指标）；③ 有无 common_utils.py（base64/MD5 下载工具）；④ 汇总指标形态（accuracy / mAP 表）。

#### 4.4.3 源码精读

| 维度 | mmmu / MathVision / RealWorldQA | VideoMME（u6-l3） | **ODinW-13（本讲）** |
| --- | --- | --- | --- |
| 数据来源 | LMUData 目录下 TSV，MD5 校验下载（如 [MathVision/dataset_utils.py:10-26](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/MathVision/dataset_utils.py#L10-L26)） | HuggingFace datasets 按 duration 档加载 | **本地 COCO 标注 + 自备 `odinw13_config.py`**（[dataset_utils.py:101-130](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/dataset_utils.py#L101-L130)） |
| common_utils.py | 有（base64 编解码、MD5、下载） | 无 | **无**（本地数据无需下载与 base64） |
| 图像进 messages | dump_image 导出 base64 文件后引用 | 视频文件路径 | **`file://` 本地路径 + 元素级 min/max_pixels**（[dataset_utils.py:159-172](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/dataset_utils.py#L159-L172)） |
| 像素预算 | 28 口径遗产常数 | 脚本内常数 | **32 口径按公式现算（768~12800 token）**（[dataset_utils.py:111-116](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/dataset_utils.py#L111-L116)） |
| eval_utils.py 内容 | OpenAIWrapper/DashScopeWrapper + can_infer 三级瀑布 | 同左 | **无 judge：xyxy2xywh + results2json + COCOeval**（[eval_utils.py:14-150](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/eval_utils.py#L14-L150)） |
| 判分是否需 API key | 是（judge 兜底） | 是 | **否，纯几何确定性计算** |
| 汇总形态 | 总体/分 split 准确率 | 准确率 | **13 子数据集 × 6 指标表 + Average mAP**（[run_odinw.py:292-336](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/run_odinw.py#L292-L336)） |
| `prepare_inputs_for_vllm` | 逐字复用 | 逐字复用 | **逐字复用**（[run_odinw.py:28-50](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/run_odinw.py#L28-L50)，全仓 5 处同构） |

这张表提炼成三句话：**数据侧因数据托管形态而变**（TSV/HF/本地三态）；**判分侧因任务输出形态而变**（文字答案→LLM judge，几何框→确定性指标）；**推理侧完全不变**（三件套输入模式 + infer/eval 以 JSONL 解耦）。

#### 4.4.4 代码实践

**实践目标**：用检索工具亲自验证上表结论，而非采信讲义。

**操作步骤**：

1. 在仓库根目录执行 `grep -rn "prepare_inputs_for_vllm" evaluation/`，确认 5 个 run 脚本各定义一次、各调用一次；
2. 执行 `ls evaluation/*/ | grep common_utils` 观察哪些目录缺失该文件；
3. 执行 `grep -l "OpenAIWrapper" evaluation/*/eval_utils.py` 对比 judge 类的分布。

**需要观察的现象**：第 1 步应输出 10 行（5 定义 + 5 调用）；第 2 步只有 ODinW-13 与 VideoMME 没有任何输出；第 3 步只有 ODinW-13 的 eval_utils.py 不在命中列表中。

**预期结果**：与上表一致。若与表不符（例如未来仓库重构），以你的检索结果为准，并思考是什么变动引起的。

#### 4.4.5 小练习与答案

**练习 1**：如果要新增一个「图像分割」基准（输出 mask），按本讲框架哪些件必须新写？
**答案**：数据侧可借用 ODinW 的本地 COCO 模式（COCO 也含 segmentation 标注）；推理侧照抄 `prepare_inputs_for_vllm`；判分侧必须新写——模型输出的 mask 文本表示（如 RLE/多边形）解析 + `COCOeval(iouType='segm')`，bbox 专用逻辑（坐标归一化、xyxy2xywh）需替换。

**练习 2**：为什么 ODinW-13 的 eval 阶段完全不需要 OpenAI API key，而 mmmu 需要？
**答案**：mmmu 的判分要把自由文本答案与选项对齐，规则抽取失败时需 LLM 理解语义做兜底；ODinW 的答案是结构化几何对象，解析后的匹配规则（IoU 阈值 + 类别相等）是纯数学，不存在「语义模糊」需要 LLM 裁决。

**练习 3**：五个基准里哪两个的「数据准备心智负担」最低？为什么？
**答案**：mmmu/MathVision/RealWorldQA（TSV 自动下载，只需设 LMUData 环境变量）与 VideoMME（HF datasets 自动下载）负担低；ODinW-13 最高——需手动获取整个数据目录并自备 `odinw13_config.py`，路径重定基逻辑（4.1 节）也是为此而生。

## 5. 综合实践

**任务：搭一个「两图玩具检测集」并端到端跑通 ODinW 评测代码路径（全程免 GPU）。**

1. **造数据**：选 2~3 张自己的图片，用任意标注工具（或手写坐标）为每图标 2 个物体，写成合法 COCO 标注 JSON（xywh 真值 + categories）；仿照 README 的目录约定，建 `toy_data/odinw13_config.py`（含 `datasets` 与 `dataset_prefixes` 两个变量，`metainfo.classes` 顺序与 categories id 升序一致）；
2. **造预测**：不跑模型，直接手写一个 `toy_predictions.jsonl`——每行按 [run_odinw.py:139-145](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/run_odinw.py#L139-L145) 的 schema 构造，`result.gen` 填 `[{"bbox_2d": [...], "label": "..."}]` 风格字符串（故意混入一条围栏包裹、一条缺 label、一条 bbox 长度为 3 的脏数据），`extra_info` 按 [dataset_utils.py:179-189](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/dataset_utils.py#L179-L189) 填全（`resized_h/w` 用 4.1 的 smart_resize 公式实算）；
3. **跑评测**：执行 `python run_odinw.py eval --data-dir toy_data --input-file toy_predictions.jsonl --output-file toy_eval.json`（需 pycocotools、tabulate）；
4. **验证与观察**：
   - 脏数据三条各自的下场（围栏被剥掉、缺 label 清空整图、长度 3 被跳过）是否符合 4.2.3 的分析；
   - 分别在 `is_rel=0` 与 `is_rel=1` 环境变量下重跑，对比 mAP_50 变化，判断你的「模拟输出」口径；
   - 把某条预测框手动平移 10 像素，观察 mAP_50 不变、mAP_75 下降（小物体图上更剧烈）；
5. **可视化收尾**：把 4.2.4 的画框脚本接上 JSONL，绿框真值、红框预测，保存对比图。

**验收标准**：能解释最终 JSON 里 6 个指标各自为何是这个值；能说清每一处指标变化是由哪条代码分支引起的。整个流程若某步数值与预期不符，本讲义标注「待本地验证」之处即为排查起点。

## 6. 本讲小结

- **检测与选择题评测的本质差异**：输出是结构化几何对象（`[{"bbox_2d": [x1,y1,x2,y2], "label": ...}]`），判分是确定性几何计算（IoU + COCOeval mAP 族），全程无 LLM judge、无 API key；
- **prompt 是「类别闭集 + JSON 约定」的模板**：把配置里的类别清单注入一句固定指令；坐标口径并未在 prompt 中约定，由模型训练时的默认行为决定；
- **坐标归一化有两条分支**：默认 `is_rel=0` 按「缩放图绝对坐标」除以 `resized_w/h` 再放大到原图；`is_rel=1` 按「0-1000 相对坐标」除以 1000——分母 `resized_h/w` 在任务生成阶段由 smart_resize(32 口径) 预算（768~12800 token/图）预计算并随 JSONL 持久化；
- **解析的容错粒度不对称**：围栏剥离与 `ast.literal_eval` 兜底整图，bbox 长度≠4 逐条跳过，缺 label 键清空整图，类别名大小写不敏感匹配、未命中静默丢弃；
- **指标翻译三步**：xyxy→xywh、类别下标→`cat_ids` 映射（依赖配置类别序与 COCO 类别 id 升序一致的隐含假设）、score 恒置 1.0（排序退化，mAP 是集合质量下界）；
- **五目录同构律**：数据侧随托管形态三态变化、判分侧随任务输出形态二态变化、推理侧（`prepare_inputs_for_vllm` + JSONL 解耦）五处逐字复用。

## 7. 下一步学习建议

本讲结束后，评测体系（单元六）告一段落，两条去向：

1. **走向应用（推荐先走）**：进入 u7-l3「2D/3D Grounding 实践与坐标格式体系」——那里正面处理本讲留下的坐标口径问题（0-1000 相对 vs 绝对、智能框模式），并结合 `qwen-vl-finetune/tools/process_bbox.ipynb` 讲如何把自有检测标注转成 Qwen-VL 训练格式，与本讲的评测解析形成「训练 schema ↔ 评测 schema」闭环；
2. **走向闭环实战**：阅读 u7-l4 端到端讲义，把本讲的 `compute_metrics` 复用为自定义检测任务的评测器（替换 `odinw13_config.py` 即可挂接自建 COCO 数据集），完成「造数 → 微调 → 检测评测」的完整项目。

延伸阅读：`pycocotools` 的 `cocoeval.py` 源码（理解 AP 的 101 点插值与 maxDets 截断）、以及 [evaluation/ODinW-13/README.md](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/ODinW-13/README.md) 的 Troubleshooting 一节（官方对「Low mAP Scores」给出的排查清单正对应本讲 4.2/4.3 的各个易错点）。
