# VideoMME 视频评测与 Instruct/Thinking 双模式

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 VideoMME 评测的数据组织方式（HF datasets + 本地 videos/subtitle 目录）与 MMMU 的 TSV 方式的区别。
2. 跟踪 `build_videomme_prompt` 如何把一条标注变成「视频元素 + 题目文本」的 messages，并把视频采样预算直接写进视觉元素。
3. 讲清 `run_videomme.py infer` 与 `eval` 两个子命令的完整流程，以及「以 JSONL 文件为接口」的两段式设计为什么能解耦 GPU 与 API 资源。
4. 逐项对比 Instruct 与 Thinking 两套采样超参（temperature、top_p、presence_penalty 等），并解释差异背后的模型行为原因。
5. 独立跑通一次小规模（5 条样本）的视频推理，并检查输出 JSONL 中每条记录的字段。

## 2. 前置知识

本讲建立在两讲之上，先快速回顾关键结论：

- **u6-l1 评测三件套**：`evaluation/` 下五个基准目录结构同构——`run_*.py` 主流程、`dataset_utils.py` 数据侧、`eval_utils.py` 判分侧。run 脚本拆成 `infer`/`eval` 两个子命令；推理侧用 vLLM 批量生成并落盘 JSONL，判分侧读回 JSONL 抽取选项、汇总准确率。判分是「规则抽取优先 → LLM judge 兜底 → 随机保底」的三级瀑布。`prepare_inputs_for_vllm` 的 `prompt / multi_modal_data / mm_processor_kwargs` 三字段模式在五个脚本中逐字复用。
- **u2-l4 video metadata 与时间戳**：Qwen3-VL 以「文本-时间戳对齐」取代 Qwen2.5-VL 的 T-RoPE。开启 `return_video_metadata=True` 后，`process_vision_info` 返回的每个视频变成 `(张量, video_metadata)` 元组，metadata 携带 `fps`、`frames_indices`、`total_num_frames`，时间以绝对秒数随视觉输入显式交给模型；同时 `video_kwargs` 恒含 `do_sample_frames=False`，防止下游对已采样帧二次抽帧。
- **本讲新增的前置概念**：
  - **VideoMME**：一个大规模视频理解多选题基准，按视频时长分 short（<2 分钟）/ medium（2-15 分钟）/ long（>15 分钟）三档，每题 4 选 1，附带可选字幕（.srt）。
  - **Instruct 与 Thinking 双模式**：同一规格的 Qwen3-VL 发布 Instruct（直接作答）与 Thinking（先在 `<think>...</think>` 里推理再作答）两个独立 checkpoint，官方采样超参不同。
  - **presence_penalty（存在惩罚）**：对已出现过的 token 施加额外惩罚，值越大越抑制重复，用于治「复读机」；但会伤害长推理链，所以 Thinking 档设为 0。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `evaluation/VideoMME/run_videomme.py` | 主脚本：`infer`/`eval` 两个子命令、`prepare_inputs_for_vllm`、vLLM 批量推理、判分汇总 |
| `evaluation/VideoMME/dataset_utils.py` | 数据侧：HF datasets 加载、字幕抽取、`build_videomme_prompt` 组装 messages |
| `evaluation/VideoMME/eval_utils.py` | 判分侧：DashScope/OpenAI 兼容 judge、`can_infer` 规则抽取、逐样本评测 |
| `evaluation/VideoMME/infer_instruct.sh` | Instruct 模型官方推理脚本（采样超参的「Instruct 档」） |
| `evaluation/VideoMME/infer_think.sh` | Thinking 模型官方推理脚本（采样超参的「Thinking 档」） |
| `evaluation/VideoMME/eval_instruct.sh` / `eval_think.sh` | 两档模型各自的判分入口（参数完全同构，只是输入文件不同） |
| `evaluation/VideoMME/README.md` | 目录说明、数据准备、双模式参数建议、时长档位建议 |
| `README.md`（仓库根） | 「Evaluation Reproduction」一节给出两档模型的官方生成超参 |

## 4. 核心概念与源码讲解

### 4.1 视频数据加载与 prompt 构建

#### 4.1.1 概念说明

评测的第一步是把「一条标注」变成「模型可消费的输入」。VideoMME 的数据分两半存放：

- **标注**（题目、选项、答案、domain、时长档）：经 HuggingFace `datasets` 从 `data_dir` 加载（这与 MMMU 的「TSV + MD5 校验 + LMUData」路径不同，是三件套内部的一种变体）。
- **媒体**（视频与字幕）：按文件名约定放在本地 `videos/{videoID}.mp4` 与 `subtitle/{videoID}.srt`。

`build_videomme_prompt` 负责把两者组装成标准 messages。它的一个关键设计是：**视频采样预算（fps、帧数、像素）不写在 run 脚本的 processor 配置里，而是直接写进 messages 中 video 元素的字段**——这些字段随后由 `process_vision_info`（qwen-vl-utils）逐元素消费。这意味着不同样本理论上可以带不同的采样预算。

#### 4.1.2 核心流程

