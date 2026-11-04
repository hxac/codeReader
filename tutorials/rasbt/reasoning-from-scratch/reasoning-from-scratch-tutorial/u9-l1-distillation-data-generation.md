# 蒸馏数据生成：调用更强的教师模型

## 1. 本讲目标

本讲是单元九（知识蒸馏，第 8 章）的第一讲。学完后你应该能够：

1. 说出一条蒸馏样本的四个字段（`problem` / `gtruth_answer` / `message_thinking` / `message_content`）各自含义，以及它们如何被下游训练代码消费。
2. 讲清 `generate_with_ollama.py`（本地教师）与 `generate_with_openrouter.py`（云端教师）两条数据生成路线的完整流程：渲染提示 → 调用教师 → 解析双流输出 → 增量落盘 → 断点续跑。
3. 理解「更换教师模型 = 更换蒸馏数据的推理风格」这一核心思想，并知道作者已发布的数据集分区（DeepSeek-R1 风格、Qwen3-235B 风格）正是这么来的。
4. 会用 `average_field_lengths_json.py` 对生成的数据做「体检」，并用 token 统计回答显存、截断与 API 成本问题。

本讲**不需要 GPU**：读样本、写 dry-run 骨架、做统计都是纯 CPU / 纯离线的活儿。真正调用教师模型属于可选步骤。

## 2. 前置知识

**知识蒸馏（knowledge distillation）。** 第 6、7 章的 RLVR 路线是：给 0.6B 学生模型一道题和一个「答对与否」的二值奖励，让它自己探索解法。蒸馏是另一条路线：直接让一个**强得多的教师模型**（如 671B 参数的 DeepSeek R1）写出完整的推理过程，再让学生模型逐 token 模仿教师的回答。RLVR「只看对错、不管过程」，蒸馏「过程与结论一起学」。两者不是对立的——实际中常常先蒸馏再强化。

**推理模型的「双流输出」。** 现代 reasoning 模型（DeepSeek R1、Qwen3 thinking 系列）的一次回答其实分两段：先在内部打草稿（thinking / reasoning 流），再写出面向用户的正式回答（content 流）。通过聊天 API 调用时，这两个流通常在响应 JSON 中分属不同字段（Ollama 是 `message.thinking`，OpenRouter 是 `message.reasoning`）。蒸馏恰恰要**两段都拿到**——thinking 流就是我们要灌给学生的「思考过程」。

**HTTP + JSON API。** 两个生成脚本都用 Python 标准库 `urllib.request` 向一个 HTTP 端点 POST 一个 JSON 载荷（payload），再把返回的 JSON 解析成字典。你只需要理解「发一个字典、收一个字典」即可，不需要其他 Web 框架知识。

**承接前讲。** 第 3 章的提示模板契约（u4-l1）：`\boxed{ANSWER}` 输出格式让答案可以被机器提取与判分。本讲的教师提示沿用同一契约，因此第 3 章的 `grade_answer` 判分管线与 `evaluate_json.py` 可以原样用来检查教师自身的准确率。第 6 章的 12000 题训练集（`math_full_minus_math500.json`，u7-l1）在本章被复用为蒸馏的题目来源——它已剔除 MATH-500，不会与评测集泄漏。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ch08/02_generate_distillation_data/generate_with_ollama.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/generate_with_ollama.py) | 本地教师路线：向本机 Ollama 服务的 `/api/chat` 端点逐题请求，生成蒸馏数据（默认模型 `qwen3:30b-thinking`） |
| [ch08/02_generate_distillation_data/generate_with_openrouter.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/generate_with_openrouter.py) | 云端教师路线：向 OpenRouter API 请求超大模型（默认 `deepseek/deepseek-r1`），支持多线程并行 |
| [ch08/02_generate_distillation_data/average_field_lengths_json.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/average_field_lengths_json.py) | 数据体检：对生成的 JSON 按字段统计 token 数的平均 / 最小 / 最大值 |
| [ch08/02_generate_distillation_data/math_train_sample.json](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/math_train_sample.json) | 5 道题的小样本输入文件，用于快速冒烟测试 |
| [ch08/02_generate_distillation_data/sample_openrouter_outputs.json](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/sample_openrouter_outputs.json) | OpenRouter（DeepSeek R1）生成的 5 条样本输出，即本讲的「实物标本」 |
| [ch08/02_generate_distillation_data/README.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/README.md) | 两个脚本的完整使用说明、成本估算与排错指南 |
| [reasoning_from_scratch/ch08.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py) | 下游消费者：`format_distilled_answer` 把两流拼成 `<think>…</think>` 训练目标（下一讲主角） |
| [reasoning_from_scratch/ch03.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py) | 提供 `eta_progress_message`、`load_tokenizer_only` 两个被复用的工具函数 |

