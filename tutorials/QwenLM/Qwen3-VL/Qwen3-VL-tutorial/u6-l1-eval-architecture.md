# 评测框架通用架构：run/dataset_utils/eval_utils 三件套

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `evaluation/` 下每个基准目录的「三件套」——`run_*.py`、`dataset_utils.py`、`eval_utils.py`——各自负责什么，以及可选的 `common_utils.py` 放什么。
2. 跟踪一条完整数据流：TSV 数据集下载 → prompt 组装 → vLLM 批量推理 → JSONL 落盘 → 规则抽取 + LLM 兜底判分 → 准确率汇总。
3. 理解判分侧的「三级瀑布」设计：规则抽取优先、OpenAI 兼容 API judge 兜底、随机兜底保底，以及为什么这样分层。
4. 识别出 `prepare_inputs_for_vllm` 模式（u3-l2 建立）在五个评测脚本中的逐字复用，从而掌握「读通一个目录 = 读通五个目录」的学习杠杆。

## 2. 前置知识

本讲处于 advanced 层，假设你已完成单元一至三。用到的概念先通俗补齐：

- **TSV 与 DataFrame**：评测数据集以制表符分隔的 `.tsv` 文件分发，用 pandas 的 `pd.read_csv(path, sep='\t')` 读成 DataFrame——可以把它理解为一张带列名的表格，每行一道题，列如 `index`、`question`、`A/B/C/D`、`answer`、`image`。
- **base64 内嵌图像**：TSV 的 `image` 列存的是图像的 base64 字符串（一大串文本形式的图片），运行时解码回 PNG/JPG 写到磁盘。一张 1M 像素的图 base64 后远超 64 字符，这个特征后面会被用来做「指针识别」。
- **LMUData 约定**：所有评测脚本用环境变量 `LMUData` 指定数据根目录，数据集 TSV 与导出的图片都放在它下面。这是从 VLMEvalKit 沿用下来的目录约定。
- **JSONL**：每行一个 JSON 对象的文本文件。推理阶段的产物（每题一条记录）用它保存，判分阶段再逐行读回。
- **LLM-as-a-judge（判分模型）**：当被测模型的回答不是干净的选项字母（例如回答了一段文字描述），需要一个外部 LLM 把「自由文本回答」映射回「A/B/C/D/Z 选项」。judge 通过 OpenAI 兼容的 HTTP API 调用（DashScope 或自建端点），`temperature=0` 保证稳定。
- **vLLM 离线批量推理**：u3-l2 讲过的模式——`LLM(...)` 引擎进程内 `llm.generate(所有输入)` 一次性返回全部输出，GPU 利用率远高于逐条请求。本讲的 `run_inference` 就是它的工业化应用。
- **argparse 子命令**：`python run_mmmu.py infer ...` 与 `python run_mmmu.py eval ...` 是同一个脚本的两个子命令，推理与判分被拆成两个独立阶段，中间以 JSONL 文件解耦。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [evaluation/mmmu/run_mmmu.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py) | 主流程：`infer` 子命令做 vLLM 推理并落盘 JSONL，`eval` 子命令做判分与汇总；内含 `build_mmmu_prompt` 与 `prepare_inputs_for_vllm` |
| [evaluation/mmmu/dataset_utils.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/dataset_utils.py) | 数据侧：数据集下载校验、TSV 解析、base64 图像导出、开放题改选择题 |
| [evaluation/mmmu/eval_utils.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py) | 判分侧：两个 API Wrapper、`build_judge` 工厂、规则抽取 `can_infer` 系列、`eval_single_sample` |
| [evaluation/mmmu/common_utils.py](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/common_utils.py) | 公共工具：base64 编解码、文件下载、MD5、字符串转列表 |

目录层面的事实（已用 `Glob` 核实）：`evaluation/` 下有 **5 个基准目录**——`mmmu`、`MathVision`、`ODinW-13`、`RealWorldQA`、`VideoMME`——每个目录都齐备 `run_*.py + dataset_utils.py + eval_utils.py` 三件套与 `infer_instruct.sh / infer_think.sh / eval_instruct.sh / eval_think.sh` 四个双档脚本；其中 `mmmu`、`MathVision`、`RealWorldQA` 额外抽出了 `common_utils.py`，`ODinW-13` 与 `VideoMME` 没有。所以「三件套」是硬约定，`common_utils` 是可选的第四件。

## 4. 核心概念与源码讲解

### 4.1 run_*.py 主流程：一条脚本、两个阶段

#### 4.1.1 概念说明

`run_mmmu.py` 是 MMMU 评测的总指挥。它把评测拆成两个可以分开重跑的阶段：

- **infer（推理）**：装载数据集 → 逐题组装 prompt → vLLM 批量生成 → 把「每题的原始标注 + 模型输出」写成 JSONL。此阶段吃 GPU。
- **eval（判分）**：读回 JSONL → 从自由文本里抽出选项字母 → 与标准答案比对 → 输出逐题 CSV 和准确率 JSON。此阶段吃 API 网络。