`build_videomme_prompt` 的执行流程：

```text
输入 data（一条 HF datasets 样本）
  ├─ 从 data 取 videoID/question/options/answer/... 字段
  ├─ 拼视频与字幕路径：videos/{videoID}.mp4、subtitle/{videoID}.srt
  ├─ 若 use_subtitle 且字幕存在：
  │     ├─ decord 解码视频 → 计算采样帧的时间戳列表
  │     └─ 匹配时间窗覆盖某帧时间戳的字幕 → 拼成一段文本
  ├─ 组 prompt 文本 = [可选字幕前言] + 固定作答指令 + Question + 选项 + "The best answer is:"
  ├─ 组 video_content 字典（含采样预算七字段）
  ├─ messages = [可选 system] + user([video_content, text])
  └─ 组 annotation（含剥离选项前缀的 choices、answer_id 等）→ (messages, annotation)
```

选项前缀剥离值得注意：标注里的选项形如 `"A. xxx"`，annotation 中用 `choice.split(".", 1)[1].strip()` 去掉 `"A. "` 前缀，供判分侧做**按内容匹配**（见 4.2 的 `can_infer_text`）。`answer_id = ord(answer) - 65` 把字母转下标（A→0，B→1…）。

#### 4.1.3 源码精读

数据加载与时长过滤——从 HF datasets 读 `test` 划分，只保留指定时长档的样本，直接返回 dict 列表（无 TSV、无 MD5 校验）：