另外目录下还有一个 `other_providers/minimax/` 子目录，是社区贡献的 MiniMax 提供商变体，本讲不展开。

## 4. 核心概念与源码讲解

### 4.1 蒸馏样本的数据契约：输入与输出 JSON

#### 4.1.1 概念说明

「数据契约」指生产者（生成脚本）与消费者（训练代码）之间约定的数据格式。本目录的契约分两端：

- **输入端**：一个 JSON 数组，每条至少含 `problem`（题面）与 `answer`（标准答案），多余字段（`level`、`type`、`unique_id`）被忽略。
- **输出端**：一个 JSON 数组，每条固定四字段：

| 字段 | 来源 | 含义 |
| --- | --- | --- |
| `problem` | 原样照抄输入 | 题面（训练时将重新渲染成学生提示） |
| `gtruth_answer` | 输入的 `answer` 改名 | 标准答案（仅用于事后核对教师准确率，**不参与**学生训练目标） |
| `message_thinking` | 教师输出 | 教师的思考流（草稿、自我怀疑、验算） |
| `message_content` | 教师输出 | 教师的正式回答，以 `\boxed{...}` 收尾 |

两个命名细节值得体会：`answer` 改名成 `gtruth_answer` 是为了避免歧义——「answer」既可以指标准答案也可以指模型答案；而 `message_thinking` / `message_content` 直接取自聊天 API 响应中 `message` 对象的两个键名，见名知义。

#### 4.1.2 核心流程

一条蒸馏样本的诞生：

```text
math_train_sample.json（输入，problem + answer）
        │
        ▼
render_prompt(problem)        ← 套上 "\boxed{ANSWER}" 输出契约的提示模板
        │
        ▼
HTTP POST → 教师（Ollama 本地 / OpenRouter 云端）
        │
        ▼
解析响应 → message_thinking（思考流）+ message_content（回答流）
        │
        ▼
{ problem, gtruth_answer, message_thinking, message_content }  ← 追加到输出数组
        │
        ▼
write_rows_json_incremental  ← 原子式整文件重写，随时可中断
```

下游消费方式（下一讲详述）：`format_distilled_answer` 把两流拼成 `f"<think>{thinking}</think>\n\n{content}"`，作为学生模型的监督目标——思考流装进 `<think>` 标签，正是 Qwen3 推理模型的输出格式。

#### 4.1.3 源码精读