两阶段用文件解耦的好处：推理很贵（GPU 小时），判分很便宜但可能要反复调（换 judge 模型、修抽取规则）。分开后调判分不必重跑推理。

#### 4.1.2 核心流程

`infer` 子命令的执行序列：

1. `main()` 解析子命令与参数；`--data-dir` 写入环境变量 `LMUData`。
2. `run_inference(args)`：`load_dataset` 拿到 DataFrame。
3. 构造 `dump_image_func`（把每行图像导出到 `LMUData/images/MMMU/`）。
4. 组装 `SamplingParams`，加载 `AutoProcessor`，初始化 vLLM `LLM` 引擎。
5. 循环每行：`build_mmmu_prompt` 出 messages → 可选拼接 CoT 后缀 → `prepare_inputs_for_vllm` 出三字段输入。
6. `llm.generate(all_inputs, sampling_params)` 一次性批量推理。
7. 逐条取 `output.outputs[0].text`，剥掉 `</think>` 前缀，连同标注一起写 JSONL。

`eval` 子命令的执行序列：

1. 逐行读 JSONL，把 `job["result"]["gen"]` 注回 `annotation["prediction"]`。
2. 重新 `load_dataset` 拿元数据，断言推理文件是数据集的子集。
3. `MMMU_preproc` 把开放题改写成选择题，构造 `GT`（标准答案）列。
4. `build_judge` 造判分模型，把 `(model, item)` 打包成任务列表。
5. 线程池并发跑 `eval_single_sample`（环境变量 `DEBUG=true` 时改为单线程前 5 条）。
6. 汇总总体与分学科准确率，落盘 CSV + `_acc.json`。

#### 4.1.3 源码精读

**依赖装配**：主脚本从三件套的另外两件导入函数，同时引入 vLLM 与 qwen-vl-utils——这正是 u3-l2 建立的调用链被评测脚本复用的证据：

- [evaluation/mmmu/run_mmmu.py:L16-L22](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L16-L22)：导入 `LLM`/`SamplingParams`、`process_vision_info`、`AutoProcessor`，并从 `dataset_utils` 与 `eval_utils` 拿来 `load_dataset`、`dump_image`、`MMMU_preproc`、`build_judge`、`eval_single_sample`。注意这是平级目录内的裸导入（没有包前缀），所以脚本必须在 `evaluation/mmmu/` 目录下运行。

**双阶段入口与子命令分发**：

- [evaluation/mmmu/run_mmmu.py:L367-L432](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L367-L432)：`main()` 用 `add_subparsers` 注册 `infer` 与 `eval` 两个子命令；[L419-L420](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L419-L420) 把 `--data-dir` 写入 `os.environ['LMUData']`；[L423-L425](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L423-L425) 在未显式指定时把张量并行度自动设为可见 GPU 数；最后 [L427-L432](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L427-L432) 按子命令分发到 `run_inference` / `run_evaluation`。

> 读码提示：`--data-dir` 的 help 文案写的是「tsv 文件的绝对路径」，但代码实际把它当**目录**用（`LMUData` 根）。以代码行为为准，help 文案不可尽信；同类问题还有 [L391-L392](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L391-L392) 注释写 default 2048、实际默认 32768。

**推理主循环与产物格式**：

- [evaluation/mmmu/run_mmmu.py:L137-L145](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L137-L145)：Instruct 档默认采样参数 `temperature=0.7 / top_p=0.8 / top_k=20 / presence_penalty=1.5 / max_tokens=32768`（Thinking 档的差异留给 u6-l3）。
- [evaluation/mmmu/run_mmmu.py:L172-L180](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L172-L180)：vLLM 引擎初始化——张量并行、显存占比 0.9、`max_model_len` 默认 128000、`limit_mm_per_prompt` 限制单 prompt 最多 10 张图、`seed=42` 保证可复现。
- [evaluation/mmmu/run_mmmu.py:L189-L212](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L189-L212)：先离线准备好**全部**输入（把 numpy 标量顺带转成 Python 原生类型），攒成 `all_inputs` 列表——这是 vLLM 批量推理的正确姿势：先备货、再一次性 `generate`。
- [evaluation/mmmu/run_mmmu.py:L222](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L222)：`outputs = llm.generate(all_inputs, sampling_params=sampling_params)` 一行完成全部推理。
- [evaluation/mmmu/run_mmmu.py:L235-L246](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L235-L246)：每条输出取 `output.outputs[0].text`，用 `str(response).split("</think>")[-1].strip()` 剥掉思维链前缀（非 thinking 模型没有该标记，切分结果就是原文）；JSONL 记录四键：`question_id`、`annotation`（整行原始标注）、`task`、`result`（`gen` 为净化文本、`gen_raw` 为原文）、`messages`。把标注整体存进产物，是判分阶段无需重新对齐数据集的关键。

**判分主流程**：

