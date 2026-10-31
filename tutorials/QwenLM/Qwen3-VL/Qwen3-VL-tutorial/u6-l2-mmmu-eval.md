# MMMU 图文混评基准实战

> 前置讲义：u6-l1（评测框架通用架构）。本讲不再重复三件套的宏观结构，而是把镜头拉近到 `evaluation/mmmu/` 这一个目录，逐行讲透它的四个环节：prompt 组装、分辨率预算、推理主循环、判分汇总。

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `build_mmmu_prompt` 如何把一条 TSV 记录拼装成「图像 + Hint + Question + Options」的标准对话消息，并理解多选题 prompt 的组装规则。
2. 说清 `MIN_PIXELS = 1280*28*28`、`MAX_PIXELS = 5120*28*28` 这两个「28 口径遗产」常数在 Qwen3-VL（32 口径）下的真实语义，掌握按数据集设定元素级像素预算的技巧。
3. 跟踪 `run_inference` 从逐样本构建输入到 `llm.generate` 一次性批量推理、再到 JSONL 落盘的完整主循环。
4. 跟踪 `run_evaluation` 的判分链：规则抽取 → judge 兜底 → 随机保底，看懂总体准确率与按 split 准确率是怎么算出来的。
5. 独立跑通一次 MMMU 评测（含 subject 子集裁剪的正确姿势），并能抽查判错样本定位错因。

## 2. 前置知识

### 2.1 MMMU 是什么

MMMU（Massive Multi-discipline Multimodal Understanding）是一个大学水平的多学科多模态选择题基准：每道题带一张或多张图（图表、公式截图、电路图、艺术作品等），配一道四选一（A/B/C/D）的题目，少数题目是开放问答。评测的通用范式是三步：

1. **生成**：让被测模型看图答题；
2. **抽取**：从模型自由格式的回答里抽出「它到底选了哪个选项」；
3. **对答案**：与标注答案比对，统计命中率。

第 2 步是所有选择题评测的难点——模型可能回答「答案是 B」「我选第二个」「The answer is B.」甚至一段不含任何选项字母的长文。这正是 u6-l1 讲过的「三级瀑布判分」要解决的问题，本讲会看到它在 MMMU 中的完整落地。

### 2.2 承接自前几讲的概念

本讲直接复用以下已建立的知识，不再展开：

| 概念 | 来源讲义 | 一句话回顾 |
|---|---|---|
| TSV 数据集与 `LMUData` | u6-l1 | 数据以 TSV 形式按 MD5 校验下载到 `LMUData` 目录；图像以 base64 存在 `image` 列，长度 ≤64 的单元格是「指向其他行」的指针 |
| `prepare_inputs_for_vllm` 三字段 | u3-l2 / u6-l1 | `prompt`（chat template 渲染串）+ `multi_modal_data`（图像/视频张量）+ `mm_processor_kwargs`（视频采样开关） |
| 元素级 `min_pixels`/`max_pixels` | u2-l2 / u3-l1 | 写在 messages 图像元素里的像素区间，由 `smart_resize` 执行：宽高对齐到 `2×patch_size`（Qwen3-VL 为 32）的倍数、总像素夹入区间、保持长宽比 |
| 视觉 token 换算 | u1-l1 / u3-l1 | 一张 H×W 的图约占 \( \frac{H}{32} \times \frac{W}{32} \) 个视觉 token |
| 三级瀑布判分 | u6-l1 | `can_infer` 规则抽取优先，OpenAI 兼容 judge 兜底，随机选择保底 |

### 2.3 本讲的新术语

- **28 口径 / 32 口径**：像素预算常数写成 `N*28*28` 还是 `N*32*32`。28 = Qwen2-VL/2.5-VL 的 patch 有效边长（patch 14 × merge 2），32 = Qwen3-VL 的（patch 16 × merge 2）。
- **元素级预算 vs processor 级预算**：前者写在 messages 的图像元素里（本讲用法），后者写在 `processor.image_processor.size` 里（u3-l1 讲过）。vLLM 离线路径走的是前者。
- **Instruct 档 / Thinking 档采样超参**：仓库为两类模型各给一套默认生成参数，本讲会列对照表。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [evaluation/mmmu/run_mmmu.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py) | 主脚本，含 `infer`/`eval` 两个子命令 | `build_mmmu_prompt`、`run_inference`、`run_evaluation`、命令行参数 |
| [evaluation/mmmu/dataset_utils.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/dataset_utils.py) | 数据侧：下载 TSV、消解图像指针、导出图片 | `load_dataset`、`dump_image`、`MMMU_preproc` |
| [evaluation/mmmu/eval_utils.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py) | 判分侧：judge 封装、答案抽取、单样本判分 | `can_infer_option`、`can_infer_text`、`extract_answer_from_item`、`eval_single_sample` |
| [evaluation/mmmu/common_utils.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/common_utils.py) | 公共工具：base64 编解码、下载、MD5 | `dump_image` 的底层依赖 |
| [evaluation/mmmu/infer_instruct.sh](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/infer_instruct.sh) / [infer_think.sh](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/infer_think.sh) | Instruct / Thinking 两档推理启动脚本 | 两档采样超参对照 |
| [evaluation/mmmu/README.md](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/README.md) | 目录说明文档 | 输出文件格式、模型分档配置 |

数据流总览（承接 u6-l1 的三件套，填入 MMMU 具体函数名）：

```text
MMMU_DEV_VAL.tsv（自动下载，MD5 校验）
   │  load_dataset()          消解 image 指针列
   ▼
逐行 DataFrame.iterrows()
   │  build_mmmu_prompt()     dump_image 导出图片 + 拼题干 + 注入 min/max_pixels
   │  prepare_inputs_for_vllm()  chat template + process_vision_info
   ▼
llm.generate(all_inputs)       vLLM 批量推理（一次性）
   ▼
predictions.jsonl              每行含 gen（剥 </think> 后）与 gen_raw
   ▼
run_evaluation()
   │  MMMU_preproc()          开放题改造为选择题
   │  can_infer → judge → 随机   三级瀑布抽取答案
   ▼
evaluation.csv + *_acc.json    总体准确率 + 按 split 准确率
```