[evaluation/VideoMME/dataset_utils.py:8-27](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/dataset_utils.py#L8-L27)

中文说明：`load_videomme_dataset` 用 `load_dataset(data_dir)["test"]` 加载数据集，逐条比较 `item['duration']` 是否等于传入的 `short/medium/long`，命中才收进 `total_data`。所以 `--duration` 决定评测哪一档时长的视频。

字幕侧的帧时间戳计算——独立用 decord 算一遍采样帧的时间戳，只服务于字幕匹配：

[evaluation/VideoMME/dataset_utils.py:44-58](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/dataset_utils.py#L44-L58)

中文说明：`nframes = round(duration) * fps`，再夹入 `[min_frames, max_frames]` 且不超过 `video_len // 2 * 2`（偶数对齐，呼应 u2-l3 讲过的 2 倍时间压缩）；`torch.linspace` 均匀取帧号，`get_frame_timestamp(indices)[:, 0]` 取各帧的秒级时间戳。注意：**这里的取帧只用于算时间戳窗口，真正喂给模型的抽帧稍后由 `process_vision_info` 完成**，两处公式相近但代码路径独立。

字幕匹配——字幕时间窗（毫秒转秒）覆盖任一帧时间戳即命中：

[evaluation/VideoMME/dataset_utils.py:73-87](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/dataset_utils.py#L73-L87)

中文说明：`pysubs2` 解析 .srt；条件 `sub.start/1000 < frame_timestamp and sub.end/1000 > frame_timestamp` 判断字幕时间窗是否严格覆盖某帧时刻，命中的字幕文本用空格连接。`--use-subtitle` 对应任务名里的 `w_subtitle`/`wo_subtitle`。

prompt 与视频元素的组装——预算七字段全部写进 video 元素：

[evaluation/VideoMME/dataset_utils.py:127-160](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/dataset_utils.py#L127-L160)

中文说明：先拼可选的字幕前言（"This video's subtitles are listed below:"），再接固定指令「只回答选项字母」、`Question:`、换行连接的四个选项、`The best answer is:`；`video_content` 字典携带 `min_pixels/max_pixels/min_frames/max_frames/total_pixels/fps`，这些正是 u2-l3 讲过的 qwen-vl-utils 元素级预算键；`contents` 列表**视频在前、文本在后**，顺序即模型接收顺序。

annotation 的构造：

[evaluation/VideoMME/dataset_utils.py:172-190](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/dataset_utils.py#L172-L190)

中文说明：断言答案在 A-E 中；`choices` 变成 `{"A": "选项文本", ...}` 字典（剥离 `"A. "` 前缀）；同时记录 `answer`、`answer_id`、`video_path`、`domain`、`sub_category`、`question_id`，这份 annotation 会原样写进推理产物 JSONL，是判分阶段的唯一真值来源。

#### 4.1.4 代码实践

**实践目标**：不下载任何数据、不启动模型，用一条 mock 标注验证 `build_videomme_prompt` 的输出结构。

1. 操作步骤（示例代码，放在仓库外任意目录运行，需 `pip install datasets pandas` 并把仓库内 `evaluation/VideoMME` 加入 `sys.path`）：

```python
# 示例代码：mock 一条 VideoMME 标注，观察 build_videomme_prompt 的返回
import sys
sys.path.insert(0, "<仓库路径>/evaluation/VideoMME")
from dataset_utils import build_videomme_prompt

data = {
    "videoID": "v_001", "duration": "short", "domain": "Perception",
    "sub_category": "Object Recognition", "question_id": "q001",
    "question": "What animal appears in the video?",
    "options": ["A. A cat", "B. A dog", "C. A bird", "D. A fish"],
    "answer": "B",
}
messages, annotation = build_videomme_prompt(
    data, data_dir="/tmp/VideoMME",            # 路径不存在也没关系
    use_subtitle=False, fps=2, min_frames=4, max_frames=512,
    min_pixels=3584, max_pixels=401408, total_pixels=19267584,
)
print(messages[0]["content"][0])   # video 元素
print(messages[0]["content"][1])   # 题目文本
print(annotation)
```

2. 需要观察的现象：video 元素里的预算字段是否与传参一致；题目文本是否以 "Select the best answer..." 开头、以 "The best answer is:" 结尾；`annotation["choices"]` 是否已剥离 `"A. "` 前缀。
3. 预期结果：`annotation` 形如 `{"question": ..., "choices": {"A": "A cat", ...}, "answer": "B", "answer_id": 1, ...}`。
4. 因涉及本地环境，具体输出待本地验证。

#### 4.1.5 小练习与答案

1. **练习**：为什么 `extract_video_frames_with_timestamps` 里要 `video_len // 2 * 2`？
   **答案**：把帧数上限向下取偶，保证抽出的帧数是偶数——Qwen3-VL 的视觉模型在时间维做 2 倍压缩（FRAME_FACTOR=2），奇数帧数会破坏两帧合一的配对（与 u2-l3 的 `smart_nframes` 取偶逻辑同理）。
2. **练习**：字幕是直接混进视频帧里，还是作为文本输入？
   **答案**：作为文本。字幕拼进 user 消息的 `text` 部分（视频元素之前的前言段），不与帧做任何像素级融合；`use_subtitle=False` 或字幕文件缺失时该段整体为空。
3. **练习**：`answer_id` 的计算公式对答案 "E" 会得到什么？
   **答案**：`ord('E') - 65 = 4`。annotation 断言允许 A-E，但 VideoMME 题目实际是 4 选 1，E 是兼容遗留。

### 4.2 推理与评测两段式脚本设计

#### 4.2.1 概念说明

`run_videomme.py` 用 argparse 的 **subparsers** 把一个脚本劈成两个互不重叠的命令：

- `infer`：吃模型 + 数据，吐 JSONL（GPU 密集，跑一次可能几小时）。
- `eval`：吃 JSONL，吐 CSV/TSV/acc.json（CPU + 外部 API，跑一次几分钟）。

两个阶段**以文件为唯一接口**——推理产物里的 `annotation` 字段自带判分所需的全部真值（问题、选项、答案、domain），判分阶段甚至不需要再碰数据集目录里的视频。这样 GPU 资源和 judge API 资源可以分开排队、分开重跑：推理结果可以反复用不同的 judge 复评，判分失败也不必重推。

#### 4.2.2 核心流程

`infer` 子命令主流程（`run_inference`）：

```text
load_videomme_dataset(duration) →（可选 max_samples 截断）
构造 SamplingParams（温度/top_p/top_k/max_tokens/repetition/presence）
AutoProcessor.from_pretrained(model_path)
LLM(tensor_parallel_size 自动=GPU 数, max_model_len=128000,
    limit_mm_per_prompt={"video": 1}, seed=3407)
for 每条样本: build_videomme_prompt → prepare_inputs_for_vllm → 攒进 all_inputs
outputs = llm.generate(all_inputs, sampling_params)      # 一次性批量推理
for 每条输出: 剥 </think> → 组 result 记录 → 逐行写 JSONL
```

`eval` 子命令主流程（`run_evaluation`）：

```text
逐行读 JSONL → annotation 补 prediction/index/category 字段 → DataFrame
把 choices 字典展开成 A/B/C/D 四列
build_judge(eval_model, api_type)        # dash → DashScopeWrapper / mit → OpenAIWrapper
ThreadPoolExecutor(nproc) 并发 eval_single_sample
→ 总准确率 + 按 domain（category）准确率 + 按 sub_category 准确率
→ 落盘 CSV + _acc.json + TSV
```

#### 4.2.3 源码精读

推理侧的 `SamplingParams` 组装——六个生成超参一一映射到 vLLM：

[evaluation/VideoMME/run_videomme.py:86-94](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/run_videomme.py#L86-L94)

中文说明：`temperature/top_p/top_k/max_tokens(=max_new_tokens)/repetition_penalty/presence_penalty` 全部来自命令行；这些参数的默认值就是 4.3 节要讲的「Instruct 档」。

vLLM 引擎初始化——视频评测特有的两个参数：

[evaluation/VideoMME/run_videomme.py:128-136](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/run_videomme.py#L128-L136)

中文说明：`limit_mm_per_prompt={"video": args.max_videos_per_prompt}` 限制单条 prompt 最多 1 个视频（VideoMME 每题只有一个视频）；`max_model_len=128000`、`gpu_memory_utilization=0.9`、固定 `seed=3407` 保证可复现。张量并行大小若未指定，则在 main 里自动取 `torch.cuda.device_count()`（见 [run_videomme.py:378-380](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/run_videomme.py#L378-L380)）。

「先全量构建输入、再一次 generate」的批量模式：

[evaluation/VideoMME/run_videomme.py:145-175](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/run_videomme.py#L145-L175)

中文说明：循环内只做 CPU 侧的 prompt 构建（抽帧解码也发生在这一步的 `process_vision_info` 里），攒齐 `all_inputs` 后**单次**调用 `llm.generate`，让 vLLM 的连续批处理自行调度——这是它吞吐高的根源，与逐条 `generate` 的写法有本质差别。

产物记录与 `</think>` 剥离——一条 JSONL 记录的全部字段：

[evaluation/VideoMME/run_videomme.py:187-205](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/run_videomme.py#L187-L205)

中文说明：`response_final = str(response).split("</think>")[-1].strip()`——Thinking 模型的输出是 `<think>推理…</think>答案`，取最后一个 `</think>` 之后的内容即为最终答案；Instruct 模型输出不含该标签，`split` 返回原串，同一行代码对两档模型都成立。每条记录含五字段：`question_id`、`annotation`（判分真值）、`task`（形如 `VideoMME_short_wo_subtitle`）、`result`（`gen`=剥后答案、`gen_raw`=原始输出）、`messages`。

判分侧读取 JSONL 并重建 DataFrame：

[evaluation/VideoMME/run_videomme.py:213-231](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/run_videomme.py#L213-L231)

中文说明：给 annotation 补上 `prediction`（取 `result.gen`）、`index`（取 `question_id`，用于排序）、`category`（取 `domain`，用于分组统计）；再把 `choices` 字典展开成 A/B/C/D 独立列，供 `eval_utils.build_choices` 按列扫描。

judge 构建与并发判分：

[evaluation/VideoMME/run_videomme.py:234-253](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/run_videomme.py#L234-L253)

中文说明：`build_judge` 按 `--api-type` 返回 DashScope 或 OpenAI 兼容 wrapper（凭 `CHATGPT_DASHSCOPE_API_KEY` / `MIT_SPIDER_TOKEN` 环境变量取密钥）；`ThreadPoolExecutor(max_workers=nproc)` 并发跑 `eval_single_sample`——判分是网络 IO 密集，线程池即可加速。

三级瀑布判分的入口在 eval_utils：

[evaluation/VideoMME/eval_utils.py:291-337](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/eval_utils.py#L291-L337)

中文说明：`extract_answer_from_item` 先用 `can_infer`（选项字母规则 + 选项内容匹配，见 [eval_utils.py:249-253](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/eval_utils.py#L249-L253)）做规则抽取；失败则把 judge prompt（[eval_utils.py:271-289](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/eval_utils.py#L271-L289)，带 few-shot 示例、允许输出 Z 表示无法匹配）发给 LLM judge，最多重试 25 次；仍失败则**随机**选一个选项并如实打上 `extract_flag=False` 审计标记。`eval_single_sample`（[eval_utils.py:339-362](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/eval_utils.py#L339-L362)）以 `opt == item['answer']` 记 `hit`。

准确率的三层统计与落盘：

[evaluation/VideoMME/run_videomme.py:256-299](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/run_videomme.py#L256-L299)

中文说明：总准确率是 `hit` 均值；再按 `domain`（print 逐行）与 `sub_category`（写文件）分组；输出三件——逐样本 CSV、`_acc.json` 汇总、同内容 TSV（兼容原始实现）。

四个 shell 脚本就是两段式的门面。Instruct 判分入口（Thinking 版仅 `--input-file`/`--output-file` 换成 `_thinking` 后缀，其余逐字相同）：

[evaluation/VideoMME/eval_instruct.sh:6-12](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/eval_instruct.sh#L6-L12)

中文说明：判分不需要 `--model-path`（不再加载被测模型），只需 judge 模型名、API 类型和并发数——直观体现「判分与 GPU 解耦」。

#### 4.2.4 代码实践

**实践目标**：跑通一次 5 条样本的推理，逐字段检查 JSONL 产物。

1. 实践目标：验证 infer 子命令端到端可跑，并确认产物字段与 4.2.3 描述一致。
2. 操作步骤：
   - 准备数据：按 [evaluation/VideoMME/README.md:66-81](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/README.md#L66-L81) 的目录约定放置 `videos/`、`subtitle/` 与 HF datasets 标注文件；
   - 安装依赖：`pip install -r evaluation/VideoMME/requirements.txt`（vllm、qwen_vl_utils、decord、pysubs2 等）；
   - 执行（单卡、2B Instruct、只取前 5 条）：

     ```bash
     cd evaluation/VideoMME
     python run_videomme.py infer \
         --model-path Qwen/Qwen3-VL-2B-Instruct \
         --data-dir /path/to/VideoMME \
         --duration short \
         --output-file results/videomme_debug_5samples.jsonl \
         --max-samples 5 \
         --temperature 0.7 --top-p 0.8 --top-k 20 \
         --presence-penalty 1.5
     ```

   - 检查产物：

     ```bash
     wc -l results/videomme_debug_5samples.jsonl
     python -c "import json; [print(sorted(json.loads(l).keys())) for l in open('results/videomme_debug_5samples.jsonl')]"
     ```
3. 需要观察的现象：JSONL 恰好 5 行；每行顶层键为 `['annotation', 'messages', 'question_id', 'result', 'task']`；`task` 值为 `VideoMME_short_wo_subtitle`；`result` 内含 `gen` 与 `gen_raw` 两个键。
4. 预期结果：5 行记录、字段齐全，`gen` 是剥离 `</think>` 后的短答案文本。本实践需要 GPU 与数据集，结果待本地验证。

#### 4.2.5 小练习与答案

1. **练习**：为什么推理产物要把整个 `messages` 也存进 JSONL？
   **答案**：完整留档输入，便于事后复查 prompt 组装是否正确（例如字幕是否拼进去了、预算字段是否生效），也便于复现实验；代价是文件变大（messages 含视频路径与预算，不含帧张量本体）。
2. **练习**：`eval` 子命令提供了 `--data-dir` 参数，但判分真正用到数据目录里的什么？
   **答案**：其实什么都没用到——判分所需的 question/choices/answer/domain 全部来自 JSONL 内嵌的 `annotation`；`--data-dir` 只是接口惯例保留（对比 mmmu 目录，其判分侧同样主要依赖产物内字段）。
3. **练习**：judge 随机兜底会不会污染准确率？
   **答案**：会。4 选 1 随机命中约 25%，但抽取完全失败的样本本就极少；且逐样本 CSV 里 `extraction_success=False` 与 `extraction_log` 留了审计痕迹，可以事后剔除或复核。

### 4.3 Instruct 与 Thinking 超参对照

#### 4.3.1 概念说明

同一规格的 Qwen3-VL 发布 Instruct 与 Thinking 两个独立 checkpoint，官方为它们规定了**不同的采样超参**。直觉解释：

- **Instruct** 直接输出答案，长输出主要是描述性文本，容易陷入逐字复读；`presence_penalty=1.5` 抑制已出现 token 的重复，`temperature=0.7 / top_p=0.8` 是偏收敛的采样。
- **Thinking** 先在 `<think>` 块里写长推理链——推理链中合法的重复（复述题面、回访中间结论）很常见，存在惩罚会掐断思路，因此 `presence_penalty=0.0`；`top_p=0.95 / temperature=0.6` 在稍低温度下保留更宽的候选集，兼顾推理多样性与稳定性。

#### 4.3.2 核心流程

对照方法是「三份材料互相印证」：两份 shell 脚本给出本目录的实测配置，根 README 的官方超参表给出基准口径。逐参数 diff 两份脚本，把差异写成对照表（见 4.3.3 的表格）。

#### 4.3.3 源码精读

Instruct 官方推理脚本：

[evaluation/VideoMME/infer_instruct.sh:9-25](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/infer_instruct.sh#L9-L25)

中文说明：模型路径指向 Instruct checkpoint，输出文件名无后缀；采样档为 `temperature=0.7, top_p=0.8, top_k=20, repetition_penalty=1.0, presence_penalty=1.5`；视频预算五参数（fps=2、min_pixels=3584、max_pixels=401408、min_frames=4、max_frames=512、total_pixels=19267584）与 Thinking 版完全一致——**两档模型共享同一套视觉输入配置，只有语言侧采样不同**。

Thinking 官方推理脚本：

[evaluation/VideoMME/infer_think.sh:9-26](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/infer_think.sh#L9-L26)

中文说明：除下表列出的差异外逐行相同；末尾多一个 `--tensor-parallel-size 4`（Thinking 大模型按 4 卡并行预置，Instruct 版则省略、让脚本自动取 GPU 数）。

两档对照表（★ 为差异项）：

| 参数 | infer_instruct.sh | infer_think.sh | 说明 |
| --- | --- | --- | --- |
| 模型路径 | `/path/to/Qwen3-VL-Instruct` | `/path/to/Qwen3-VL-Thinking` | ★ 独立 checkpoint |
| `--temperature` | 0.7 | 0.6 | ★ Thinking 略降温保稳 |
| `--top-p` | 0.8 | 0.95 | ★ Thinking 放宽候选集 |
| `--top-k` | 20 | 20 | 相同 |
| `--repetition-penalty` | 1.0 | 1.0 | 相同（均不启用） |
| `--presence-penalty` | 1.5 | 0.0 | ★ Instruct 防复读 / Thinking 护推理链 |
| `--max-new-tokens` | 32768 | 32768 | 相同 |
| `--tensor-parallel-size` | 未指定（自动=GPU 数） | 4 | ★ 仅 think 脚本显式指定 |
| `--output-file` | `..._predictions.jsonl` | `..._predictions_thinking.jsonl` | ★ 产物隔离 |
| fps / 像素 / 帧数 | 同左 | 同左 | 视觉配置完全一致 |

argparse 默认值就是 Instruct 档（因此裸跑 `run_videomme.py infer` 不加采样参数得到的是 Instruct 配置）：

[evaluation/VideoMME/run_videomme.py:350-362](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/run_videomme.py#L350-L362)

中文说明：注释明确写着 "aligned with MMMU/RealWorldQA for Instruct model"；`--max-new-tokens` 默认 32768、`--temperature` 0.7、`--top-p` 0.8、`--top-k` 20、`--presence-penalty` 1.5。

仓库根 README 的官方口径（可作为以上配置的出处交叉验证）：

[README.md:1142-1164](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/README.md#L1142-L1164)

中文说明：Instruct 档 `top_p=0.8, top_k=20, temperature=0.7, presence_penalty=1.5, out_seq_length=32768`；Thinking 档 `top_p=0.95, top_k=20, temperature=0.6, presence_penalty=0.0, out_seq_length=40960, seed=1234`。注意两处细节差异：官方 Thinking 的输出上限是 40960、种子 1234，而 VideoMME 的 infer_think.sh 统一用了 32768 与脚本默认 seed 3407（[run_videomme.py:348](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/run_videomme.py#L348)）——目录 README 的「Model-Specific Configurations」一节（[evaluation/VideoMME/README.md:243-269](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/README.md#L243-L269)）与 shell 脚本一致，以脚本为准。

#### 4.3.4 代码实践

**实践目标**：亲手 diff 两份脚本，把差异沉淀成自己的笔记。

1. 实践目标：确认 4.3.3 表格中的差异项，并理解每处差异的动机。
2. 操作步骤：

   ```bash
   cd evaluation/VideoMME
   diff infer_instruct.sh infer_think.sh
   ```

3. 需要观察的现象：diff 输出只命中 6 行左右——模型路径、输出文件名、temperature、top-p、presence-penalty、tensor-parallel-size；其余（含全部视频预算参数）完全一致。
4. 预期结果：与你笔记中的对照表一致；把每处差异的「为什么」写一句话（例如 presence_penalty 1.5→0.0：Thinking 的推理链需要容忍合法重复）。纯文本 diff，无环境依赖，可直接完成。
5. 延伸（可选，待本地验证）：用 4.2.4 的 5 样本流程分别以两档参数各跑一次同一模型，对比 `gen_raw` 是否出现明显复读差异——注意这不是严格的 Instruct/Thinking 对照（模型本身没换），只观察 presence_penalty 的作用。

#### 4.3.5 小练习与答案

1. **练习**：为什么 Instruct 档用 `top_p=0.8` 而 Thinking 档放宽到 `0.95`？
   **答案**：Instruct 输出短答案，收敛采样即可且更稳；Thinking 要生成数千 token 的推理链，过窄的候选集容易把模型锁死在低质量前缀上，`0.95` 在温度 0.6 的配合下保留更多可行分支。
2. **练习**：如果给 Thinking 模型误用 `presence_penalty=1.5`，预期会发生什么？
   **答案**：推理链中合法的重复（重复题目关键词、回访中间假设）会被持续惩罚，模型倾向于回避必要的信息复述，可能表现为推理跳跃、前后不自洽甚至提前截断；这也是两套脚本必须分开维护的原因。
3. **练习**：两份脚本里 `--max-new-tokens` 都是 32768，对 Thinking 够用吗？
   **答案**：VideoMME 单题推理链通常远短于 32K，够用；但根 README 官方 Thinking 口径是 40960，若迁移到推理链更长的基准（如数学类）建议按官方值上调。

### 4.4 视频多模态输入复用

#### 4.4.1 概念说明

VideoMME 的 `prepare_inputs_for_vllm` 与 mmmu 等目录**逐字同构**，是 u3-l2 讲过的仓库级标准模式在视频场景的复用。对视频评测而言有三个要点：

1. `image_patch_size=processor.image_processor.patch_size` 从 processor 动态读取（Qwen3-VL 为 16），而不是硬编码——避免 u2-l1 警告过的「传错 patch 值静默算错 token 网格」。
2. `return_video_metadata=True`：视频在返回值中变成 `(张量, video_metadata)` 元组列表，直接塞进 `multi_modal_data['video']`，由 vLLM 引擎解包——这正是 u2-l4 讲的 Qwen3-VL 时间戳机制的接入点。
3. `video_kwargs` 作为 `mm_processor_kwargs` 原样传给引擎，其中 `do_sample_frames=False` 声明「帧已在客户端采样完」；采样用的 fps/帧数/像素预算已在 4.1 中写进 video 元素、由 `process_vision_info` 消费。

#### 4.4.2 核心流程

```text
messages（含带预算的 video 元素）
  ├─ processor.apply_chat_template(tokenize=False) → 只含 <|video_pad|> 占位符的文本 prompt
  ├─ process_vision_info(messages, image_patch_size=动态, 
  │                       return_video_kwargs=True, return_video_metadata=True)
  │     └─ fetch_video：按元素预算抽帧+缩放 → (帧张量, metadata) 元组列表
  └─ 组 { prompt, multi_modal_data: {video: [...]}, mm_processor_kwargs: video_kwargs }
       → llm.generate 引擎侧解包 metadata、按 do_sample_frames=False 不再抽帧
```

#### 4.4.3 源码精读

`prepare_inputs_for_vllm` 全文：

[evaluation/VideoMME/run_videomme.py:27-58](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/run_videomme.py#L27-L58)

中文说明：三字段返回值与 mmmu 目录的实现一致；注意两点——`image_patch_size` 动态取自 `processor.image_processor.patch_size`（注释要求 qwen_vl_utils 0.0.14+）；`mm_data['video']` 里装的是带 metadata 的元组列表，vLLM 路径无需像 transformers 路径那样手工 zip 拆分（承接 u3-l2 的结论）。

像素预算的「口径」问题。argparse 默认值与 shell 脚本传值并不相同：

[evaluation/VideoMME/run_videomme.py:327-337](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/run_videomme.py#L327-L337)

中文说明：argparse 默认 `min_pixels=128*28*28=100352`、`max_pixels=512*28*28=401408`、`total_pixels=24576*28*28=19267584`，是 28 口径遗产写法；而两份官方 shell 脚本实际传 `min_pixels=3584`（即 \(128 \times 28\)，比 argparse 默认低一个 28 倍因子）、`max_pixels=401408`、`total_pixels=19267584`。**直接裸跑脚本与按官方脚本跑，视觉预算并不相同**——目录 README「Video Processing Arguments」一节写的 default 值（3584/401408/19267584，[evaluation/VideoMME/README.md:145-151](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/README.md#L145-L151)）对应的是 shell 传值而非 argparse 默认，复现时以官方 shell 脚本为准。

预算换算到 32 口径的视觉 token（估算口径）：单帧上限 \(401408 / 32^2 = 392\) 个视觉 token，全部帧总上限 \(19267584 / 32^2 / 2 = 9408\) 个视觉 token（除以 2 是时间维 2 倍压缩）——这与 `max_model_len=128000` 的窗口相比留足了文本空间，长视频场景可参照 README 的时长档位建议下调 fps（[evaluation/VideoMME/README.md:271-289](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/VideoMME/README.md#L271-L289)：short 用 fps=2，medium/long 建议 fps=1 甚至 0.5-1）。

帧预算参数的最终消费方是 qwen-vl-utils 的 `fetch_video`（承接 u2-l3）：元素里的 `fps` 决定 `smart_nframes` 的目标帧数、`min_frames/max_frames` 是夹紧区间、`total_pixels` 只压单帧分辨率不减帧数、`max_pixels` 只能往下调。

#### 4.4.4 代码实践

**实践目标**：观察一条 VideoMME messages 经过 `prepare_inputs_for_vllm` 后，视频变成什么形态、`mm_processor_kwargs` 里有什么。

1. 操作步骤（示例代码，需要 `pip install "qwen-vl-utils" transformers` 及一个可用的小视频文件）：

```python
# 示例代码：检验视频多模态输入的三字段形态
import sys
sys.path.insert(0, "<仓库路径>/evaluation/VideoMME")
from transformers import AutoProcessor
from run_videomme import prepare_inputs_for_vllm

processor = AutoProcessor.from_pretrained("Qwen/Qwen3-VL-2B-Instruct")
messages = [{
    "role": "user",
    "content": [
        {"video": "/path/to/demo.mp4", "fps": 2,
         "min_pixels": 3584, "max_pixels": 401408,
         "min_frames": 4, "max_frames": 512, "total_pixels": 19267584},
        {"text": "Select the best answer ... The best answer is:"},
    ],
}]
out = prepare_inputs_for_vllm(messages, processor)
print(type(out["multi_modal_data"]["video"][0]))   # 期望 tuple: (tensor, metadata)
print(out["multi_modal_data"]["video"][0][1])      # video_metadata: fps/frames_indices/total_num_frames
print(out["mm_processor_kwargs"])                  # 期望含 do_sample_frames=False
```

2. 需要观察的现象：`video` 列表元素是 `(torch.Tensor, dict)` 元组；metadata 含 `fps`、`frames_indices`、`total_num_frames` 等字段；`mm_processor_kwargs` 含 `do_sample_frames: False`。
3. 预期结果：帧张量形状为 `(1, T, 3, H, W)`，T 为偶数；metadata 的 `frames_indices` 长度等于 T。需本地有视频与依赖，待本地验证。
4. 思考题：如果把 `return_video_metadata=True` 去掉会怎样？（答案见练习 1。）

#### 4.4.5 小练习与答案

1. **练习**：去掉 `return_video_metadata=True`，Qwen3-VL 会少拿到什么信息？
   **答案**：少拿到 `fps / frames_indices / total_num_frames`，模型无法把帧换算成绝对秒级时间戳——文本-时间戳对齐失效，秒级事件定位类题目的表现会退化；且返回形态退回纯张量列表，`video_kwargs` 会改带 `fps` 列表（Qwen2.5-VL 兼容形态）。
2. **练习**：为什么 `mm_processor_kwargs` 里要恒带 `do_sample_frames=False`？
   **答案**：抽帧已在 `process_vision_info` 内按元素预算完成，如果引擎侧再按默认 `fps=2` 抽一次，等于对已抽的帧序列二次抽样，破坏帧数与 metadata 的一致性。「谁采样，谁决定 do_sample_frames」——离线客户端采样就传 False（承接 u3-l3 的结论）。
3. **练习**：`min_pixels=3584` 这个值换算下来不足一个 64×64 的帧，它实际上几乎不起作用，为什么脚本还要传？
   **答案**：它是从 28 口径遗产换算来的保守下限（\(128 \times 28\)），作用是防止低分辨率帧被过度放大浪费 token；实际视频帧分辨率远高于此，真正的约束来自 `max_pixels` 与 `total_pixels`，因此该值小到不绑定也无关紧要——读懂这类「无害的历史包袱」是复现官方配置时避免误改的关键。

## 5. 综合实践

**任务**：把本讲四个模块串成一次完整的「推理 → 判分」小闭环，并产出一份双模式对照笔记。

步骤：

1. **准备**（4.1）：按目录约定布置一个迷你 VideoMME 数据目录（哪怕只有 5 个 short 视频），跑通 4.1.4 的 mock 实践确认 prompt 组装正确。
2. **推理**（4.2 + 4.3）：用 `Qwen/Qwen3-VL-2B-Instruct`、`--max-samples 5`、Instruct 档采样参数执行 `infer`；检查 JSONL 的五个字段。条件允许时（GPU 充足或模型换档）再以 Thinking 档参数对同样 5 条跑一次，输出到 `_thinking.jsonl`。
3. **对照**（4.3）：`diff` 两份脚本并把你机器上两次实际使用的参数写成对照表，附上每次的 `gen_raw` 长度统计（Thinking 的原始输出应显著更长，`gen` 应基本等长）。
4. **检验输入形态**（4.4）：跑 4.4.4 的脚本，确认视频是 `(张量, metadata)` 元组、`do_sample_frames=False`。
5. **判分**（4.2）：配置 `CHATGPT_DASHSCOPE_API_KEY` 与 `DASHSCOPE_API_BASE` 环境变量后执行 `eval` 子命令（或 `eval_instruct.sh`），得到 5 样本准确率与 `_acc.json`。

预期产物：一个 JSONL、一个 CSV、一个 acc.json、一份双模式参数对照笔记。全程涉及 GPU 与外部 API，结果待本地验证；若只有 CPU，可退化为「mock 推理产物 + 真实判分」——手工构造 5 行 JSONL（`gen` 字段填 A/B/C/D），只跑第 5 步，同样能验证判分链路。

## 6. 本讲小结

- VideoMME 的数据侧变体：标注走 HF `load_dataset(data_dir)["test"]` 并按 `duration` 过滤，媒体按 `videos/{videoID}.mp4`、`subtitle/{videoID}.srt` 文件名约定定位，区别于 MMMU 的 TSV+MD5 路径。
- `build_videomme_prompt` 把 fps、帧数、三档像素预算直接写进 video 元素，采样预算随样本走、由 `process_vision_info` 逐元素消费；字幕以时间窗匹配后拼进文本前缀。
- `run_videomme.py` 用 subparsers 实现 infer/eval 两段式，以 JSONL（内嵌 annotation 真值）为唯一接口，GPU 推理与 judge API 判分彻底解耦；判分沿用「规则优先 → judge 兜底（重试 25 次）→ 随机保底」三级瀑布。
- 双模式采样差异只有四处：`temperature 0.7→0.6`、`top_p 0.8→0.95`、`presence_penalty 1.5→0.0`、think 版显式 `tensor-parallel-size 4`；视觉输入配置两档完全一致；`split("</think>")[-1]` 一行代码同时兼容两档输出。
- `prepare_inputs_for_vllm` 三字段模式逐字复用于视频场景：patch_size 动态读取、`return_video_metadata=True` 让视频变成 `(张量, metadata)` 元组、`do_sample_frames=False` 声明客户端已采样。
- 复现陷阱：argparse 默认像素预算（`128*28*28` 系）与官方 shell 脚本实际传值（`3584/401408/19267584`）不一致，且官方 Thinking 的 out_seq_length/seed 与根 README 口径略有出入——复现一律以官方 shell 脚本为准。

## 7. 下一步学习建议

- 下一讲 u6-l4 将进入 ODinW-13 检测评测：从「多选题抽取判分」换成「bbox JSON 解析 + 坐标归一化 + 检测指标」，三件套骨架不变，建议带着「哪里同构、哪里特化」的问题去读 `run_odinw.py`。
- 想深挖视频输入的底层机制，回到 u2-l3/u2-l4 重读 `vision_process.py` 的 `fetch_video` 与 metadata 合成，对照本讲 4.4 的实践观察。
- 想做自定义视频评测，可以把本目录当模板：替换 `dataset_utils.load_videomme_dataset` 与 `build_videomme_prompt` 中的题目拼接，保留 infer/eval 两段式骨架，即可快速搭起自己的视频基准。