- [evaluation/mmmu/run_mmmu.py:L261-L266](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L261-L266)：读 JSONL，把模型输出注入 `annotation["prediction"]`，还原成 DataFrame。
- [evaluation/mmmu/run_mmmu.py:L283-L292](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L283-L292)：以 `index` 对齐推理文件与数据集元数据（断言子集关系），构造 `answer_map` 与 `GT` 列；不在 A–Z 内的标准答案统一记作 `'A'`。
- [evaluation/mmmu/run_mmmu.py:L299-L331](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L299-L331)：`build_judge` 造判分模型 → 打包 `(model, item)` 任务 → `DEBUG=true` 时单线程跑前 5 条便于排错，正常模式用 `ThreadPoolExecutor(max_workers=nproc)` 并发（判分是网络 I/O 型，线程池即可，无需多进程）。
- [evaluation/mmmu/run_mmmu.py:L333-L359](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L333-L359)：总体准确率按 \[ \text{accuracy} = \frac{\sum_i \text{hit}_i}{N} \] 计算，再按 `split`（MMMU 的学科分组）分桶统计，输出逐题 CSV 与 `*_acc.json`。

#### 4.1.4 代码实践

1. **实践目标**：不跑模型，仅用「读码 + 干跑 CLI」确认双阶段结构。
2. **操作步骤**：
   - 打开 [evaluation/mmmu/run_mmmu.py:L367-L432](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L367-L432)，给 `infer` 子命令的每个 `add_argument` 标注：属于「模型/vLLM/生成/数据」哪一组、默认值是什么。
   - 在装有 vllm 的环境里执行 `cd evaluation/mmmu && python run_mmmu.py --help && python run_mmmu.py infer --help && python run_mmmu.py eval --help`，对照 help 输出与你的标注表。
3. **需要观察的现象**：help 里的 default 值；`--presence-penalty` 与 `--max-new-tokens` 的默认值是否与源码一致。
4. **预期结果**：help 显示 `--max-new-tokens` 默认 32768、`--presence-penalty` 默认 1.5，与 [L391-L402](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L391-L402) 一致，从而验证「docstring 注释会过期、参数默认值才是真相」。
5. 本实践依赖 vllm 导入成功（[L16](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L16) 在模块顶层），未装环境时输出报 ImportError——属预期现象，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么推理和判分要拆成两个子命令，而不是一个命令跑完？
**答案**：两者资源模型不同（推理吃 GPU、判分吃网络 API）且重复运行频率不同（判分规则或 judge 模型常需迭代）。拆开后以 JSONL 文件解耦，重跑判分不必重烧 GPU，也便于人工抽查中间产物。

**练习 2**：`run_inference` 里为什么要先把所有输入攒进 `all_inputs` 再一次性 `llm.generate`，而不是边循环边 generate？
**答案**：vLLM 的吞吐优势来自批内调度（continuous batching）：一次性提交全部请求，引擎可以动态拼批填满 GPU；逐条 generate 则退化为串行，浪费显存与算力。参见 [L183-L222](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L183-L222)。

**练习 3**：JSONL 产物里为什么要同时保存 `gen` 和 `gen_raw` 两个版本的输出？
**答案**：`gen_raw` 保留 thinking 模型的完整思维链，`gen` 是剥掉 `</think>` 前缀的最终回答（[L238](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L238)）。判分用 `gen`，排查误判时需要看 `gen_raw` 里模型的推理过程。

### 4.2 dataset_utils 数据侧：下载、解析与图像导出

#### 4.2.1 概念说明

`dataset_utils.py` 负责「把外部世界的评测数据变成脚本内部统一形态」：确定数据从哪下载、如何校验、TSV 里那些 base64 图像如何落到磁盘、以及把少量开放题改写成选择题以统一判分流程。它隔离了**数据集的异质性**——无论上游数据长什么样，`run_*.py` 拿到的永远是规整的 DataFrame 与磁盘上的图片路径。

#### 4.2.2 核心流程

`load_dataset` 的五步：

1. 以 `LMUData` 为根，拼出 `<数据集名>.tsv` 的本地路径。
2. 文件不存在或 MD5 与常量不符 → 从 `MMMU_DATASET_URL` 重新下载。
3. `pd.read_csv(sep='\t')` 读入，`index` 列先统一转字符串。
4. **图像指针消解**：`image` 列若某行长度 ≤ 64，说明它存的不是图像数据而是**另一行的 index**（复用图像），查表替换。
5. `toliststr` 把 `"[a, b]"` 形式的字符串还原成列表（多图样本），`index` 能转 int 则转 int。

`dump_image` 的三分支伪代码：

```text
if 行里有 image:
    if image 是列表:      # 多图
        对每个 (base64, 文件名) 组合: 不存在则解码写盘
    else:                 # 单图
        解码写到 <img_root>/<index>.jpg
else:                     # 数据集直接给了路径
    toliststr(image_path) 原样返回
返回 本地图片路径列表
```