## 4. 核心概念与源码讲解

### 4.1 MMMU prompt 组装：build_mmmu_prompt

#### 4.1.1 概念说明

模型看到的「一道 MMMU 题」必须被翻译成 Qwen3-VL 的标准 messages 结构。`build_mmmu_prompt` 就是这个翻译器：输入是 TSV 的一行（pandas Series），输出是一个只含单条 user 消息的 messages 列表。它要处理三件事：

1. **图像落地**：TSV 里的图是 base64 字符串，必须先导出成磁盘文件，messages 里的 `image` 字段才能引用路径（vLLM 路径不支持直接喂 base64 巨串进 messages）。
2. **题干拼接**：MMMU 的可选字段有 `hint`（提示）、`question`（问题）、`A`~`D`（选项列）。字段可能缺失（NaN），拼接逻辑要容错。
3. **预算注入**：每张图都写上元素级 `min_pixels`/`max_pixels`，这是本评测的分辨率策略入口。

#### 4.1.2 核心流程

```text
输入 line（TSV 一行）
  ├─ dump_image_func(line) → 图片路径（单图 [path]，多图 [p1, p2, ...]）
  ├─ options = {A: ..., B: ..., C: ..., D: ...}（跳过 NaN 列）
  ├─ prompt 文本 = [Hint: {hint}\n] + Question: {question}\n
  │              + [Options:\nA. ...\nB. ...\n + 请从上述选项中选择正确答案]
  │              （最后 rstrip 去尾换行）
  ├─ content = [ {type:image, image:路径, min_pixels, max_pixels} × 图数 ]
  │            + [ {type:text, text:prompt} ]
  └─ return [{role: "user", content: content}]
```

注意两个容错分支：`hint` 为 NaN 时整行 Hint 省略；`options` 为空（开放题，A~D 全 NaN）时 Options 段与「请选择」指示语都省略——此时 prompt 退化为开放问答，这个不对称会在 4.4 的判分侧被 `MMMU_preproc` 补偿回来。

#### 4.1.3 源码精读

**（1）选项收集与题干拼接**。[evaluation/mmmu/run_mmmu.py:L33-L47](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L33-L47)：先调 `dump_image_func` 导出图片；再用字典推导从 `string.ascii_uppercase` 里挑出「列存在且非 NaN」的选项列；然后按「Hint → Question → Options → 作答指示」的固定顺序拼字符串，`hint` 缺失时跳过、选项为空时连作答指示一起跳过，最后 `rstrip()` 去掉尾部换行。

**（2）多图与单图的统一处理**。[evaluation/mmmu/run_mmmu.py:L50-L66](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L50-L66)：`dump_image` 永远返回**列表**（见 4.2.3），这里按 `isinstance(tgt_path, list)` 分支逐图生成 image 元素——每个元素都带同样的 `MIN_PIXELS`/`MAX_PIXELS`——最后追加一个 text 元素。图像在前、文本在后，即「先看图再读题」。

**（3）CoT 后处理**。[evaluation/mmmu/run_mmmu.py:L201-L205](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L201-L205)：若开启 `--use-cot`，推理主循环会把 CoT 文案**追加到 messages 最后一个 text 元素的尾部**（而不是新增元素）。默认文案（[L132-L133](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L132-L133)）大意是「不确定时就做合理猜测，别无限重复步骤」——这是给 Thinking 模型防「思考死循环」的安全阀。