输入样本长这样（共 5 条，此处引第 1 条）——[math_train_sample.json:L2-L8](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/math_train_sample.json#L2-L8)：每条含 `problem` / `level` / `type` / `answer` / `unique_id` 五个键，脚本只消费前述两个。

输出样本（OpenRouter + DeepSeek R1 生成，共 5 条）——[sample_openrouter_outputs.json:L2-L7](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/sample_openrouter_outputs.json#L2-L7)：第 1 条的 `message_thinking` 是一长段「Okay, so I need to find a value for a and b…」的草稿式自言自语，`message_content` 则是分点整洁的正式解答，末尾 `\boxed{0}`。打开文件对比同一行的两个字段，能直观感受「草稿 vs 誊写」的差异。

下游如何消费这两字段——[reasoning_from_scratch/ch08.py:L77-L83](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py#L77-L83)：`format_distilled_answer` 先校验 `message_content` 非空（空则抛 `ValueError`），再把 `message_thinking` 塞进 `<think>…</think>`、与 `message_content` 拼接。这就是四字段契约的「消费者签名」：`message_content` 必须非空是硬约束。

README 中给出的等价拼接示例——[ch08/02_generate_distillation_data/README.md:L143-L155](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/README.md#L143-L155)：`complete_answer = f"<think>{data['message_thinking']}</think>\n\n{data['message_content']}"`，并展示了拼接后的形态。

#### 4.1.4 代码实践

**实践目标**：亲手度量「思考流比正式回答长多少」，对蒸馏数据的形态建立数量直觉。

**操作步骤**（以下为示例代码，非项目原有代码，只需标准库、完全离线）：

```python
# inspect_samples.py —— 放在仓库根目录运行: python inspect_samples.py
import json

path = "ch08/02_generate_distillation_data/sample_openrouter_outputs.json"
rows = json.load(open(path, encoding="utf-8"))
print(f"共 {len(rows)} 条样本")
print(f"{'#':>2} {'think_chars':>11} {'content_chars':>13} {'ratio':>6}")
for i, r in enumerate(rows):
    th, ct = len(r["message_thinking"]), len(r["message_content"])
    print(f"{i:>2} {th:>11} {ct:>13} {th / ct:>6.2f}")
```

**需要观察的现象**：每行的 `ratio`（思考流字符数 ÷ 回答流字符数）都明显大于 1。

**预期结果**：按 README 记载的 token 统计（[README.md:L270-L279](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/README.md#L270-L279)），同一份文件中 `message_thinking` 平均 933.20 token、`message_content` 平均 196.00 token，比值约 4.8。字符比的具体数值待本地验证，但量级应与之接近。也可以把 `path` 换成 `sample_ollama_outputs.json`（本地 DeepSeek-R1 蒸馏版教师生成）对比两种教师的风格差异。

#### 4.1.5 小练习与答案

**练习 1**：为什么输出里把输入的 `answer` 改名为 `gtruth_answer`，而不是保留原名？

**答案**：因为 "answer" 是个泛称，既可指标准答案也可指模型的回答；改名后在同一行里 `gtruth_answer`（标准答案）与 `message_content`（模型回答）分工明确，避免下游脚本读错字段。README 的 Output Format 一节（[README.md:L62-L65](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/README.md#L62-L65)）明确记录了这一动机。

**练习 2**：如果某条样本的 `message_content` 是空字符串，训练管线会怎样？

**答案**：`format_distilled_answer`（ch08.py:L79-L80）会抛出 `ValueError("Missing non-empty 'message_content' field.")`；而下游 `build_examples` 用 try/except 捕获并跳过该样本（下一讲细讲）。这正是把「非空校验」放在数据契约入口处的意义。

**练习 3**：`gtruth_answer` 会被用来计算学生模型的训练损失吗？

**答案**：不会。训练损失只作用于教师写的 `message_thinking` + `message_content` 拼接目标；`gtruth_answer` 只用于事后评估教师准确率（见 4.4 节的 `evaluate_json.py` 用法）。

### 4.2 generate_with_ollama.py：本地教师生成脚本

#### 4.2.1 概念说明

Ollama 是一个在本地运行大模型的开源工具（llama.cpp 的封装），启动后在 `http://localhost:11434` 提供 HTTP 服务。它只做**推理**，不能训练——这恰好是教师的工作：我们只需要它「写答案」，不需要改它的权重。本地路线适合你机器装得下的教师（脚本默认 `qwen3:30b-thinking`，README 示例用 `deepseek-r1:8b`，约需 30 GB 内存）；装不下的（DeepSeek R1 671B、Qwen3-235B、Kimi K2.5 1T）交给下一节的 OpenRouter。

为什么坚持「教师越大越好」？因为蒸馏的上限就是教师水平：学生的训练目标完全来自教师，教师答错，学生就学错。README 因此专门给了一节「Teacher accuracy」教你怎么测量教师准确率（见 4.4.4）。

#### 4.2.2 核心流程

`main` 部分的执行骨架：

```text
解析命令行参数（模型名、题数、输出路径、--resume 等）
  ├─ 若给了 --prompt：单条问答模式，打印一个 JSON 就退出
  ├─ 确定输出文件（--out_file 或按模型名自动生成）
  ├─ 加载题目（--math_json 指定文件，否则回退 MATH-500）
  ├─ 若 --resume：读已有输出、逐行校验题面对齐，跳过已完成行
  ├─ 预热：发一句 "Reply with OK." 让 Ollama 把模型装进显存
  └─ 逐题循环：
       render_prompt → query_ollama_chat（带重试）
       → rows.append(四字段) → write_rows_json_incremental
       → 打印 ETA 进度
```

#### 4.2.3 源码精读

**提示模板与渲染。** [generate_with_ollama.py:L17-L32](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/generate_with_ollama.py#L17-L32) 定义两套模板：默认模板要求「答题并把最终结果另起一行写成 `\boxed{ANSWER}`」；`SHORTER_ANSWERS_PROMPT_TEMPLATE` 加了一句「Provide a short explanation」——教师思考动辄上千 token，简短版能压缩学生要学的目标长度。[L136-L142](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/generate_with_ollama.py#L136-L142) 的 `render_prompt` 按开关二选一并 `format` 填空。注意它与第 3 章 [ch03.py:L413-L420](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L413-L420) 的 `render_prompt` 措辞几乎相同——同一个 `\boxed` 契约，从评测沿用到了蒸馏数据生产。

**请求载荷与 think 开关。** [generate_with_ollama.py:L177-L186](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/generate_with_ollama.py#L177-L186) 构造 POST 载荷：`messages` 只有一条 user 消息；`"think": True` 是关键——告诉 Ollama 返回独立的思考流；`stream: False` 一次拿全量；`num_predict` 即生成上限（对应参数 `max_new_tokens`）。

**重试与响应解析。** [generate_with_ollama.py:L190-L238](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/generate_with_ollama.py#L190-L238) 是完整的请求-重试循环：最多 `max_retries` 次，失败间隔固定 `retry_delay` 秒；HTTP 错误、URL 错误、超时、JSON 损坏各自捕获。[L202-L208](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/generate_with_ollama.py#L202-L208) 的解析做了双格式兼容：新版 Ollama 返回 `message.content` / `message.thinking`，旧版 / generate 端点返回顶层 `response` / `thinking`；[L219-L222](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/generate_with_ollama.py#L219-L222) 统一返回 `{"message_thinking": …, "message_content": …}`——输出契约里的两个字段名就诞生在这里。

**原子式增量落盘与断点续跑。** [generate_with_ollama.py:L264-L269](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/generate_with_ollama.py#L264-L269) 的 `write_rows_json_incremental` 每题都重写整个文件，但先写 `.tmp` 再 `replace`——操作系统保证替换是原子操作，中途断电也不会留下半个 JSON。这与第 3 章评测脚本的逐行 jsonl 不同：这里选择整文件重写，是因为样本数（最多 12000）不大而代码更简单。[L272-L309](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/generate_with_ollama.py#L272-L309) 的 `load_resume_rows` + `validate_resume_rows` 实现 `--resume`：逐行核对已有输出的 `problem` 与当前数据集是否一致，对不上就报错拒绝——防止换了一份数据还接着旧文件续跑。

**预热与主循环。** [generate_with_ollama.py:L379-L389](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/generate_with_ollama.py#L379-L389) 先发一句 `"Reply with OK."`（`max_new_tokens=8`）——把模型加载时间从第一题的计时里摘出去。[L393-L432](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/generate_with_ollama.py#L393-L432) 是主循环：渲染 → 请求 → [L410-L417](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/generate_with_ollama.py#L410-L417) 组装四字段行 → 落盘 → 用第 3 章复用的 `eta_progress_message`（[ch03.py:L465-L494](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L465-L494)，按平均耗时线性外推剩余时间）打印进度。

#### 4.2.4 代码实践

**实践目标**：不依赖 Ollama、不联网，跑通「渲染教师输入」这一步，亲眼看到教师收到的完整提示。

**操作步骤**（示例代码，离线 dry-run）：

```python
# teacher_dry_run.py —— 放在仓库根目录运行: python teacher_dry_run.py
import json, sys
sys.path.insert(0, "ch08/02_generate_distillation_data")
from generate_with_ollama import render_prompt, query_ollama_chat  # 仅导入纯函数

problems = json.load(
    open("ch08/02_generate_distillation_data/math_train_sample.json", encoding="utf-8")
)[:3]

for i, row in enumerate(problems):
    prompt = render_prompt(row["problem"])          # 默认模板
    payload = {                                      # 仿照脚本 L177-L186 构造载荷
        "model": "qwen3:30b-thinking",
        "messages": [{"role": "user", "content": prompt}],
        "think": True, "stream": False,
        "options": {"num_predict": 2048, "temperature": 0.0},
    }
    print(f"--- 题 {i} (answer={row['answer']!r}) ---")
    print(prompt)
    print("payload bytes:", len(json.dumps(payload).encode("utf-8")), "\n")
```

**需要观察的现象**：打印出的提示中包含 `Answer the question and write the final result on a new line as:` 与 `\boxed{ANSWER}` 指令，题面被嵌入 `Question:` 与 `Answer:` 之间。

**预期结果**：三段完整教师提示 + 各自的载荷字节数。若本机装有 Ollama，可再执行 `python ch08/02_generate_distillation_data/generate_with_ollama.py --prompt "What is 2+2?"` 走一遍单条模式（脚本 L314-L335，打印一个 JSON 即退出）；无 Ollama 时该步待本地验证，dry-run 部分不受影响。

#### 4.2.5 小练习与答案

**练习 1**：为什么每次循环后都要重写整个输出文件，而不是最后写一次？

**答案**：一万两千题的生成耗时以小时计，中途必然中断（主动或意外）。每题落盘 + `--resume` 让任意时刻的中间结果都可用、可续跑；`.tmp` + `replace` 保证文件永远完整。README「Files are written incrementally after each sample」一节（[README.md:L62-L66](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/README.md#L62-L66)）即此设计。

**练习 2**：`--shorter_answers_prompt` 什么时候值得开？

**答案**：当教师思考过长导致学生训练目标超出 `max_len`、被大量过滤时（下一讲的 `filter_examples_by_max_len`）。它让教师写更简短的正式回答；思考流长度主要由模型自身的思考习惯决定，但整体数据会更紧凑。

**练习 3**：脚本默认 `temperature=0.0`（L80-L84），这对蒸馏数据意味着什么？

**答案**：低温让教师输出尽量确定、可复现，同一份数据集不会因随机采样而漂移；这与第 4 章学生采样实验的目的不同——这里要的是「标准教材」，不是多样性。

### 4.3 generate_with_openrouter.py：云端教师与防御性解析

#### 4.3.1 概念说明

OpenRouter 是一个聚合多家模型供应商的 API 网关：同一套 OpenAI 风格的 chat 接口，背后可以切换 DeepSeek R1（671B）、Qwen3-235B、Kimi K2.5（1T）等本地跑不动的模型。本脚本与 Ollama 版共享同一份数据契约、同一套模板与落盘/续跑逻辑，差别全在「网络对面是谁」：

- 需要 API Key（环境变量 `OPENROUTER_API_KEY`）；
- 响应格式来自**不同供应商**，字段名五花八门，需要防御性解析；
- 支持多线程并行（`--num_processes`），因为瓶颈在网络往返而非本地算力。

「换教师即换推理风格」在这一层最直观：把 `--model` 从 `deepseek/deepseek-r1` 换成别的思考模型，产出的 `message_thinking` 风格完全不同，学生学到的问题分解习惯也随之改变。作者发布的公开数据集 [rasbt/math_distill](https://huggingface.co/datasets/rasbt/math_distill)（README.md:L256-L258）与下一讲 `load_distill_data` 的四个分区（`deepseek-r1-*`、`qwen3-235b-a22b-*`）正是用不同教师跑本脚本得到的。

#### 4.3.2 核心流程

```text
main
  ├─ 校验 OPENROUTER_API_KEY（缺失即退出）
  ├─ 单条模式 / 数据集模式（同 Ollama 版）
  ├─ num_processes == 1：串行逐题（与 Ollama 版同构）
  └─ num_processes > 1：并行流水线
       ThreadPoolExecutor(max_workers=N)
       维护三个游标：next_submit（已提交）/ next_write（待按序落盘）/ futures 在途表
       循环：
         ① 把在途任务补满到 N 个
         ② wait(FIRST_COMPLETED) 收割一批完成future
         ③ 结果暂存 completed_rows[offset]
         ④ 只要 next_write 的结果已到 → 按原顺序追加 + 落盘 + 打印进度
         ⑤ 任一 future 失败 → 取消全部在途 → 整体报错退出
```

顺序之所以重要：输出文件必须与输入数据集逐行对齐，`--resume` 的校验逻辑（`validate_resume_rows`）才能继续工作。

#### 4.3.3 源码精读

**API Key 检查。** [generate_with_openrouter.py:L502-L506](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/generate_with_openrouter.py#L502-L506)：从环境变量读 `OPENROUTER_API_KEY`，缺失时 `SystemExit` 并提示——密钥永远不进命令行参数（避免进 shell 历史），只走环境变量。

**递归文本抽取器。** [generate_with_openrouter.py:L180-L247](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/generate_with_openrouter.py#L180-L247) 的 `content_to_text` 是防御性解析的地基：输入可能是字符串、字典、列表或 None（不同供应商各不相同）；字典先按优先键（`content`、`text`、`value`…L184-L199）挑文本，挑不到再递归扫非元数据键（L209-L224）；列表则逐项拼接。任何形态最终都被压成一段纯文本。

**瀑布式字段回退。** [generate_with_openrouter.py:L250-L341](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/generate_with_openrouter.py#L250-L341) 的 `parse_openrouter_response` 先校验 `choices` 非空（L251-L257），再为 `content` 走十余级回退链：`message.content` → `choice.text` → `choice.content` → `choice.output_text` → `choice.delta` → 顶层 `response` / `output_text` / `output` / `message` / `messages` / `completion`（L261-L288）；`thinking` 同样在 `message.reasoning` / `reasoning_content` / `thinking` 与 choice、顶层之间逐级回退（L290-L324）。两个兜底很妙：若 `content` 为空而 `thinking` 非空，直接令 `content = thinking`（L327-L328，有些供应商只暴露推理文本）；若全落空，抛出带 `choice_keys` / `root_keys` 的错误（L330-L336），排障时能立刻看到响应里到底有什么键。

**指数退避重试。** [generate_with_openrouter.py:L371-L407](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/generate_with_openrouter.py#L371-L407)：与 Ollama 版的固定间隔不同，这里 `backoff_delay = min(retry_delay * 2**(attempt-1), 60.0)`（L400-L402），3/6/12 秒指数增长、封顶 60 秒——对按速率限制的云端 API 更礼貌；捕获的异常种类也更多（`IncompleteRead`、`RemoteDisconnected`、`ConnectionResetError` 等，L389-L397）。

**单条样本的生成单元。** [generate_with_openrouter.py:L464-L496](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/generate_with_openrouter.py#L464-L496) 的 `generate_row` 把「渲染 → 请求 → 组装四字段行」封装成一个纯函数，串行与并行两条路径共用它——这是让并发代码保持单一职责的典型手法。

**保序并行。** [generate_with_openrouter.py:L629-L693](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/generate_with_openrouter.py#L629-L693)：线程池窗口式提交（L632-L652），`FIRST_COMPLETED` 收割（L654-L657），完成的行先暂存 `completed_rows` 字典，只有轮到 `next_write` 的行到了才写入（L670-L693）——乱序完成、按序落盘。任一 future 失败则取消其余并抛错（L695-L701），不让坏数据混进文件。README 记载实战效果（[README.md:L252](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/README.md#L252)）：12000 条 DeepSeek R1 回答，串行约 100 小时，`--num_processes 50` 压到约 2 小时。

**成本直觉。** README 的估算（[README.md:L204-L214](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/README.md#L204-L214)）：平均输入 11 token、输出 1524 token，DeepSeek R1 定价输入 \$0.70 / 百万、输出 \$2.50 / 百万，则 1000 题成本

\[ 11{,}000 \times \frac{0.70}{10^6} + 1{,}524{,}000 \times \frac{2.50}{10^6} \approx \$3.82 \]

可见成本几乎全部由输出（含思考流）决定——教师「想得越多、越贵」。

#### 4.3.4 代码实践

**实践目标**：离线验证 `parse_openrouter_response` 的防御性解析与两处兜底行为，不发出任何网络请求。

**操作步骤**（示例代码）：

```python
# test_parse_offline.py —— 放在仓库根目录运行: python test_parse_offline.py
import sys
sys.path.insert(0, "ch08/02_generate_distillation_data")
from generate_with_openrouter import parse_openrouter_response

# 用例 1：标准 OpenAI 风格（reasoning 字段承载思考流）
r1 = parse_openrouter_response({
    "choices": [{"message": {
        "content": "The answer is 1. \\boxed{1}",
        "reasoning": "Let me think... 0.5+0.5=1",
    }}]
})
print("用例1:", {k: v[:30] for k, v in r1.items()})

# 用例 2：只有推理、没有 content —— 应触发 content = thinking 兜底
r2 = parse_openrouter_response({
    "choices": [{"message": {"reasoning": "step by step..."}}]
})
print("用例2:", r2)

# 用例 3：什么文本都没有 —— 应抛 RuntimeError 并列出响应的键
try:
    parse_openrouter_response({"choices": [{"finish_reason": "length"}]})
except RuntimeError as e:
    print("用例3 抛错:", e)
```

**需要观察的现象**：用例 1 正常分离出两个流；用例 2 中 `message_content` 与 `message_thinking` 相同；用例 3 的报错信息里出现 `choice_keys` / `root_keys`。

**预期结果**：如上三条。本实践纯函数、无网络，结果可直接本地复现；若与预期不符，回读 L327-L336 定位兜底顺序。

#### 4.3.5 小练习与答案

**练习 1**：并行模式下为什么不能「谁先完成谁先写进文件」？

**答案**：输出文件必须与输入数据集第 i 行对齐第 i 条输出，`--resume` 的 `validate_resume_rows`（L441-L461）按行号核对 `problem`，乱序会破坏对齐、续跑时误判。所以用 `completed_rows` 暂存 + `next_write` 游标按序写（L670-L693）。

**练习 2**：`parse_openrouter_response` 最后为何要把 `choice_keys` / `root_keys` 放进异常信息？

**答案**：OpenRouter 背后供应商众多、响应形态不一。解析失败时，调用者最想知道的是「这次响应里到底有哪些键」，从而判断该在回退链的哪一级加支持；不带键名的 "did not contain parseable content" 几乎无法排障（见 L330-L336）。

**练习 3**：与 Ollama 版相比，OpenRouter 版的重试为什么改用指数退避？

**答案**：本地 Ollama 的失败多是「服务没起 / 模型没下」这类确定性问题，固定间隔重试足矣；云端 API 的失败常见于速率限制与服务端瞬时过载，指数退避（3s→6s→12s，封顶 60s）给对端恢复时间，避免雪上加霜（L400-L402）。

### 4.4 average_field_lengths_json.py：蒸馏数据体检

#### 4.4.1 概念说明

拿到蒸馏数据后、送进训练前，要先回答三个问题：**显存够不够**（样本 token 长度决定批处理开销与过滤比例）、**会不会截断**（`max_new_tokens` 设多少才不腰斩教师回答）、**成本有多高**（输出 token 数直接换算成 API 账单）。本脚本用学生的 reasoning 分词器对每个字段统计 token 数的平均 / 最小 / 最大值，一次回答三个问题。

#### 4.4.2 核心流程

```text
读取 JSON（--json_path）
  ├─ 先按整体 JSON 数组解析；失败则回退按 JSONL 逐行解析
  └─ 若是 {"records": [...]} 包装的字典，取其 records
对每条记录的每个字段：
  value_to_text（非字符串值序列化成 JSON 文本）
  → tokenizer.encode(text) 数 token
  → 累计 total / count / min / max
按字段名排序，打印对齐的统计表
```

#### 4.4.3 源码精读

**双格式输入兼容。** [average_field_lengths_json.py:L24-L56](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/average_field_lengths_json.py#L24-L56) 的 `load_records`：先整体 `json.load`；抛 `JSONDecodeError` 就回退逐行解析（JSONL，L32-L45）；解析出字典时兼容 `{"records": [...]}` 包装或单对象。生成脚本写的是 JSON 数组，但历史文件或第三方数据可能是别的形态——入口做宽容处理，统计核心保持简单。

**值转文本。** [average_field_lengths_json.py:L59-L64](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/average_field_lengths_json.py#L59-L64)：字符串原样、None 记空串、其他类型（数字、嵌套结构）序列化成 JSON 文本再数——保证任意字段的记录都不至于崩。

**统计核心。** [average_field_lengths_json.py:L67-L103](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/average_field_lengths_json.py#L67-L103)：三层 `defaultdict` / 字典分别累计总和、条数与极值；注意它是**按记录里实际出现的字段**动态统计（L79 遍历 `record.items()`），不写死字段名——因此输入是原始 MATH 数据（`problem`/`answer`/…）还是蒸馏输出（四字段）都能出表。

**分词器选择与一个隐藏开销。** [average_field_lengths_json.py:L109](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/average_field_lengths_json.py#L109) 用 `load_tokenizer_only(which_model="reasoning")`（[ch03.py:L93-L118](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L93-L118)，首次运行会下载约 10 MiB 的 `tokenizer-reasoning.json`）。注意该分词器开启了聊天模板：`encode` 会先经 `_wrap_chat` 包上 `<|im_start|>user\n…<|im_end|>\n<|im_start|>assistant\n`（[qwen3.py:L397-L398 与 L411-L419](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L411-L419)），这部分固定开销约 8 个 token。README 记录的统计表（[README.md:L270-L279](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/README.md#L270-L279)）中 `gtruth_answer` 最短 9 token（内容只是 "0"、"98" 这类单值）正与此吻合——每个字段的绝对值都含约 8 token 的模板底数，做精确预算时应予扣除；用于字段间相对比较则无妨。

**表格输出。** [average_field_lengths_json.py:L112-L141](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/average_field_lengths_json.py#L112-L141)：手工按各列最长内容计算宽度、打印对齐表格——不引第三方表格库，保持脚本零额外依赖。

**附带一提：教师准确率。** README「Teacher accuracy」一节（[README.md:L282-L295](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/README.md#L282-L295)）用第 3 章的 `evaluate_json.py` 离线重判输出文件（`--gtruth_answer gtruth_answer --generated_text message_content`），样例结果 5/5 全对——蒸馏前先量一量教师，这是「学生上限」的直接估计。

#### 4.4.4 代码实践

**实践目标**：对样例数据跑一次体检，并解读每个数字。

**操作步骤**：

```bash
cd ch08/02_generate_distillation_data
python average_field_lengths_json.py --json_path sample_openrouter_outputs.json
```

**需要观察的现象**：首次运行先下载 `tokenizer-reasoning.json`（约 10 MiB，进度条），随后打印 `Records: 5` 与五行统计表。

**预期结果**：与 README 记载一致（待本地验证）：

```text
Field             AvgTokens  MinTokens  MaxToken  Count
gtruth_answer          9.40          9        10      5
message_content      196.00        166       259      5
message_thinking     933.20        449      1676      5
problem               77.80         30       121      5
```

解读要点：① `message_thinking` 平均约为 `message_content` 的 4.8 倍——思考流才是训练目标的体积大头；② `message_thinking` 最大 1676 token，说明下一讲的 `max_len` 过滤阈值若设在 1024 会裁掉长样本；③ `gtruth_answer` 的 9~10 token 里约 8 个是聊天模板固定开销（见 4.4.3）。把 `--json_path` 换成 `sample_ollama_outputs.json` 再跑一次，即可对比两位教师的风格体积差。

#### 4.4.5 小练习与答案

**练习 1**：为什么用 reasoning 分词器而不是 base 分词器来统计？

**答案**：统计的最终目的是为学生（即将被蒸馏成 reasoning 模型的 Qwen3 0.6B）做训练预算；reasoning 分词器才是学生实际使用的分词器（且训练时提示确实会套聊天模板）。用同一把尺子量，数字才能直接指导 `max_len`、批大小等训练参数。

**练习 2**：若输入文件是 JSONL（每行一个 JSON 对象），脚本如何处理？

**答案**：`load_records` 先整体 `json.load` 失败后回退逐行解析（L30-L45），空行跳过、坏行报出具体行号；因此两种格式都能统计。

**练习 3**：从统计表看，若下一讲把过滤阈值 `max_len` 设为 1024，最可能损失什么？

**答案**：主要损失**长思考样本**——`message_thinking` 最大已达 1676 token，加上 `message_content` 与题面后总长更容易超限；被裁掉的恰是教师「想得最久」的难题样本，可能造成难度分布偏移。

## 5. 综合实践

把本讲三个模块串成一个离线小任务（全程无需 Ollama、无需 API Key）：

**任务**：为「自选教师」制作一份 dry-run 数据生产计划。

1. **量样本**：运行 4.1.4 的 `inspect_samples.py`，对 `sample_openrouter_outputs.json` 与 `sample_ollama_outputs.json` 各输出一张「思考/回答字符比例」表，写 2~3 句对比两种教师（DeepSeek R1 云端 vs 本地蒸馏版）的风格差异。
2. **做骨架**：扩展 4.2.4 的 `teacher_dry_run.py`，读取 `math_train_sample.json` 全部 5 题，为每题打印：渲染后的教师提示、四字段输出行的「预览版」（`gtruth_answer` 用真值，`message_thinking` / `message_content` 填占位字符串），并用 4.4.3 的知识估计每题输出行的 token 量级。把这个骨架存成一个脚本，将来装好 Ollama 后只需把「打印载荷」换成 `query_ollama_chat(...)` 即可真跑。
3. **验契约**：在你的骨架里加一个断言函数：模拟 `format_distilled_answer`（ch08.py:L77-L83）的校验逻辑，对每条预览行检查 `message_content` 非空、`problem` 与输入对齐——提前暴露契约违规。
4. **可选真跑**（需本地 Ollama + 已下载模型）：`python generate_with_ollama.py --math_json math_train_sample.json --dataset_size 5 --model deepseek-r1:8b --out_file my_first_distill.json`，然后 `python average_field_lengths_json.py --json_path my_first_distill.json` 与 4.4.4 的表对照。无 Ollama 环境时此步待本地验证。

**验收标准**：两张比例表 + 一个可复用的 dry-run 脚本 + 一段「若把教师换成 X，我预计数据会……」的推断（不少于 3 句）。

## 6. 本讲小结

- 蒸馏数据契约是四字段行：`problem`（题面）、`gtruth_answer`（标准答案，仅供评估）、`message_thinking`（教师思考流）、`message_content`（教师正式回答）；两流由下游 `format_distilled_answer` 拼成 `<think>…</think>` 训练目标。
- 教师提示沿用第 3 章的 `\boxed{ANSWER}` 契约，使 `grade_answer` / `evaluate_json.py` 判分管线可原样复用来测量教师准确率；教师即学生上限，先量教师再蒸馏。
- `generate_with_ollama.py` 走本地路线：`think: True` 拿独立思考流、固定间隔重试、`.tmp`+`replace` 原子落盘、`--resume` 逐行校验续跑、预热请求摘出模型加载时间。
- `generate_with_openrouter.py` 走云端路线：`content_to_text` + `parse_openrouter_response` 的瀑布式回退消化各供应商的响应差异，指数退避对付速率限制，`ThreadPoolExecutor` + `next_write` 游标实现「乱序完成、按序落盘」的并行。
- `average_field_lengths_json.py` 用学生的 reasoning 分词器对字段做 token 体检，回答显存、截断与成本三问；注意每个字段的绝对值含约 8 token 的聊天模板固定开销。
- 更换教师即更换推理风格：`--model` 一换，思考流的解题习惯随之改变——作者公开的 `rasbt/math_distill` 数据集的多个分区正来源于此。

## 7. 下一步学习建议

下一讲（u9-l2《蒸馏数据管线：从 JSON 到训练样本》）将进入 `reasoning_from_scratch/ch08.py`：`load_distill_data` 如何按分区下载本讲脚本产出的数据、`format_distilled_answer` 如何包装 `<think>` 标签、`build_examples` 如何拼接 prompt 与 answer 并记录 `prompt_len`，以及长度统计与过滤。建议先读 [reasoning_from_scratch/ch08.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch08.py) 的开头到 `build_examples` 一段，并回顾 u2-l6（分词器与聊天模板）——四字段数据如何变成 token 序列，全靠那把尺子。