`MMMU_preproc` 的改写规则：对每个标准答案不在 A–Z 的开放题，把它的答案塞进 `A` 列、`B` 列填 `'Other Answers'`——开放题就被改写成「二选一」，与选择题共用同一套判分管线。

#### 4.2.3 源码精读

**下载源与校验和**：

- [evaluation/mmmu/dataset_utils.py:L7-L8](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/dataset_utils.py#L7-L8)：数据集 URL 指向 OpenCompass 托管的 `MMMU_DEV_VAL.tsv`，配 MD5 常量做完整性校验。

**load_dataset 主体**：

- [evaluation/mmmu/dataset_utils.py:L10-L21](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/dataset_utils.py#L10-L21)：`data_root = os.path.join(os.environ['LMUData'])`，文件缺失或 [L19](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/dataset_utils.py#L19) 的 `md5(data_path) != MMMU_DATASET_MD5` 触发重新下载——注意这意味着**自制 tsv 会被校验失败逻辑覆盖重下**，做实验时要意识到这一点。
- [evaluation/mmmu/dataset_utils.py:L30-L40](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/dataset_utils.py#L30-L40)：图像指针消解——base64 图像动辄上万字符，长度 ≤ 64 的 cell 必然是另一行的 index；`image_map` 查表替换后，多图字符串经 `toliststr` 展开为列表，单图保持字符串。

**dump_image**：

- [evaluation/mmmu/dataset_utils.py:L53-L75](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/dataset_utils.py#L53-L75)：解码写盘前先 `os.path.exists` 判断，已导出则直接复用（幂等，重复跑不重复解码）；多图分支要求行内同时有 `image_path` 列提供文件名；没有 `image` 列时退回 `image_path`。返回的恒为**列表**（单图也包一层），`build_mmmu_prompt` 因此可以统一按列表或单值两种形态处理。
- 底层的 `decode_base64_to_image_file` 在 [evaluation/mmmu/common_utils.py:L31-L34](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/common_utils.py#L31-L34)：base64 → PIL Image → 保存文件，三行完成。

**MMMU_preproc**：

- [evaluation/mmmu/dataset_utils.py:L77-L94](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/dataset_utils.py#L77-L94)：统计并打印改写题数（`cnt`），`As[i] = Ans[i]`、`Bs[i] = 'Other Answers'`。这保证判分侧永远在做「字母比对」，无需为开放题写第二套判分逻辑。

顺带认识 `common_utils.py` 的其余工具：[encode_image_to_base64](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/common_utils.py#L9-L24)（可按长边等比缩放，供 judge 上传图像用）、[download_file](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/common_utils.py#L36-L43)、[md5](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/common_utils.py#L45-L51)、[toliststr](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/common_utils.py#L53-L60)。其中 `toliststr` 对 `"[...]"` 字符串直接 `eval`——在这类受控数据场景可行，但自己写代码时应对未知输入改用 `ast.literal_eval`。

#### 4.2.4 代码实践

1. **实践目标**：亲手复现「图像指针消解」逻辑，理解 ≤64 字符判定。
2. **操作步骤**（以下为示例代码，可在任意有 pandas 的机器运行）：

```python
# 示例代码：模拟 load_dataset 的 image 指针消解（不含下载环节）
import pandas as pd, io
tsv = "index\tquestion\timage\n1\tQ1\t" + "BASE64_" + "x"*200 + "\n2\tQ2\t1\n"
data = pd.read_csv(io.StringIO(tsv), sep='\t')
image_map = {x: y for x, y in zip(data['index'], data['image'])}
for k in image_map:
    if len(image_map[k]) <= 64:           # 短 cell = 指向另一行的 index
        idx = image_map[k]
        image_map[k] = image_map[idx]      # 查表替换为真实 base64
print({k: len(v) for k, v in image_map.items()})
```

3. **需要观察的现象**：index=2 的 `image` 原本是字符串 `"1"`，替换后长度变成与 index=1 相同的 200+。
4. **预期结果**：打印出 `{1: 207, 2: 207}`（长度数值因前缀而异，重点看两行相等），证明第 2 题成功复用了第 1 题的图像。待本地验证。
5. 若想跑真实 `load_dataset`：`export LMUData=/tmp/mmmu_data` 后调用会因 MD5 校验触发约几百 MB 的官方 tsv 下载（首跑属正常行为）。

#### 4.2.5 小练习与答案

**练习 1**：为什么用「长度 ≤ 64」判断 image 列存的是指针而不是数据？
**答案**：真实图像的 base64 编码最短也远超 64 字符（几十像素的图也上千字符），而另一行的 index 只是几个字符。这是不用改数据格式就能实现「多题共用一图」的低成本约定，见 [L33-L37](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/dataset_utils.py#L33-L37)。

**练习 2**：`dump_image` 为什么每次都要 `os.path.exists` 检查？
**答案**：判分阶段会重新 `load_dataset`，若再走推理流程会重复调用 `dump_image`；先查存在性使导出幂等，避免对几千行数据反复 base64 解码与写盘（[L63-L64](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/dataset_utils.py#L63-L64)、[L67-L69](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/dataset_utils.py#L67-L69)）。

**练习 3**：开放题（答案不是字母）如果不经 `MMMU_preproc` 改写，判分侧会发生什么？
**答案**：`can_infer` 只认 A–Z 选项字母，自由文本答案永远抽不出与 `GT` 可比的选项，这些题会全部误判。`MMMU_preproc` 把答案本身作为选项 A，使所有题共用字母比对管线（[L84-L93](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/dataset_utils.py#L84-L93)）。

### 4.3 eval_utils 判分侧：规则优先、API 兜底、随机保底

#### 4.3.1 概念说明

判分要解决的核心问题是：模型的回答是自由文本（"The answer is B"、"图中是一只泰迪熊"……），而标准答案是单个字母。`eval_utils.py` 设计了一个**三级瀑布**：

1. **规则抽取（免费、确定）**：字符串匹配直接从回答里找选项字母或选项文本。
2. **LLM judge 兜底（付费、需网络）**：规则失败时，把「问题 + 选项 + 回答」发给一个 OpenAI 兼容 API 的 judge 模型，让它输出最相似的选项字母。
3. **随机兜底（保底不崩溃）**：judge 也失败（重试 25 次无果）时随机选一个，保证程序不因单题卡死，并在日志里标记 `extract_flag=False`。

绝大多数回答第一级就解决，judge 只处理长尾——这是典型的「快路径/慢路径」工程分层。

#### 4.3.2 核心流程

`eval_single_sample(task)` 的展开：

```text
eval_single_sample((model, item)):
    result = extract_answer_from_item(model, item)
        choices = build_choices(item)                # {'A': ..., 'B': ...}
        ret = can_infer(item.prediction, choices)    # 第一级：规则
        if ret:  return (opt=ret, extract_model='rule', ...)
        loop 25 次:                                    # 第二级：LLM judge
            ans = model.generate([judge prompt])
            ret = can_infer(ans, choices)
            if ret 且 ret != 'Z': return (opt=ret, extract_model=model.model, ...)
            随机退避 sleep
        return (opt=random.choice(选项+Z), extract_flag=False)   # 第三级
    hit = 1 if result.opt == item.GT else 0
    return {index, split, question, prediction, extracted_answer,
            extraction_method, extraction_success, extraction_log, gt, hit}
```

`can_infer` 本身也是两级：`can_infer_option`（把标点替换成空格后按词元找独立选项字母）失败时再走 `can_infer_text`（把回答与选项都小写后做子串匹配）。

judge 的构造由 `build_judge(model, api_type)` 工厂完成：`api_type='dash'` 走 DashScope（阿里云，国内推荐），`'mit'` 走任意 OpenAI 兼容自建端点；两者密钥与地址全部来自环境变量。

#### 4.3.3 源码精读

**API Wrapper 与工厂**：

- [evaluation/mmmu/eval_utils.py:L13-L70](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L13-L70)：`OpenAIWrapper.generate` 把内部消息格式（`{type, value}`）转成 OpenAI chat 格式，文本转 `text` 块、图像转 `image_url` 的 base64 data URL（[L36-L43](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L36-L43)）；payload 固定 `max_tokens=4096, temperature=0`（[L45-L50](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L45-L50)）；重试循环失败后返回哨兵串 `'Failed to obtain answer via API.'` 而不是抛异常——让单次网络故障不中断整批评测。
- [evaluation/mmmu/eval_utils.py:L72-L156](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L72-L156)：`DashScopeWrapper` 与前者结构同构，差异在 payload 用 `max_completion_tokens`/`n`/`stream`（[L104-L111](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L104-L111)），并检查 `finish_reason` 必须是 `stop`/`function_call`（[L126-L130](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L126-L130)），网络异常分类捕获后统一退避重试。
- [evaluation/mmmu/eval_utils.py:L158-L169](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L158-L169)：`build_judge` 工厂——`'mit'` 读 `MIT_SPIDER_TOKEN`/`MIT_SPIDER_URL` 构造 `OpenAIWrapper`（OpenAI 兼容兜底端点），`'dash'` 读 `CHATGPT_DASHSCOPE_API_KEY`/`DASHSCOPE_API_BASE` 构造 `DashScopeWrapper`。

**规则抽取**：

- [evaluation/mmmu/eval_utils.py:L171-L210](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L171-L210)：`can_infer_option` 先排除 API 失败哨兵与拒答话术（返回 `'Z'` 表示「都不像」），再把 `.()[],:;!*#{}` 等标点替换为空格、按空白切词元，统计有多少个选项字母以**独立词元**出现：恰好 1 个即返回它；1 个都没有且只匹配到 `Z` 则返回 `'Z'`。其中 [L203-L205](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L203-L205) 有个反直觉启发式：若词元里出现 `A` 且句子很长，`A` 很可能是冠词（"a plan"）而非选项，宁可返回 False 交给 judge。
- [evaluation/mmmu/eval_utils.py:L212-L231](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L212-L231)：`can_infer_text` 把选项内容小写后在回答里做子串匹配（回答"图中是一只泰迪熊"↔选项"A. teddy bear"），唯一命中才返回；`can_infer` 按先 option 后 text 组合两者。

**judge prompt 与抽取主函数**：

- [evaluation/mmmu/eval_utils.py:L247-L264](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L247-L264)：`build_prompt` 的 judge 模板给了三个 few-shot 示例（含「答 Spider → 输出 Z」的负例），并明确要求只输出 A/B/C/D/Z 单个大写字母——把开放生成约束成分类任务。
- [evaluation/mmmu/eval_utils.py:L266-L311](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L266-L311)：`extract_answer_from_item` 即三级瀑布的实现。规则命中直接返回 `extract_model='rule'`（[L273-L284](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L273-L284)）；judge 循环 `retry=25` 次、每次随机退避 `random.random() * wait * 2` 秒（[L292-L306](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L292-L306)）；彻底失败则 `random.choice(选项+['Z'])` 并把 `extract_flag` 置 False 留下审计痕迹（[L308-L311](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L308-L311)）。
- [evaluation/mmmu/eval_utils.py:L313-L334](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L313-L334)：`eval_single_sample` 打包最终记录：`extracted_answer`、`extraction_method`（'rule' 或 judge 模型名）、`extraction_success`、`extraction_log` 与 `hit = 1 if opt == GT else 0`——判分过程全程可审计，复查误判时不必猜。

#### 4.3.4 代码实践

1. **实践目标**：用纯函数 `can_infer` 做离线抽取实验，验证规则级瀑布的行为（不需要任何 API key 与 GPU）。
2. **操作步骤**：

```bash
cd evaluation/mmmu
python -c "
from eval_utils import can_infer
choices = {'A': 'teddy bear', 'B': 'rabbit', 'C': 'cat', 'D': 'dog'}
print(can_infer('The answer is B.', choices))
print(can_infer('a cute teddy bear', choices))
print(can_infer('Spider', choices))
print(can_infer('I choose A and C', choices))
"
```

3. **需要观察的现象**：四个调用分别走 option 匹配、text 匹配、返回 `'Z'`、返回 `False` 中的哪条路径。
4. **预期结果**：依次输出 `B`（"B." 标点被替换后成独立词元）、`A`（选项文本子串匹配）、`Z`（无任何匹配且仅 Z 命中）、`False`（两个选项同时出现，歧义拒绝）。待本地验证。
5. 若第 4 例输出与预期不符，回到 [L171-L231](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L171-L231) 逐行标注你自己的推理再对照。

#### 4.3.5 小练习与答案

**练习 1**：judge 请求为什么强制 `temperature=0`？
**答案**：判分必须可复现——同一份推理结果多次评测应得到同一准确率。`temperature=0` 让 judge 输出近似确定性，见 [L49](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L49) 与 [L109](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L109)。

**练习 2**：API 失败时返回哨兵字符串而不是抛异常，这个设计带来了什么连锁反应？
**答案**：`extract_answer_from_item` 靠检测哨兵串 `'Failed to obtain answer via API'` 决定是否重试（[L295](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L295)），`can_infer_option` 也先检查它（[L173](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L173)）。单题失败被限制在题目粒度，线程池里其他题目照常判分，整批评测不会因一次网络抖动崩溃。

**练习 3**：最后一级「随机选一个」不会污染准确率吗？
**答案**：会有轻微影响，但仅在 judge 连续 25 次失败后触发，且记录里 `extract_success=False` 与日志明确标注（[L308-L311](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/eval_utils.py#L308-L311)）。工程上「带审计标记的降级」优于「单点卡死整批任务」；复查时可用 CSV 过滤掉 `extraction_success=False` 的行重算净准确率。

### 4.4 vLLM 输入准备复用：prepare_inputs_for_vllm 的工业化拷贝

#### 4.4.1 概念说明

u3-l2 建立过 `prepare_inputs_for_vllm` 三件套字段模式：`prompt`（chat template 渲染后的文本）+ `multi_modal_data`（图像/视频张量）+ `mm_processor_kwargs`（视频采样参数）。本讲的关键发现是：**这个函数被五个评测脚本逐字复制**——已用检索核实，`def prepare_inputs_for_vllm` 同名同构地出现在 `run_mmmu.py:76`、`run_realworldqa.py:26`、`run_mathv.py:83`、`run_videomme.py:27`、`run_odinw.py:28`。这说明官方把「u3-l2 的模式」当作稳定惯用法（idiom）在维护：各基准目录彼此独立、不共享库代码，复制而非 import，换取每个目录的自包含可运行性。

#### 4.4.2 核心流程

函数三步（与 u3-l2 完全一致，此处标注评测语境下的要点）：

1. `processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)` → 只含视觉占位符的纯文本 prompt。
2. `process_vision_info(messages, image_patch_size=processor.image_processor.patch_size, return_video_kwargs=True, return_video_metadata=True)` → 图像列表、视频（张量, metadata）元组列表、video_kwargs。`image_patch_size` 从 processor **动态读取**而非硬编码，是 u2-l1 强调的最佳实践在此处的落地；`do_sample_frames=False` 藏在 video_kwargs 里防止引擎二次抽帧。
3. 组装 `{'prompt': ..., 'multi_modal_data': ..., 'mm_processor_kwargs': video_kwargs}` 返回，攒进 `all_inputs` 后交 `llm.generate`。

另外一个评测专属细节：MMMU 的像素预算写在 `build_mmmu_prompt` 里而非 processor 上——每个 image 元素自带 `min_pixels/max_pixels`，走的是 u2-l2 讲过的「元素级预算」通道。

#### 4.4.3 源码精读

- [evaluation/mmmu/run_mmmu.py:L30-L31](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L30-L31)：`MIN_PIXELS = 1280*28*28`（约 100 万像素）、`MAX_PIXELS = 5120*28*28`（约 400 万像素）。注意这是 28 口径的历史写法——像素数本身与 patch 无关，但对 Qwen3-VL（patch 16、对齐因子 32）换算视觉 token 要除以 \(32 \times 32 = 1024\)：单图上限约 \(4014080 / 1024 \approx 3920\) 个视觉 token。
- [evaluation/mmmu/run_mmmu.py:L50-L66](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L50-L66)：图像元素（单图或多图）逐个携带 `min_pixels`/`max_pixels` 键，文本元素（Hint + Question + Options + 作答指令，见 [L36-L47](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L36-L47)）垫后——正是 u1-l3 学过的标准 messages 形态。
- [evaluation/mmmu/run_mmmu.py:L76-L107](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L76-L107)：`prepare_inputs_for_vllm` 全文 30 行。[L87](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L87) 渲染文本；[L90-L95](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L90-L95) 解码视觉素材（`image_patch_size=processor.image_processor.patch_size` 动态取值）；[L97-L101](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L97-L101) 按有无可选地填充 `multi_modal_data`；[L103-L107](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L103-L107) 返回三字段字典。
- [evaluation/mmmu/run_mmmu.py:L208](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L208) 与 [L222](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L222)：调用点——每个 prompt 独立调用一次函数，随后统一交给 `llm.generate`。

#### 4.4.4 代码实践

1. **实践目标**：用只读 diff 量化五个目录对 `prepare_inputs_for_vllm` 的复制程度。
2. **操作步骤**：

```bash
cd evaluation
diff <(sed -n '/def prepare_inputs_for_vllm/,/^def /p' mmmu/run_mmmu.py | head -35) \
     <(sed -n '/def prepare_inputs_for_vllm/,/^def /p' RealWorldQA/run_realworldqa.py | head -35)
diff <(sed -n '/def prepare_inputs_for_vllm/,/^def /p' mmmu/run_mmmu.py | head -35) \
     <(sed -n '/def prepare_inputs_for_vllm/,/^def /p' VideoMME/run_videomme.py | head -35)
```

3. **需要观察的现象**：diff 输出的差异行数；差异集中在哪些行（提示：关注 docstring 与 VideoMME 里视频处理相关分支）。
4. **预期结果**：mmmu 与 RealWorldQA 的函数体几乎零差异；VideoMME 因必须处理视频输入会有少量分支差异，但三字段返回结构与 `image_patch_size` 动态读取完全一致。待本地验证。
5. 结论请写成一句话放进笔记，例如「prepare_inputs_for_vllm 是仓库级稳定惯用法，评测目录以复制方式复用」。

#### 4.4.5 小练习与答案

**练习 1**：为什么五个目录选择「复制函数」而不是抽成一个共享模块 import？
**答案**：每个基准目录保持自包含（复制 `requirements.txt`、`common_utils.py` 也是同样思路），用户只需进入单个目录 `pip install -r requirements.txt` 即可跑通该基准，不引入跨目录的包结构；代价是惯用法升级时要多处同步。这是「独立分发」与「代码复用」间的取舍。

**练习 2**：如果某评测目录忘了传 `image_patch_size=16`，会发生什么？
**答案**：不会报错。`process_vision_info` 会按旧的 patch=14 口径（对齐因子 28）计算 token 网格，视觉 token 数被静默算错——这正是 u2-l1 指出过的「传错不报错、只静默算错」，所以本函数从 processor 动态读取 patch_size（[L92](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L92)）是防御性写法。

**练习 3**：评测脚本把像素预算放在 messages 的 image 元素上（`min_pixels`/`max_pixels`），而不是改 `processor.image_processor.size`，两者区别是什么？
**答案**：前者是元素级预算，只影响该样本的图像，且随样本一起序列化进 JSONL 的 `messages` 字段留档（u2-l2）；后者是 processor 级预算，影响所有走该 processor 的输入（u3-l1）。评测中每题图像数量不一、又要按基准统一分辨率口径，元素级写法让 prompt 自包含、可复现。

## 5. 综合实践

**任务：画出 MMMU 评测全链路数据流图（本讲规格指定的实践）。**

以 `evaluation/mmmu` 为例，把从数据集加载到判分汇总的每个环节标注「所在文件 + 函数 + 输入输出」，产出一张图（Mermaid 或 ASCII 均可）加一段 200 字说明。参考答案骨架（请自己补全每个箭头携带的数据形态）：

```text
┌─ infer 阶段（GPU）──────────────────────────────────────────────┐
│ dataset_utils.load_dataset ──DataFrame──▶ run_mmmu.build_mmmu_prompt
│      │ 下载 tsv / md5 校验 / 图像指针消解        │ 组装 messages(含元素级像素预算)
│      ▼                                           ▼
│ dataset_utils.dump_image ──图片路径──▶ run_mmmu.prepare_inputs_for_vllm
│   (base64→磁盘, 幂等)                    │ 三字段: prompt / multi_modal_data /
│                                          ▼        mm_processor_kwargs
│                            vllm.LLM.generate(all_inputs, SamplingParams)
│                                          │ outputs[].outputs[0].text
│                                          ▼
│                     run_mmmu.run_inference 落盘 predictions.jsonl
│                       (annotation + gen/gen_raw + messages)
└──────────────────────────────────────────────────────────────────┘
                              │ 文件解耦
┌─ eval 阶段（API 网络）───────────────────────────────────────────┐
│ run_mmmu.run_evaluation 读 jsonl, 注入 prediction 列
│      ▼                                                            ▼
│ dataset_utils.MMMU_preproc(开放题改选择题)          eval_utils.build_judge
│      ▼                                               │ DashScope/OpenAIWrapper
│ eval_utils.eval_single_sample ◀──(model,item)任务────┘
│   └ extract_answer_from_item: can_infer 规则 → judge API → 随机兜底
│      ▼
│ 汇总 accuracy / by-split → evaluation.csv + _acc.json
└──────────────────────────────────────────────────────────────────┘
```

进阶两步（可选）：

1. 给 VideoMME 目录画同款图，标注它与 MMMU 的差异点（视频 prompt、无 `common_utils.py`、`dump_image` 换成视频抽帧路径），为 u6-l3 预热。
2. 有 GPU 与 DashScope key 时做冒烟验证：`export LMUData=/path/to/mmmu_data`，按 [evaluation/mmmu/README.md](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/README.md) 的命令先 `infer` 再 `eval`，并设 `DEBUG=true` 让判分只单线程跑前 5 条（[run_mmmu.py:L313-L315](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/evaluation/mmmu/run_mmmu.py#L313-L315)）。待本地验证。

## 6. 本讲小结

- `evaluation/` 的 5 个基准目录共享「三件套」结构：`run_*.py` 主流程、`dataset_utils.py` 数据侧、`eval_utils.py` 判分侧，`common_utils.py` 是 3/5 目录可选的第四件；`infer`/`eval` 两个子命令以 JSONL 文件解耦 GPU 推理与 API 判分。
- 数据侧三件事：按 URL+MD5 下载 TSV、用「长度 ≤64」消解图像指针并把 base64 落盘（幂等）、`MMMU_preproc` 把开放题改写成选择题以统一判分。
- 判分侧是三级瀑布：`can_infer` 规则抽取（免费确定）→ OpenAI 兼容 API judge（`temperature=0`、重试 25 次、返回哨兵串而非抛异常）→ 随机兜底（`extract_flag=False` 留审计痕迹）。
- `prepare_inputs_for_vllm` 三字段模式（prompt / multi_modal_data / mm_processor_kwargs）在五个评测脚本中逐字复制，是仓库级稳定惯用法；`image_patch_size` 从 processor 动态读取是防静默算错的关键写法。
- 读码方法论：help 文案与 docstring 会过期（`--data-dir` 实为目录、max_new_tokens 注释写 2048 实为 32768），一切以参数默认值与代码行为为准。

## 7. 下一步学习建议

- 下一讲 **u6-l2（MMMU 图文混评基准实战）** 将真正跑通本讲的三件套：下载 MMMU 验证集子集、用 2B 模型推理、解读准确率与误判样本。
- 横向对比源码：用本讲 4.4 的 diff 方法对比 `RealWorldQA/run_realworldqa.py` 与 `run_mmmu.py`，验证「读通一个目录 = 读通五个目录」。
- 若对判分细节感兴趣，可提前浏览 `evaluation/ODinW-13/eval_utils.py`，看检测类基准（bbox 而非选项字母）时三级瀑布如何被替换，为 u6-l4 做准备。
- 回顾 u3-l2 与 u2-l2，把「三字段输入」「元素级像素预算」两块地基再夯实，本讲的推理侧完全建立在它们之上。