**（4）图像从哪来**。[evaluation/mmmu/dataset_utils.py:L53-L75](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/dataset_utils.py#L53-L75)：`dump_image` 的三分支——`image` 列是列表（多图）时配合 `image_path` 列逐张 base64 解码落盘（已存在则跳过，幂等）；`image` 列是单串时解码为 `{index}.jpg`；没有 `image` 列时直接把 `image_path` 当作现成路径用。落盘目录由 `run_inference` 指定为 `$LMUData/images/MMMU`（[run_mmmu.py:L120-L125](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L120-L125)）。

#### 4.1.4 代码实践：离线打印一条 MMMU prompt

**实践目标**：不加载模型、不跑推理，直接观察 `build_mmmu_prompt` 的输出结构。

**操作步骤**（示例代码，需在已安装 vllm/transformers 的环境中运行，因为 `import run_mmmu` 会触发顶层 `from vllm import LLM`）：

```python
# save as inspect_prompt.py, run inside evaluation/mmmu/
import sys
sys.path.insert(0, '.')  # 保证 from dataset_utils import ... 能找到同目录文件
from run_mmmu import build_mmmu_prompt

line = {  # 示例数据：手工模拟 TSV 的一行
    'index': 1,
    'image_path': '/abs/path/to/some_chart.jpg',   # 用本地任意一张图
    'question': 'Which trend does the line chart show?',
    'hint': float('nan'),          # 模拟无提示
    'A': 'A steady increase', 'B': 'A sharp drop', 'C': 'No change', 'D': 'Cannot tell',
    'answer': 'A',
}

# 用 lambda 替身跳过真实的 base64 导出
messages = build_mmmu_prompt(line, lambda l: ['/abs/path/to/some_chart.jpg'], 'MMMU_DEV_VAL')
for ele in messages[0]['content']:
    print(ele)
```

**需要观察的现象**：

- content 有两个元素：先 image 后 text；
- image 元素里带着 `min_pixels: 1003520`、`max_pixels: 4014080`（即 1280×784 与 5120×784）；
- text 元素里**没有** `Hint:` 前缀（因为 hint 是 NaN），且以 `Question:` 开头、`Options:` 结尾、末尾是「Please select the correct answer from the options above.」。

**预期结果**：打印出的 text 大致形如：

```text
Question: Which trend does the line chart show?
Options:
A. A steady increase
B. A sharp drop
C. No change
D. Cannot tell
Please select the correct answer from the options above.
```

若把 `hint` 换成真实字符串，则首行会多出 `Hint: ...`。完整运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：一道开放题（A~D 全为 NaN）经过 `build_mmmu_prompt` 后，text 内容与选择题有何不同？这会对后续判分造成什么影响？

**参考答案**：`options` 字典为空，`if len(options)` 不成立，因此 Options 段和「Please select...」指示语都被跳过，prompt 退化为 `Hint + Question` 的开放问答。模型会生成一段自由文本而不是选项字母，判分侧无法用「找选项字母」的规则直接命中——这正是 4.4 中 `MMMU_preproc` 把开放题改造成「A=原答案、B=Other Answers」二选一的原因。

**练习 2**：为什么 CoT 文案是追加到已有 text 元素的尾部，而不是在 content 里再插入一个新 text 元素？

**参考答案**：追加（[L202-L205](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L202-L205)）保证图像仍然全部位于文本之前、且只有一条文本指令，与 `build_mmmu_prompt` 产出的「先图后文」结构保持一致；同时代码显式检查 `last_content['type'] == 'text'` 才追加，避免把指令错误地拼到图像元素上。若插入新元素则会破坏「题目与作答要求在同一段文本里」的紧凑格式，也可能改变 chat template 渲染出的特殊 token 布局。

---

### 4.2 分辨率预算设定：MIN_PIXELS / MAX_PIXELS 与视觉 token 换算

#### 4.2.1 概念说明

这是本讲最值得停下来思考的一个模块。`build_mmmu_prompt` 的前两行定义了 MMMU 的分辨率策略：

```python
MIN_PIXELS = 1280*28*28  # ~1M pixels
MAX_PIXELS = 5120*28*28  # ~4M pixels
```

两个问题：这四个数字什么意思？为什么用的是 28 口径，而 Qwen3-VL 的有效 patch 边长是 32？

**第一问**：这是元素级像素预算（u2-l2 讲过的 `smart_resize` 区间边界），写进每个 image 元素后由 `process_vision_info` 执行。任何一张 MMMU 图，无论原始尺寸如何，送进模型前都会被缩放到 \([1{,}003{,}520,\ 4{,}014{,}080]\) 像素区间内、宽高对齐 32 的倍数、尽量保持长宽比。

**第二问**：28 是 Qwen2-VL/2.5-VL 的 patch 有效边长（patch 14 × 2×2 空间合并），在旧口径下这两个常数恰好等于「至少 1280 个视觉 token、至多 5120 个视觉 token」：

\[ n_{\text{token}}^{(28)} = \frac{H \times W}{28 \times 28} \in [1280,\ 5120] \]

Qwen3-VL 的有效边长是 32（patch 16 × merge 2），同一像素区间换算成：

\[ n_{\text{token}}^{(32)} = \frac{H \times W}{32 \times 32} = \frac{H \times W}{1024} \in [980,\ 3920] \]

即 MMMU 给每张图保底约 **980** 个视觉 token、上限约 **3920** 个。28 口径只是常数书写的历史习惯，`smart_resize` 只把这两个数当作区间边界使用，对齐因子永远由当前模型的 `2×patch_size` 决定（vLLM 路径下由 `prepare_inputs_for_vllm` 传入的 `image_patch_size=processor.image_processor.patch_size` 决定），所以旧口径常数在新模型上依然正确工作。

**为什么 MMMU 要把下限抬这么高**：qwen-vl-utils 的库级默认下限极低（约 4 个视觉 token）。一张 800×600 的图表在默认预算下只占约 469 个视觉 token，细节（坐标轴刻度、公式下标、图表线条）会被压糊；MMMU 是大学水平的专业题目，这些细节恰恰是解题关键。抬高 `min_pixels` 意味着**宁可多花 token、宁可对小图上采样，也要保住细节密度**——这就是「按数据集设定预算」的核心技巧：日常对话可以省 token，专业图文评测必须保真。

#### 4.2.2 核心流程

```text
build_mmmu_prompt 写入 min/max_pixels（每张图相同）
   ↓
prepare_inputs_for_vllm → process_vision_info(messages, image_patch_size=16, ...)
   ↓
fetch_image → smart_resize(宽高, factor=32, min_pixels, max_pixels)
   ├─ 宽高先就近对齐到 32 的倍数
   ├─ 总像素 > max → 等比缩小（floor）
   ├─ 总像素 < min → 等比放大（ceil）
   └─ 结果宽高均为 32 的倍数，像素落在区间内（floor/ceil 后可能略越界一点）
   ↓
视觉 token 数 = (H'/32) × (W'/32)，约 980 ~ 3920
```

#### 4.2.3 源码精读

**（1）常数定义位置**。[evaluation/mmmu/run_mmmu.py:L29-L31](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L29-L31)：`MIN_PIXELS`/`MAX_PIXELS` 是 `build_mmmu_prompt` 的**函数内局部常量**，不是命令行参数——想改分辨率只能改源码（README 的「Custom Image Resolution」一节也是这么教的）。做对比实验时记得复制脚本，别动仓库原文件。

**（2）逐图注入**。[evaluation/mmmu/run_mmmu.py:L51-L65](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L51-L65)：无论单图还是多图，每个 image 元素都写入相同的 `min_pixels`/`max_pixels` 键。注意这是**每张图各自**的预算：一道 3 图题的总视觉 token 上限约为 3×3920。

**（3）预算的执行者**。[evaluation/mmmu/run_mmmu.py:L89-L95](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L89-L95)：`prepare_inputs_for_vllm` 调用 `process_vision_info` 时显式传 `image_patch_size=processor.image_processor.patch_size`（Qwen3-VL 为 16，对齐因子 32）与 `return_video_kwargs=True`——这正是 u2-l1 强调过的最佳实践：patch 边长从 processor 动态读取，而非硬编码。MMMU 无视频输入，`video_inputs` 为 None，`mm_data` 里只有 `image` 键（[L97-L101](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L97-L101)）。

#### 4.2.4 代码实践：验证预算下的实际尺寸与 token 数

**实践目标**：对同一张图，对比「MMMU 预算」与「库默认预算」下 `process_vision_info` 的输出尺寸，手算视觉 token 数。

**操作步骤**（示例代码，需安装 `qwen-vl-utils`；不需要 GPU 与模型权重）：

```python
from qwen_vl_utils import process_vision_info

IMG = '/abs/path/to/some_chart.jpg'   # 本地任意一张图，最好选一张 800x600 左右的

def run(min_p=None, max_p=None):
    ele = {"type": "image", "image": IMG}
    if min_p: ele["min_pixels"] = min_p
    if max_p: ele["max_pixels"] = max_p
    msgs = [{"role": "user", "content": [ele]}]
    imgs, _, _ = process_vision_info(
        msgs, image_patch_size=16, return_video_kwargs=True, return_video_metadata=True)
    w, h = imgs[0].size               # PIL 的 size 是 (W, H)
    print(f"尺寸 {w}x{h}，像素 {w*h}，视觉 token ≈ {(w*h)//1024}")

print("库默认预算："); run()
print("MMMU 预算："); run(1280*28*28, 5120*28*28)
```

**需要观察的现象**：

- MMMU 预算下输出的宽、高都是 **32 的倍数**，总像素落在 \([1{,}003{,}520,\ 4{,}014{,}080]\) 附近（ceil 放大后允许略超下限）；
- 若原图小于 ~1M 像素，会被**上采样**到下限附近；若远大于 ~4M 像素，会被下采样；
- 库默认预算下，小图基本保持原样，视觉 token 数远低于 980。

**预期结果**：以一张 800×600 的图为例，MMMU 预算下应得到约 1000+ 个视觉 token，而默认预算下约 469 个（\(800 \times 600 / 1024\)）。把两个数字记入笔记——它们直接决定 `input_ids` 的长度（u3-l1 的因果链：size 预算 → 缩放后尺寸 → 视觉 token 数 → 输入长度）。完整数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：把 `MIN_PIXELS`/`MAX_PIXELS` 改成 `1280*32*32`/`5120*32*32`（32 口径），对 Qwen3-VL 而言预算变宽松了还是变紧了？

**参考答案**：变宽松了。32 口径下下限像素变为 \(1280 \times 1024 = 1{,}310{,}720\)、上限变为 \(5120 \times 1024 = 5{,}242{,}880\)，都大于原来的 1,003,520 与 4,014,080。语义上从「约 980 个 token 起」变成「精确 1280 个 token 起」。两种写法都合法，因为 `smart_resize` 只看像素数值本身。

**练习 2**：一道 MMMU 题有 3 张图，全部顶到上限时，仅视觉部分的输入长度大约是多少 token？这解释了 `--max-images-per-prompt`（默认 10）和 `--max-model-len`（默认 128000）里的哪一个更「紧」？

**参考答案**：约 \(3 \times 3920 \approx 11760\) 个视觉 token。相比之下 `max_model_len=128000` 非常宽裕，真正起约束的是图像数量上限 `limit_mm_per_prompt={"image": 10}`——10 张图顶格约 39,200 视觉 token，仍远小于 128000，所以这两个默认值对 MMMU 而言都留了充足余量（128000 主要是为 Thinking 模型最长 40960 的生成长度准备的）。

---

### 4.3 推理主循环：run_inference 与两段式落盘

#### 4.3.1 概念说明

`run_inference` 是「三件套」里 run 脚本的推理侧实现。它的关键设计在 u6-l1 已给出框架（infer/eval 两段式、以文件解耦 GPU 与 API 资源），本讲聚焦三个 MMMU 特有的细节：

1. **先全量构建、后一次性生成**：先把所有样本的 vLLM 输入字典准备完，再调一次 `llm.generate(all_inputs)`。这与逐条 `generate` 相比，能让 vLLM 的连续批处理（continuous batching）引擎自由调度，吞吐最大化。
2. **`</think>` 剥离**：同一份脚本要同时服务 Instruct 与 Thinking 模型。Thinking 模型的输出是 `<think>推理过程</think>最终答案`，判分前必须剥掉推理段。
3. **JSONL 落盘的四字段结构**：每行存 `question_id`、`annotation`（TSV 原行）、`task`、`result`（含 `gen` 与 `gen_raw`）、`messages`——把「题目 + 原始输出 + 送达模型的完整输入」全部归档，评测与事后审计都不需要再碰图像与模型。

#### 4.3.2 核心流程

```text
run_inference(args)
  ├─ load_dataset(args.dataset)                    # 首次运行自动下载 TSV
  ├─ img_root = $LMUData/images/MMMU               # 图片导出目录
  ├─ SamplingParams(temperature, top_p, top_k,
  │                 max_tokens, repetition/presence_penalty)
  ├─ processor = AutoProcessor.from_pretrained(model_path)
  ├─ llm = LLM(model, tensor_parallel_size, gpu_memory_utilization,
  │            max_model_len, limit_mm_per_prompt, seed=42)
  ├─ for line in data.iterrows():                  # 逐样本三步流水
  │     ├─ numpy 标量 → Python 原生 int/float      # 为了 json.dumps
  │     ├─ messages = build_mmmu_prompt(line, ...)
  │     ├─ (可选) 追加 CoT 文案到最后一个 text 元素
  │     └─ vllm_input = prepare_inputs_for_vllm(messages, processor)
  ├─ outputs = llm.generate(all_inputs, sampling_params)   # 一次性批量
  ├─ response_final = response.split("</think>")[-1].strip()
  └─ 逐行写 JSONL：question_id / annotation / task / result / messages
```

#### 4.3.3 源码精读

**（1）numpy 类型归一化**。[evaluation/mmmu/run_mmmu.py:L189-L197](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L189-L197)：pandas 读 TSV 后数值列是 `np.integer`/`np.floating`，`json.dumps` 无法序列化它们，所以每行先转成 Python 原生 `int`/`float`。这是一个所有「pandas → JSON」管线都要过的坑。

**（2）三步流水与输入积累**。[evaluation/mmmu/run_mmmu.py:L199-L212](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L199-L212)：对每行依次执行 4.1 的 `build_mmmu_prompt`、可选的 CoT 追加、4.2 的 `prepare_inputs_for_vllm`，把 vLLM 输入字典、原始行 dict、messages 三份平行列表积累起来。注意此时**还没有任何 GPU 计算**——图像解码、缩放、chat template 渲染全部发生在 CPU 侧的准备阶段。

**（3）引擎参数**。[evaluation/mmmu/run_mmmu.py:L172-L180](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L172-L180)：`LLM(...)` 的参数与命令行一一对应：`tensor_parallel_size` 未指定时自动取 GPU 数（[L423-L425](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L423-L425)）；`limit_mm_per_prompt={"image": args.max_images_per_prompt}` 限制单条请求最多 10 张图；`seed=42` 固定随机种子，配合采样温度保证实验可复现。

**（4）批量生成与耗时统计**。[evaluation/mmmu/run_mmmu.py:L216-L228](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L216-L228)：单次 `llm.generate(all_inputs, sampling_params=...)` 完成全部推理，随后打印总耗时、每样本平均耗时与吞吐量——评测报告里「samples/second」的出处。

**（5）`</think>` 剥离与落盘**。[evaluation/mmmu/run_mmmu.py:L234-L252](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L234-L252)：对每个输出取 `output.outputs[0].text`（vLLM 只返回新生成部分，无需裁剪输入前缀，u3-l2 已讲）；`str(response).split("</think>")[-1].strip()` 一行同时服务两类模型——Instruct 输出不含该标签，`split` 后原串不变；Thinking 输出则只剩 `</think>` 之后的最终答案。`gen`（剥离后）与 `gen_raw`（原始）双双入档，判分侧只消费 `gen`（见 4.4）。

**（6）两档采样超参对照**。脚本默认值即 Instruct 档（[L391-L402](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L391-L402)），Thinking 档由 [infer_think.sh:L11-L16](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/infer_think.sh#L11-L16) 显式覆盖：

| 参数 | Instruct（脚本默认 / infer_instruct.sh） | Thinking（infer_think.sh） | 差异原因 |
|---|---|---|---|
| `--max-new-tokens` | 32768 | 40960 | Thinking 需要更长的推理链空间 |
| `--temperature` | 0.7 | 1.0 | Thinking 模型按官方推荐高温采样 |
| `--top-p` | 0.8 | 0.95 | 同上 |
| `--top-k` | 20 | 20 | 相同 |
| `--repetition-penalty` | 1.0 | 1.0 | 相同 |
| `--presence-penalty` | 1.5 | 0.0 | Instruct 档靠它抑制选择题复读；Thinking 档长推理中重复用词是正常现象，惩罚会剪断推理链，必须归零 |

#### 4.3.4 代码实践：跟踪一条样本从 TSV 行到 vLLM 输入

**实践目标**：不启动 vLLM 引擎，手动执行主循环体的三步流水，观察一个 `vllm_input` 字典的三个字段。

**操作步骤**（示例代码，需能联网拉取 processor 配置；只加载 processor 不加载模型权重，CPU 即可）：

```python
# save as inspect_vllm_input.py, run inside evaluation/mmmu/
import sys
sys.path.insert(0, '.')
from run_mmmu import build_mmmu_prompt, prepare_inputs_for_vllm
from transformers import AutoProcessor

MODEL = 'Qwen/Qwen3-VL-2B-Instruct'
processor = AutoProcessor.from_pretrained(MODEL)

line = {  # 示例数据
    'index': 1,
    'image_path': '/abs/path/to/some_chart.jpg',
    'question': 'Which trend does the line chart show?',
    'hint': float('nan'),
    'A': 'A steady increase', 'B': 'A sharp drop', 'C': 'No change', 'D': 'Cannot tell',
    'answer': 'A',
}
messages = build_mmmu_prompt(line, lambda l: ['/abs/path/to/some_chart.jpg'], 'MMMU_DEV_VAL')
vllm_input = prepare_inputs_for_vllm(messages, processor)

print(vllm_input['prompt'])                    # 字段 1：渲染后的完整文本
print(vllm_input['multi_modal_data']['image'][0].size)  # 字段 2：缩放后的图像尺寸
print(vllm_input['mm_processor_kwargs'])       # 字段 3：视频采样开关（此处无视频）
```

**需要观察的现象**：

- `prompt` 里含 `<|vision_start|>` 与图像占位 token、`<|im_start|>user` 等 chat template 特殊标记，且以 `<|im_end|>\n<|im_start|>assistant` 结尾（`add_generation_prompt=True` 的效果）；
- 图像尺寸与 4.2.4 实践中 MMMU 预算下的输出一致——证明元素级预算在 vLLM 路径真实生效；
- `mm_processor_kwargs` 里只有视频相关键（如 `do_sample_frames: False`），无图像键。

**预期结果**：三个字段与 u3-l2 总结的三字段契约完全对上。完整输出待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：如果某条样本的 `messages` 没有存进 JSONL，评测阶段会受影响吗？

**参考答案**：不会。`run_evaluation` 只读 `job["annotation"]` 与 `job["result"]["gen"]`（[L261-L266](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L261-L266)），真值元数据另行通过 `load_dataset(args.dataset)` 重新加载。`messages` 入档纯粹是为了事后审计与复现（能精确重放当时送达模型的输入，包括像素预算）。

**练习 2**：为什么 `presence_penalty` 的默认值是 1.5 这样一个偏强的惩罚，而 Thinking 脚本里又必须归零？

**参考答案**：多选题场景下，Instruct 模型容易陷入「复读选项原文」的循环（尤其当答案不确定时反复罗列 A/B/C/D 的内容），presence_penalty=1.5 对已出现过的 token 施加持续惩罚，能有效打断复读（脚本在 [L153-L154](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L153-L154) 还专门打印了「Anti-repetition enabled」提示）。而 Thinking 模型的长推理链里合法地反复引用同一术语与中间结论，任何 presence 惩罚都会让推理链在半途「变题」，所以 infer_think.sh 显式置 0。

**练习 3**：脚本把 `VLLM_WORKER_MULTIPROC_METHOD` 设为 `spawn`（[L25](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L25)），这解决什么问题？

**参考答案**：vLLM 的 worker 子进程默认可能用 `fork` 方式创建，`fork` 会复制父进程里已初始化的 CUDA 上下文，多个子进程共享同一 CUDA 上下文会导致崩溃或死锁；`spawn` 让每个 worker 重新导入模块、各自初始化 CUDA，是多 GPU 推理脚本的常见防御性设置（README 的 Troubleshooting 也把它列为环境问题的第一剂药方）。

---

### 4.4 结果判分与汇总：run_evaluation 的准确率统计

#### 4.4.1 概念说明

推理落盘的 JSONL 只是「模型说了什么」，评测要回答「说对了没有」。`run_evaluation` 的判分链在 u6-l1 的三级瀑布框架下展开为五个具体步骤：

1. **读回与注入**：把 `result.gen` 注入 `annotation.prediction`，重组为 DataFrame；
2. **口径归一**：列名小写化、预测字符串化；
3. **真值挂接**：从重新加载的 `meta`（TSV 真身）构造 `answer_map`，并对数据侧执行 `MMMU_preproc`（开放题改造）；
4. **逐样本判分**：`eval_single_sample` = `extract_answer_from_item`（规则 → judge → 随机）+ 与 GT 比对得 `hit`；
5. **汇总落盘**：总体准确率 + 按 split 准确率，写 CSV 与 JSON。

这里有一个容易看漏的**双 DataFrame 设计**：`data`（预测侧，会被 `MMMU_preproc` 改写选项列）与 `meta`（真值侧，保持原样只提供 `answer_map`）。二者用 `index` 对齐。

#### 4.4.2 核心流程

```text
predictions.jsonl ──► data（prediction 列 = gen）
meta = load_dataset() ──► answer_map{index: 标准答案}
assert data ⊆ meta                       # 子集合法
data = MMMU_preproc(data)                # 开放题: A=原答案, B='Other Answers'
answer_map 中非 A-Z 的答案 → 强制 'A'    # 与 preproc 配对，统一 GT 口径
data['GT'] = answer_map[index]
for item in data:  eval_single_sample
    ├─ choices = build_choices(item)     # 非空选项列
    ├─ ret = can_infer(prediction, choices)   # 规则抽取
    │     ├─ can_infer_option: 标点→空格→分词→唯一选项字母即中
    │     └─ can_infer_text:  选项文本⊆预测文本（忽略大小写）
    ├─ 规则失败 → judge 模型（最多 25 次重试）
    ├─ judge 也失败 → 随机选一个（含 'Z'），extract_flag=False
    └─ hit = (抽取结果 == GT)
accuracy = mean(hit)；accuracy_by_split 按 split 列分组
```

#### 4.4.3 源码精读

**（1）读回、注入与列名归一**。[evaluation/mmmu/run_mmmu.py:L260-L273](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L260-L273)：逐行 `json.loads`，把 `job["result"]["gen"]` 写进 `annotation["prediction"]`；按 `index` 排序；预测转字符串；最后一行循环把「非单字母列名」全部小写化——`k not in list(string.ascii_uppercase)` 保证选项列 `A`~`Z` 不被误改，其余列（如可能大小写不一致的表头）统一到小写口径。

**（2）子集校验与真值挂接**。[evaluation/mmmu/run_mmmu.py:L276-L292](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L276-L292)：用两套 index→question 映射断言「评测文件必须是数据集本身或其子集」——这个 assert 是本讲综合实践中「先推理全量、再裁剪子集评测」策略合法性的直接依据。随后 `answer_map` 取自 `meta`，`MMMU_preproc(data)` 改写的是**预测侧**的选项列；`{k: (v if v in ... ascii_uppercase else 'A')}` 把开放题的非字母标准答案强制为 `'A'`——因为 preproc 已把原答案文本放进了选项 A，GT 统一成 `'A'` 后二者可比。

**（3）开放题改造**。[evaluation/mmmu/dataset_utils.py:L77-L94](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/dataset_utils.py#L77-L94)：`MMMU_preproc` 扫描 A 列为 NaN 的行（即开放题），把 `A` 设为原标准答案文本、`B` 设为字面量 `'Other Answers'`，并打印改造计数。这样开放题也变成二选一：模型回复与原答案语义一致 → 文本匹配命中 A → 与 GT `'A'` 相等 → hit。

**（4）规则抽取的两个入口**。[evaluation/mmmu/eval_utils.py:L171-L210](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L171-L210)（`can_infer_option`）：先把 `.()[],:;!*#{}` 等标点替换为空格再分词，避免「A.」这类带标点写法漏配；统计分词结果里出现了几个候选选项字母，**恰好一个**才返回该字母；特别地，若 `'A'` 出现在一个超过 3 个词的长回复里，直接返回 False——因为英语里 "a" 是量词，极易误判（注释原话：「A might be a quantifier」）。[L212-L231](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L212-L231)（`can_infer_text` 与 `can_infer`）：字母抽取失败后退而求其次，把每个选项文本小写后看是否作为子串出现在预测里，同样要求**恰好命中一个**；两个入口在 `can_infer` 里串联。

**（5）judge 兜底与随机保底**。[evaluation/mmmu/eval_utils.py:L266-L311](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L266-L311)：`extract_answer_from_item` 先试规则（多数样本在此命中，零 API 成本）；失败则把 question/options/prediction 填进 [L247-L264](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L247-L264) 的 few-shot 模板发给 judge 模型（`max_completion_tokens=4096, temperature=0`，见 [L104-L111](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L104-L111)），judge 的回答再过一遍 `can_infer`；最多重试 25 次、每次随机退避 `random.random() * wait * 2` 秒；全部失败后**随机**从选项+`Z` 中挑一个并在 log 里注明「randomly generate one」，`extract_flag=False` 保留审计线索。judge 的构造由 `build_judge`（[L158-L169](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L158-L169)）按 `--api-type` 选择 DashScope 或自建 OpenAI 兼容端点，密钥全部来自环境变量。

**（6）单样本判分与并行调度**。[evaluation/mmmu/eval_utils.py:L313-L334](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L313-L334)：`eval_single_sample` 返回 10 个审计字段（`extracted_answer`、`extraction_method`、`extraction_success`、`extraction_log`、`gt`、`hit` 等）。调度在 [run_mmmu.py:L313-L331](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L313-L331)：设 `DEBUG=true` 环境变量则单线程只跑前 5 条（排障用）；否则 `ThreadPoolExecutor(max_workers=nproc)` 并行打 API（默认 4，脚本建议 16）。

**（7）准确率汇总与落盘**。[evaluation/mmmu/run_mmmu.py:L333-L359](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L333-L359)：总体准确率就是 `hit` 的均值；再按 `split` 列（MMMU 的学科/子集维度）分组求均值，逐组打印；最后写两个文件——逐样本 CSV 和 `*_acc.json`（含 `overall_accuracy` 与 `accuracy_by_split`）。

#### 4.4.4 代码实践：亲手喂三个回答给规则抽取器

**实践目标**：不跑推理、不调 API，用三个手工构造的回答验证 `can_infer` 的两条规则路径与一个失败案例。

**操作步骤**（示例代码，`eval_utils` 只依赖 requests/PIL/pandas，无需 GPU）：

```python
# save as inspect_can_infer.py, run inside evaluation/mmmu/
import sys
sys.path.insert(0, '.')
from eval_utils import can_infer

choices = {'A': 'A steady increase', 'B': 'A sharp drop', 'C': 'No change', 'D': 'Cannot tell'}

for pred in [
    'The answer is B.',                       # 字母路径
    'Based on the chart, it shows a steady increase over the years.',  # 文本路径
    'I would say the third one.',             # 两条路径都失败 → 需要 judge
]:
    print(repr(pred), '->', can_infer(pred, choices))
```

**需要观察的现象**：

- 第 1 条：标点被替换、分词后 `B` 是唯一出现的选项字母 → 返回 `'B'`；
- 第 2 条：字母路径失败（无选项字母；注意句子里的 "a" 是量词，恰是 `can_infer_option` 专门防御的场景），文本路径中 `'a steady increase'` 是唯一被包含的选项文本 → 返回 `'A'`；
- 第 3 条：既无选项字母也无选项文本 → 返回 `False`，真实评测中它会进入 judge 分支。

**预期结果**：依次打印 `'B'`、`'A'`、`False`。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`answer_map` 里为什么要把非 A-Z 的答案强制改成 `'A'`？如果去掉这行会发生什么？

**参考答案**：MMMU 的开放题标准答案是自由文本（不在 A-Z 中），而 `MMMU_preproc` 已把该文本放进了选项 A。强制 GT 为 `'A'` 让「模型回复文本 → `can_infer_text` 命中 A」与「GT = 'A'」在同一口径下可比。去掉后 `hit = (opt == GT)` 对开放题永远为 False（抽取结果最多是 'A'/'B'/'Z'，永远不等于一段自由文本），所有开放题被误判为错。

**练习 2**：没有配置 judge API key 时直接跑 eval，会发生什么？结果还能信吗？

**参考答案**：`build_judge` 照常构造 wrapper，只是 `api_key` 为空串，每次请求失败返回 `'Failed to obtain answer via API'`。规则抽取成功的样本不受影响（多数选择题回答能被字母/文本路径命中）；规则失败的样本则要空转 25 次重试（每次带随机退避，单样本可能耗时数分钟）后落到**随机选择**，`extraction_success=False`。所以「能出分数，但边界样本带随机噪声且评测很慢」——正式报数必须配好 judge，并抽查 `extraction_success` 列。

**练习 3**：`DEBUG=true` 与 `--nproc` 分别作用于哪个阶段？为什么一个用环境变量、一个用命令行参数？

**参考答案**：都只作用于 eval 阶段：`DEBUG=true` 把判分切到单线程前 5 条（[L313-L323](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L313-L323)），用于快速排障；`--nproc` 控制 ThreadPoolExecutor 并行度（[L326-L331](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L326-L331)）。DEBUG 用环境变量属于原作者的临时调试手法，未写进 `--help`；正式使用建议只依赖命令行参数，避免环境变量残留导致「只评了 5 条」的假结果。

---

## 5. 综合实践：跑通一次 MMMU subject 子集评测并抽查判错样本

这是贯穿本讲四个模块的完整实战：用 Qwen3-VL-2B-Instruct 在 MMMU 验证集的一个 subject 子集上完成「推理 → 评测 → 读数 → 错因分析」闭环。

### 5.1 环境准备

```bash
cd evaluation/mmmu
pip install -r requirements.txt        # vllm / transformers / qwen_vl_utils / pandas / Pillow / requests
export LMUData=/abs/path/to/mmmu_data  # 数据根目录；也可用 --data-dir 传入（脚本会写入该环境变量）
mkdir -p results
```

注意：`LMUData` 必须可用——`load_dataset` 与 `img_root` 都直接读 `os.environ['LMUData']`。若要跑评测的 judge 兜底，还需按 README 配置 `CHATGPT_DASHSCOPE_API_KEY`/`DASHSCOPE_API_BASE`（或 `MIT_SPIDER_TOKEN`/`MIT_SPIDER_URL`）。

### 5.2 关键坑：subject 子集为什么不能靠裁剪 TSV

`load_dataset` 每次都会做 MD5 校验（[dataset_utils.py:L19-L21](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/dataset_utils.py#L19-L21)）：文件不存在**或 MD5 不等于官方值**就重新下载完整 TSV 覆盖本地文件。因此把手工裁剪的子集存成 `MMMU_DEV_VAL.tsv`（或改 `--dataset` 名字）都会被自动覆盖，白做功。

正确姿势二选一：

- **策略 A（零改码，推荐）**：推理跑官方全量 `MMMU_DEV_VAL`，然后**在 JSONL 层面**过滤出 subject 子集再交给 eval——eval 侧的 assert 明确允许「评测文件是数据集子集」（[run_mmmu.py:L283-L286](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L283-L286)）。
- **策略 B（省 GPU，复制脚本改一行）**：把 `run_mmmu.py` 复制为自己的 `run_mmmu_subset.py`（不动仓库原文件），在 `data = load_dataset(args.dataset)` 之后加一行过滤，只推理子集。

### 5.3 步骤一：先摸清 index 格式

先跑一次推理（或写三行 pandas 脚本读 TSV）打印 `data['index'].head()`。按 MMMU 的命名约定，index 形如 `validation_Art_and_Design_1`（subject 编码在 index 前缀里，具体大小写与连字符格式**以本地打印为准，待本地验证**）。同时打印 `data['split'].unique()` 观察 split 取值。

### 5.4 步骤二：推理

**策略 B 示例**（在 `run_mmmu_subset.py` 中 `load_dataset` 之后加过滤，示例代码）：

```python
data = load_dataset(args.dataset)
print(data['index'].head())                                   # 先确认格式
data = data[data['index'].astype(str).str.contains('Art_and_Design')]  # 按实际打印结果调整
print(f"Subset size: {len(data)}")
```

然后启动推理（2B 模型 Instruct 档参数即脚本默认值）：

```bash
python run_mmmu_subset.py infer \
    --model-path Qwen/Qwen3-VL-2B-Instruct \
    --data-dir /abs/path/to/mmmu_data \
    --dataset MMMU_DEV_VAL \
    --output-file results/art_predictions.jsonl
```

**需要观察的现象**：首次运行自动下载 TSV 并打印 `Downloading ... dataset...`；随后逐样本打印 `Building prompts` 进度条（此阶段完成图像导出与像素预算缩放，可在 `$LMUData/images/MMMU` 下看到导出的 jpg）；最后打印批量推理的吞吐量与 `Results saved to ...`。

**预期结果**：`results/art_predictions.jsonl` 每行含 `question_id`/`annotation`/`task`/`result`（`gen` 与 `gen_raw`）/`messages` 五个键。若走策略 A，则在此用一段十行脚本按 `annotation['index']` 过滤出子集 JSONL 再进入下一步。

### 5.5 步骤三：评测

```bash
python run_mmmu.py eval \
    --data-dir /abs/path/to/mmmu_data \
    --input-file results/art_predictions.jsonl \
    --output-file results/art_eval.csv \
    --dataset MMMU_DEV_VAL \
    --eval-model gpt-3.5-turbo-0125 \
    --api-type dash \
    --nproc 16
```

排障时可先 `DEBUG=true` 前置运行，只判 5 条验证链路。**需要观察的现象**：`MMMU_preproc` 打印开放题改造计数；判分进度条；每个 split 一行准确率；最后打印 `Overall accuracy`。**预期结果**：得到 `results/art_eval.csv` 与 `results/art_eval_acc.json`，后者形如 `{"overall_accuracy": 0.xxxx, "accuracy_by_split": {...}}`。2B 模型在单一 subject 上的具体分数待本地验证。

### 5.6 步骤四：抽查 3 个判错样本

（示例代码）把判错样本与 `gen_raw` 对齐，区分三类错因：模型真选错、抽取失败（`extraction_success=False`，答案可能对但没抽出来）、随机兜底：

```python
import json, pandas as pd

df = pd.read_csv('results/art_eval.csv')
raw = {}
with open('results/art_predictions.jsonl') as f:
    for line in f:
        job = json.loads(line)
        raw[str(job['question_id'])] = job['result']['gen_raw']

wrong = df[df['hit'] == 0].head(3)
for _, row in wrong.iterrows():
    print('=' * 60)
    print('index:', row['index'], '| GT:', row['gt'],
          '| 抽取:', row['extracted_answer'],
          '| 方式:', row['extraction_method'],
          '| 抽取成功:', row['extraction_success'])
    print('原始输出:', raw.get(str(row['index']), '<未找到>')[:400])
```

**预期结果**：3 个样本的 `extraction_method` 大多为 `rule`（规则抽取）；若出现 `gpt-3.5-turbo-0125` 说明走了 judge，`extraction_success=False` 则说明落到随机兜底、该条判错不可信。把「真错 / 抽取错 / 随机错」三类的数量写进实验报告——这是判分可信度的直接证据。

## 6. 本讲小结

- `build_mmmu_prompt` 把 TSV 一行翻译成「先图后文」的单轮 user 消息：图像元素带元素级 `min_pixels`/`max_pixels`，文本按 `Hint → Question → Options → 作答指示` 顺序拼接，`hint` 缺失与开放题（无选项列）各有容错分支。
- `MIN_PIXELS=1280*28*28`、`MAX_PIXELS=5120*28*28` 是 28 口径遗产常数；在 Qwen3-VL 的 32 口径下对应每图约 980~3920 个视觉 token。`smart_resize` 只把它们当区间边界，对齐因子始终由 `2×patch_size` 决定，故旧口径常数在新模型上依然正确。
- 抬高 `min_pixels` 是「按数据集定预算」的典型技巧：专业图文评测宁可上采样小图、多花 token，也要保住图表/公式细节；这与日常对话「省 token」的默认策略相反。
- `run_inference` 是「CPU 全量构建 → 单次 `llm.generate` 批量推理 → JSONL 归档」的流水线；`split("</think>")[-1]` 一行同时兼容 Instruct 与 Thinking 输出；`presence_penalty` 在两档间的取舍（1.5 vs 0.0）体现「防复读」与「护推理链」的矛盾。
- `run_evaluation` 的判分链是「列名归一 → 子集校验 → `MMMU_preproc` 开放题改造 + GT 统一为 'A' → 规则/judge/随机三级抽取 → hit 比对」，预测侧 `data` 与真值侧 `meta` 是两个分工明确的 DataFrame。
- 想裁剪 subject 子集，不能改 TSV（MD5 校验会触发重新下载覆盖），合法做法是「推理全量 + JSONL 过滤子集再 eval」或「复制脚本加过滤行」；`DEBUG=true` 可只判 5 条快速排障。

## 7. 下一步学习建议

- **下一讲 u6-l3（VideoMME 视频评测）**：本讲的 Instruct/Thinking 双档参数表将在 VideoMME 的 `infer_instruct.sh`/`infer_think.sh` 中再次出现，可横向对照两套脚本；同时把 u2-l4 的 `video_metadata` 时间戳机制接入 `prepare_inputs_for_vllm` 的视频分支，补齐本讲（纯图像）未走到的视频多模态路径。
- **u6-l4（ODinW-13 检测评测）**：看同一个三件套骨架如何把「选择题判分」替换为「bbox 解析与检测指标」，检验你对通用架构的抽象是否成立。
- **源码延伸阅读**：对照 [evaluation/mmmu/eval_utils.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py) 中 judge 的 few-shot 模板（`build_prompt`），思考为什么模板要求 judge 在「全部选项都不相似」时输出 `Z`——这与随机兜底的审计标记如何配合。
- 为 u7-l4 的收官项目做准备：本讲的「自测集 + 三级判分 + 错因抽查」流程可以直接复用于微调前后效果对比。
