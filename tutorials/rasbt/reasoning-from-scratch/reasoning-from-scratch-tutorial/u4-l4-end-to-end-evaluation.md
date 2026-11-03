# 端到端 MATH-500 评测流程

## 1. 本讲目标

前四讲（u4-l1 到 u4-l3）我们分别造好了评测流水线的全部零件：数据与提示模板（`load_math500_test`、`render_prompt`）、模型加载（`load_model_and_tokenizer`）、答案提取（`extract_final_candidate`）与 SymPy 判分（`grade_answer`）。本讲把这些零件**装配成一台完整的机器**——在 MATH-500 的 500 道题上自动跑完「渲染→生成→提取→判分→落盘→汇报」全流程。

学完本讲，你应该能够：

1. 描述评测主循环中**生成、提取、判分、记录**四步在代码里的确切位置与顺序。
2. 读懂并复用 `eta_progress_message` 的进度与 ETA（预计剩余时间）输出，理解 `\r` 原地刷新与 `ljust` 补空格两个终端技巧。
3. 打开一份评测产出的 jsonl 文件，说出六个字段各自的含义，并用它手算准确率。
4. 对比单条流式评测脚本 `evaluate_math500.py` 与批处理版 `evaluate_math500_batched.py` 的适用场景，理解批处理何时更快、何时反而更慢。
5. 用 `evaluate_json.py` 对已有日志**离线重判**——不重新生成就能重算准确率。

## 2. 前置知识

本讲默认你已读过 u4-l1～u4-l3。这里补充三个新概念：

- **jsonl（JSON Lines）**：一种文本格式，文件里每一行是一个独立、完整的 JSON 对象。与「把 500 条记录塞进一个大 JSON 数组」相比，jsonl 可以逐行追加、逐行解析——评测跑到第 300 题时，前 299 行已经是合法数据。大模型评测、日志系统普遍采用它。
- **ETA（Estimated Time of Arrival）**：预计剩余时间。最简单的估法是线性外推：用已完成的题目数除已流逝的时间得到平均单题耗时，再乘上剩余题数。它假设「未来的题和已做的题一样快」，对 MATH-500 这种题长不一的数据只是近似。
- **批处理推理（batched inference）**：把多条 prompt 左侧补齐（left-padding）到同一长度后拼成一个二维张量，一次前向同时为所有序列各生成一个 token。GPU 擅长这种大规模并行，吞吐可以成倍提升；但 CPU 上一个大 batch 的前向计算量是逐条叠加的，反而可能更慢——本讲会用仓库 README 里的真实测量数据说明这一点。

另外回顾一个 u2-l6 的结论：Qwen3Tokenizer 的 `pad_token_id` 恒为 `<|endoftext|>` 的 id，`eos_token_id` 则按 base / reasoning 权重自动区分——本讲的批处理脚本正是用这两个属性做补齐和截断的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `reasoning_from_scratch/ch03.py` | 包内库代码。本讲涉及 `generate_text_stream_concat`（生成适配器）、`mini_eval_demo`（单题演示）、`eta_progress_message`（进度消息）、`evaluate_math500_stream`（评测主循环） |
| `ch03/02_math500-verifier-scripts/evaluate_math500.py` | 命令行脚本：串行流式评测（教学基准形态） |
| `ch03/02_math500-verifier-scripts/evaluate_math500_batched.py` | 命令行脚本：批量生成版评测（吞吐优先） |
| `ch03/02_math500-verifier-scripts/evaluate_json.py` | 命令行脚本：对已保存的 JSON/JSONL 记录离线重算准确率 |
| `ch03/02_math500-verifier-scripts/README.md` | 三个脚本的用法说明与各设备实测耗时/准确率表 |
| `tests/test_ch03.py` | `test_evaluate_math500_stream` 用假生成器验证评测循环的正确性 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**① 单题演示与生成适配器**、**② 进度消息**、**③ 评测主循环**、**④ 三个独立脚本**（批处理与离线重判）。

### 4.1 从一道题到五百题：mini_eval_demo 与 generate_text_stream_concat

#### 4.1.1 概念说明

`mini_eval_demo` 是作者在 notebook 里用的「一题版评测」：拿一道 `1/2 + 1/6` 的算术题，把前三讲的四个零件串成一条链，打印每一步的中间结果。它的价值不在评测（只有一道题），而在于**把抽象的四步链路具象化成 20 行可读代码**——读懂了它，`evaluate_math500_stream` 只是把同样的四步套进 for 循环。

链条中间还有一个不起眼但关键的适配器 `generate_text_stream_concat`：第 2 章（u3-l2）的生成函数族吃的是 token id 张量、吐的是 token 流（generator），而评测链路希望「文本进、文本出」。这个函数负责包装：encode → 逐 token 消费流 → 把收集到的 id 拼回文本。

#### 4.1.2 核心流程

```text
generate_text_stream_concat(model, tokenizer, prompt, device, max_new_tokens):
    input_ids = tokenizer.encode(prompt) 升维成 (1, T) 张量
    generated_ids = []
    for token in generate_text_basic_stream_cache(  # 第 2 章的流式+KV缓存生成器
            model, input_ids, max_new_tokens, eos_token_id=tokenizer.eos_token_id):
        generated_ids.append(token.squeeze(0).item())
        verbose 时逐 token decode 打印（打字机效果）
    return tokenizer.decode(generated_ids)   # 只含生成部分，不含 prompt

mini_eval_demo 四步链路:
    1. render_prompt(problem)        → 提示文本
    2. generate_text_stream_concat   → 模型生成文本
    3. extract_final_candidate(text) → 候选答案
    4. grade_answer(候选, 标准答案)  → True/False
```

注意第 2 步把 `tokenizer.eos_token_id` 传给了生成器，因此模型发出结束符时生成提前停止（复习 u3-l1：`max_new_tokens` 由「必须」变为「最多」）。

#### 4.1.3 源码精读

生成适配器——把第 2 章的 token 流包装成「文本进、文本出」，并透传 `verbose` 实现打字机效果：

[reasoning_from_scratch/ch03.py:121-145](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L121-L145)

单题演示——四步链路各占一行，编号注释与本章各讲一一对应，最后打印预测、标准答案与判分结果：

[reasoning_from_scratch/ch03.py:445-462](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L445-L462)

单元测试对适配器的验证手法值得学习：测试用一个假流（依次产出 id 1、2、1 的三个张量）替换掉真实的 `generate_text_basic_stream_cache`，从而在**完全不做模型计算**的情况下验证「收集 id → 拼回文本」的逻辑：

[tests/test_ch03.py:118-136](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch03.py#L118-L136)

#### 4.1.4 代码实践

1. **实践目标**：跑通最小的一题评测，亲眼看到四步链路的中间产物。
2. **操作步骤**（示例代码）：

   ```python
   # eval_one.py（示例代码）
   import torch
   from reasoning_from_scratch.ch02 import get_device
   from reasoning_from_scratch.ch03 import (
       load_model_and_tokenizer, mini_eval_demo,
   )

   device = get_device()
   model, tokenizer = load_model_and_tokenizer(
       which_model="base", device=device, use_compile=False,
   )
   mini_eval_demo(model, tokenizer, device)
   ```

3. **需要观察的现象**：终端先打印设备名，然后依次输出 Prediction（第 3 步提取结果）、Ground truth（`2/3`）、Correct（True/False）。
4. **预期结果**：base 0.6B 模型在一道简单分数加法题上的表现取决于权重与采样设置，本讲不预设结论，**待本地验证**。若 Prediction 为空串，多半是生成里既无 `\boxed{}` 也无数字——回看 u4-l2 的三级回退。
5. 无 GPU / 未下载权重的替代方案：直接阅读上面的单元测试，理解「假流 → ABA」这条断言链即可，不需要运行任何模型。

#### 4.1.5 小练习与答案

**练习 1**：`generate_text_stream_concat` 里为什么要 `token.squeeze(0)` 再 `.item()`？
**答案**：生成器每次 yield 的是一个形状约 `(1, 1)` 的张量（batch 维 × 单 token）。`squeeze(0)` 去掉 batch 维得到单元素张量，`.item()` 取出 Python 标量，才能用 `generated_ids` 列表收集、最后一次性 decode。

**练习 2**：`mini_eval_demo` 的四步分别来自前三讲的哪个函数？
**答案**：第 1 步 `render_prompt`（u4-l1，提示模板）；第 2 步 `generate_text_stream_concat`（内部调用 u3-l2 的 `generate_text_basic_stream_cache`）；第 3 步 `extract_final_candidate`（u4-l2，boxed 优先 + 数字回退）；第 4 步 `grade_answer`（u4-l3，SymPy 等价判定）。

**练习 3**：如果想在评测时看到模型逐 token 打字的效果，该传哪个参数？
**答案**：把 `verbose=True` 一路传给 `generate_text_stream_concat`（该参数在 `evaluate_math500_stream` 中同名透传）。

### 4.2 eta_progress_message：进度与预计剩余时间

#### 4.2.1 概念说明

在 H100 上评完 500 题 base 模型要 13 分钟，reasoning 模型要 3 小时（见 4.4 节 README 实测表）——没有进度反馈的长任务会让人以为程序卡死了。`eta_progress_message` 用约 35 行纯 Python 解决两件事：**显示进度**（`processed/total`）与**线性外推 ETA**。它不依赖 torch、不依赖模型，是一个可以单独复用的工具函数。

#### 4.2.2 核心流程

设已完成 \( n \) 题、总题数 \( N \)、已流逝 \( E \) 秒，则平均单题耗时与 ETA 为：

\[ \bar{t} = \frac{E}{n}, \qquad \mathrm{ETA} = \bar{t} \times (N - n) = \frac{E \, (N-n)}{n} \]

```text
eta_progress_message(processed, total, start_time, show_eta, label):
    若 show_eta=False 或 processed <= 0:
        只返回 "label: processed/total"（补齐到固定宽度）
    elapsed = time.time() - start_time
    remaining = max(total - processed, 0)
    eta_seconds = elapsed / processed * remaining   # 线性外推
    用两次 divmod 把秒拆成 时/分/秒，按量级选 "XXh XXm XXs" / "XXm XXs" / "XXs"
    返回 "label: processed/total | ETA: ..."（同样补齐）
```

两个工程细节：

- **`ljust(pad_width)` 防残留**：调用方用 `print(msg, end="\r")` 原地覆盖上一条消息。若新消息比旧的短，终端会留下旧消息的尾巴。函数以「最长可能形态」`label: total/total | ETA: 00h 00m 00s` 的长度为基准右补空格，保证覆盖干净。
- **边界防御**：`processed <= 0`（避免除零）与 `elapsed <= 0`（计时精度内）都退化为纯进度显示；`remaining` 用 `max(..., 0)` 钳住非负。

#### 4.2.3 源码精读

完整实现——注意 `pad_width` 的计算方式与两级防御分支：

[reasoning_from_scratch/ch03.py:465-500](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L465-L500)

#### 4.2.4 代码实践

1. **实践目标**：不加载任何模型，亲手做一个假进度条，观察 ETA 收敛与原地刷新效果。
2. **操作步骤**（示例代码）：

   ```python
   # fake_progress.py（示例代码）
   import time
   from reasoning_from_scratch.ch03 import eta_progress_message

   total, start = 10, time.time()
   for i in range(1, total + 1):
       time.sleep(0.5)  # 假装每题耗时 0.5 秒
       msg = eta_progress_message(
           processed=i, total=total, start_time=start,
           show_eta=True, label="MATH-500",
       )
       print(msg, end="\r", flush=True)
   print()
   ```

3. **需要观察的现象**：同一行不断被覆盖刷新；第 1 题后 ETA 偏大或抖动，随着题目增多逐渐稳定在剩余题数 × 0.5 秒附近。
4. **预期结果**：结束时终端停留在 `MATH-500: 10/10 | ETA: 00s`（宽度与过程消息一致，无残留字符）。
5. 想看「尾巴残留」的反例：把 `ljust` 那两行临时注释掉再跑一次（自己复制一份函数做实验，勿改源码），观察覆盖后行尾是否留下旧字符——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 ETA 用平均耗时外推，而不是用最近一题的耗时？
**答案**：单题耗时方差很大（题目长短、生成是否提前停止都影响），最近一题噪声大；平均耗时是移动范围更稳的估计。它仍假设未来题目与已做题目同分布，对 MATH-500 这类难度不均的数据只是近似。

**练习 2**：`pad_width` 为什么取 `label: total/total | ETA: 00h 00m 00s` 这个「最终形态」的长度？
**答案**：这是所有可能输出里最长的（processed ≤ total，ETA 固定 8 字符），以它为宽度补空格能保证任何时刻的输出都等宽，`\r` 覆盖时不残留上一条更长消息的尾部。

**练习 3**：`evaluate_math500_batched.py` 里调用它时 `show_eta` 的默认值与脚本传值有何不同？
**答案**：函数签名默认 `show_eta=False`（[evaluate_math500_batched.py:42](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/02_math500-verifier-scripts/evaluate_math500_batched.py#L42)），而命令行脚本在主入口显式传 `show_eta=True`（[evaluate_math500_batched.py:256](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/02_math500-verifier-scripts/evaluate_math500_batched.py#L256)）；流式版 `evaluate_math500_stream` 则硬编码 `show_eta=True`。

### 4.3 evaluate_math500_stream：评测主循环与 jsonl 落盘

#### 4.3.1 概念说明

`evaluate_math500_stream` 是本讲的主角：把 `mini_eval_demo` 的四步装进 `for` 循环，再加上三件工程化的事——**逐题记录**（写 jsonl）、**实时进度**（4.2 节的消息）、**汇总报告**（准确率、总耗时、平均响应长度）。它是「库函数」形态：输入模型、分词器、数据列表，输出三元组 `(num_correct, num_examples, acc)` 并顺带落盘一份可复查的日志。

落盘格式选 jsonl 的动机：每题一行、随循环推进逐行写入，评测中断时已完成部分仍是合法数据；后续既可以用 `evaluate_json.py` 重判，也可以用 pandas 逐行流式分析。

#### 4.3.2 核心流程

```text
evaluate_math500_stream(model, tokenizer, device, math_data, out_path=None,
                         max_new_tokens=512, verbose=False):
    out_path 缺省时按设备命名 math500-{dev}.jsonl（':' → '-'，兼容 Windows）
    打开 out_path（"w" 模式）
    for i, row in enumerate(math_data, start=1):
        ① prompt    = render_prompt(row["problem"])          # 渲染
        ② gen_text  = generate_text_stream_concat(...)        # 生成
           total_len += len(tokenizer.encode(gen_text))       # 累计 token 数
        ③ extracted = extract_final_candidate(gen_text)       # 提取
        ④ correct   = grade_answer(extracted, row["answer"])  # 判分
           num_correct += int(correct)
        ⑤ 写一行 jsonl：{index, problem, gtruth_answer,
                          generated_text, extracted, correct}
        ⑥ 打印进度消息（end="\r"）
    汇总打印：准确率 / 总时间(分) / 平均响应长度(token) / 日志路径
    return num_correct, num_examples, acc
```

每行记录的六个字段：

| 字段 | 含义 | 来源 |
| --- | --- | --- |
| `index` | 题号，**从 1 开始** | `enumerate(..., start=1)` |
| `problem` | 原始题目 | 数据集 |
| `gtruth_answer` | 标准答案 | 数据集 |
| `generated_text` | 模型完整生成文本 | 步骤② |
| `extracted` | 提取出的候选答案 | 步骤③ |
| `correct` | 判分布尔值 | 步骤④ |

#### 4.3.3 源码精读

评测主循环完整实现——①～④ 与 `mini_eval_demo` 逐行同构，⑤⑥ 是新增的记录与进度：

[reasoning_from_scratch/ch03.py:503-574](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L503-L574)

其中三处细节值得单独看：

- 默认输出路径按设备名生成，并把 `:` 替换成 `-`（`cuda:0` → `cuda-0`），因为 Windows 文件名不允许冒号：
  [reasoning_from_scratch/ch03.py:513-515](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L513-L515)
- 六字段记录逐行写入，`ensure_ascii=False` 保留 Unicode 原文（数学符号、非 ASCII 字符不转义成 `\uXXXX`，日志可读）：
  [reasoning_from_scratch/ch03.py:540-548](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L540-L548)
- 单元测试 `test_evaluate_math500_stream` 的手法：用 `monkeypatch` 把生成函数替换成按序吐出两段假文本的哑元（一条含 `\boxed{A}` 判对、一条 `\boxed{X}` 判错），再断言返回值与 jsonl 六字段——完全不跑模型就锁死了评测循环的行为：
  [tests/test_ch03.py:366-418](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch03.py#L366-L418)

另一个值得琢磨的小设计：平均响应长度不是在生成时数 token，而是事后 `tokenizer.encode(gen_text)` 重新编码统计（[ch03.py:530](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L530)，注释标注对应书中习题 3.2）。原因是 token id 列表被封装在 `generate_text_stream_concat` 内部没有暴露；「decode 后再 encode」理论上可能与原始序列不完全一致，但对「平均长度」这个统计用途足够精确。

#### 4.3.4 代码实践（本讲核心实践·上半）

1. **实践目标**：在 5 题子集上真实跑一遍评测主循环，检查 jsonl 六字段并手算准确率。
2. **操作步骤**（示例代码）：

   ```python
   # eval_subset.py（示例代码）
   import torch
   from reasoning_from_scratch.ch02 import get_device
   from reasoning_from_scratch.ch03 import (
       load_model_and_tokenizer, load_math500_test, evaluate_math500_stream,
   )

   device = get_device()
   model, tokenizer = load_model_and_tokenizer(
       which_model="base", device=device, use_compile=False,
   )
   math_data = load_math500_test()[:5]          # 前 5 题
   evaluate_math500_stream(
       model=model, tokenizer=tokenizer, device=device,
       math_data=math_data,
       out_path="math500-first5.jsonl",
       max_new_tokens=128,                       # 控制时长
   )
   ```

   然后检查日志（示例代码）：

   ```python
   # inspect_log.py（示例代码）
   import json
   n, c = 0, 0
   with open("math500-first5.jsonl", encoding="utf-8") as f:
       for line in f:
           rec = json.loads(line)
           n += 1
           c += rec["correct"]
           print(rec["index"], rec["extracted"], "| GT:", rec["gtruth_answer"],
                 "| correct:", rec["correct"])
   print("手算准确率:", c, "/", n, "=", c / n)
   ```

3. **需要观察的现象**：终端一行进度反复刷新；结束后打印 Accuracy、Total time、Average response length、Logs written to；`inspect_log.py` 输出每题的提取结果与判分。
4. **预期结果**：jsonl 恰好 5 行，`index` 为 1～5；`evaluate_math500_stream` 打印的 Accuracy 与手算的 `c/n` 一致（两者本就同一份 `num_correct`/`num_examples`）。5 题 × 128 token 在 CPU 上通常数分钟内完成；具体准确率取决于模型与硬件，**待本地验证**。
5. 若暂时没有权重：仿照 `test_evaluate_math500_stream`，用 `monkeypatch`（或直接给 `ch03.generate_text_stream_concat` 赋一个假函数）替换生成步骤，即可在零计算下验证循环、jsonl 与准确率统计——这也是本讲综合实践提供的 B 方案。

#### 4.3.5 小练习与答案

**练习 1**：`out_path=None` 时文件名如何决定？为什么要把 `:` 换成 `-`？
**答案**：文件名为 `math500-{设备名}.jsonl`，设备名来自 `str(device)`（如 `cuda:0`、`mps`）。Windows 文件名不允许冒号，替换成 `-` 保证跨平台可写。

**练习 2**：为什么每题写一行 jsonl，而不是跑完 500 题后一次性 `json.dump` 一个大数组？
**答案**：逐行写入使已完成的部分立即可用（中断/崩溃后仍是合法 jsonl），也便于边跑边用别的进程读取分析；一个大数组要么全有要么全无，长任务风险高。

**练习 3**：`record["correct"]` 存的是 `bool(is_correct)` 而不是 `int(is_correct)`，两者在 jsonl 里有何区别？
**答案**：bool 序列化为 `true/false`，int 序列化为 `1/0`；语义上更直白，且与函数内部累计用的 `num_correct += int(is_correct)` 分工清晰（存储看语义，计数用整数）。

### 4.4 三个独立脚本：命令行入口、批处理与离线重判

#### 4.4.1 概念说明

`reasoning_from_scratch/ch03.py` 提供「库」，`ch03/02_math500-verifier-scripts/` 下的三个脚本是「应用」——它们 import 包里的函数，避免代码重复（该目录 README 明说了这一点）。三者分工：

- **`evaluate_math500.py`**：命令行版串行流式评测，就是 4.3 节主循环的一个 argparse 壳。
- **`evaluate_math500_batched.py`**：把「生成」这一步换成批量推理，其余链路（提取、判分、记录）与流式版完全一致，追求吞吐。
- **`evaluate_json.py`**：完全不生成，只对已有日志重跑提取 + 判分，重算准确率。

为什么「批处理」值得单独做？整条链路里**生成是最贵的环节**（占 99% 以上时间），提取与判分是毫秒级的纯 CPU 工作。所以加速的杠杆只在这里；也正因如此，`evaluate_json.py` 的「离线重判」才有价值——将来升级了答案解析器，不必花几小时重新生成，重判旧日志几秒钟出结果。

#### 4.4.2 核心流程

`evaluate_math500.py` 主流程：

```text
parse_args（--device auto / --which_model base|reasoning|instruct /
           --dataset_size 默认10 / --max_new_tokens 默认2048 /
           --compile / --checkpoint_path / --verbose）
device = get_device()（auto 时）
math_data = load_math500_test()
instruct → 内部按 reasoning 权重加载，但 tokenizer.add_thinking = False
checkpoint_path 有值 → 从第 6 章的 RL checkpoint 加载权重（评测训练成果）
model.eval() + set_float32_matmul_precision("high")
evaluate_math500_stream(..., out_path=f"math500_{模型}-{设备}-evaluate-script.jsonl")
```

`evaluate_math500_batched.py` 的批量循环：

```text
for start in range(0, N, batch_size):
    batch = 数据切片
    prompts = [render_prompt(row.problem) for row in batch]
    tokenized = [encode(p) for p in prompts]
    left_padded = 每条左侧补 pad_id 到该批最长长度   # 右端对齐
    gen = generate_text_basic_batched_cache[_stop](input_ids, ...)  # (B, T_new)
    for 每行 b:
        在第一个 eos_id 处截断该行 token 序列
        gen_text = decode(截断后序列)
        extract_final_candidate → grade_answer → 写一行 jsonl（字段与流式版一致）
```

left-padding 的直觉：解码器从序列**右端**继续生成，把 pad 全放在左侧，使一批序列的「生长点」对齐在同一列，批内每行按各自 pad 数量偏移位置编码与注意力掩码。截断逻辑则保证批生成的 EOS 之后内容（可能是对同批其他仍在生成序列的陪跑输出）不会进入 `generated_text`，与流式版的语义对齐。

#### 4.4.3 源码精读

命令行参数定义——注意默认值：`dataset_size=10`（不是 500）、`max_new_tokens=2048`：

[evaluate_math500.py:21-64](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/02_math500-verifier-scripts/evaluate_math500.py#L21-L64)

主入口两处关键分支——`--checkpoint_path` 复用第 6 章 RL 训练成果做评测；`instruct` 变体加载 reasoning 权重但关闭 thinking（对应 u2-l6 的模板开关）：

[evaluate_math500.py:86-112](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/02_math500-verifier-scripts/evaluate_math500.py#L86-L112)

批处理版**换掉了整个模型实现**——注意 import 来自 `qwen3_batched` 而非 `qwen3`（前者支持 batch 维，是单元十 u10-l2 的主题）：

[evaluate_math500_batched.py:15-30](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/02_math500-verifier-scripts/evaluate_math500_batched.py#L15-L30)

批内编码与左侧补齐——pad 到本批最长，右端对齐：

[evaluate_math500_batched.py:64-73](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/02_math500-verifier-scripts/evaluate_math500_batched.py#L64-L73)

每行在第一个 EOS 处截断后再 decode，随后提取、判分、写记录——注释明说变量命名「与 evaluate_math500_stream 保持一致」，六字段完全同构：

[evaluate_math500_batched.py:91-111](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/02_math500-verifier-scripts/evaluate_math500_batched.py#L91-L111)

高效模式的开关——默认用 `generate_text_basic_batched_cache_stop`（某行发出停止符后该行停算，配合 `shrink_kv_cache_inplace` 收缩缓存），`--disable_efficient_mode` 退回更简单但更慢的实现：

[evaluate_math500_batched.py:198-205](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/02_math500-verifier-scripts/evaluate_math500_batched.py#L198-L205)

README 的实测数据是理解「批处理何时值得」的最好材料——H100 上 base 模型 500 题从 13.3 分钟降到 3.3 分钟（约 4 倍），但 Mac Mini M4 的 CPU 从 43.6 分钟涨到 167.2 分钟，DGX Spark 也从 10.0 变 16.3 分钟；同时给出 base 15.6%、reasoning 48.2% 的全量准确率：

[ch03/02_math500-verifier-scripts/README.md:104-128](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/02_math500-verifier-scripts/README.md#L104-L128)

`evaluate_json.py` 的兼容加载——先按整体 JSON 解析，失败则回退逐行 jsonl，再兼容「单对象」与 `{"records": [...]}` 两种包装：

[evaluate_json.py:38-70](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/02_math500-verifier-scripts/evaluate_json.py#L38-L70)

离线重判——对每条记录重跑同一对 `extract_final_candidate` + `grade_answer`，键名可通过参数自定义以适配自制日志：

[evaluate_json.py:73-104](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/02_math500-verifier-scripts/evaluate_json.py#L73-L104)

#### 4.4.4 代码实践（本讲核心实践·下半）

1. **实践目标**：① 用 `evaluate_json.py` 离线重算 4.3 节产出的 `math500-first5.jsonl`，验证与在线准确率一致；② 通读批处理脚本，写出它与流式版吞吐差异的三句话总结。
2. **操作步骤**：

   ```bash
   # 在仓库根目录（或 ch03/02_math500-verifier-scripts/ 下）
   python ch03/02_math500-verifier-scripts/evaluate_json.py \
       --json_path math500-first5.jsonl
   ```

   然后对照阅读 [evaluate_math500_batched.py:59-127](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/02_math500-verifier-scripts/evaluate_math500_batched.py#L59-L127) 与 [evaluate_math500.py:67-122](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/02_math500-verifier-scripts/evaluate_math500.py#L67-L122)。
3. **需要观察的现象**：`evaluate_json.py` 打印的 `Accuracy: xx.x% (c/5)` 应与 4.3 节 `eval_subset.py` 结束时打印的 Accuracy 完全相同。
4. **预期结果**：两者必然一致——离线重判用的是同一对提取/判分函数、同一份 `generated_text`，生成环节（唯一的随机/耗时来源）没有参与。
5. **三句话总结的参考答案**（可对照自己的表述）：
   - 批处理版把「一次一条」的流式生成换成 left-padding 后的整批前向（`generate_text_basic_batched_cache_stop`），把 batch 维的并行压给硬件，GPU 吞吐成倍提升（H100 实测约 4 倍）。
   - 代价是批内必须陪跑到最长序列（高效模式下已停止的行虽停算但仍占位），且一个 batch 的前向计算量与显存占用按 batch_size 叠加，在 CPU / 小显存设备上反而更慢（Mac M4 CPU 实测慢约 3.8 倍）。
   - 除生成外的链路（渲染、提取、判分、六字段 jsonl）两者逐行同构，所以日志格式与 `evaluate_json.py` 完全互通——变的是吞吐，不变的是评测协议。

#### 4.4.5 小练习与答案

**练习 1**：两个评测脚本的 `--which_model` 都接受 `instruct`，它和 `reasoning` 有何区别？
**答案**：权重相同（都加载 reasoning 权重与聊天模板分词器），区别仅在 `tokenizer.add_thinking = False`——不预填 `<think>` 块，让模型跳过显式思考直接作答（u2-l6 的模板形态之一）。

**练习 2**：批处理版为什么必须在 decode 前做「第一个 EOS 截断」？流式版需要吗？
**答案**：批内各序列长短不一，高效模式下先结束的行是陪跑（stop 之后可能继续产出无意义 token），不截断就会混入 `generated_text` 污染提取与判分。流式版的生成器内部用 `eos_token_id` 提前停止、EOS 本身不入输出（u3-l1），所以不需要这步。

**练习 3**：`--checkpoint_path` 分支的存在是为了什么？
**答案**：让第 6/7 章 GRPO 训练产出的 checkpoint 可以直接用同一套评测协议打分，形成「训练→评测」闭环；这也是后面单元会用到的标准评估入口。

**练习 4**：如果 `evaluate_json.py` 的输入文件既不是合法整体 JSON、某一行又不是合法 JSON，会发生什么？
**答案**：`load_records` 在逐行解析时抛出 `ValueError`，并带上出错的行号与原始解析异常信息（[evaluate_json.py:55-58](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch03/02_math500-verifier-scripts/evaluate_json.py#L55-L58)）。

## 5. 综合实践

把本讲全部内容串成一条「小规模但完整」的评测工作流。**A 方案**需要能跑模型的机器；**B 方案**零模型零权重，只验证评测循环本身。

**A 方案（真实模型，约几分钟到十几分钟）**

1. 用 4.3.4 的 `eval_subset.py` 在前 5 题、`max_new_tokens=128` 下评测 base 模型，得到 `math500-first5.jsonl`。
2. 用 `inspect_log.py` 逐行打印六字段，手算准确率，与函数返回的 Accuracy 核对。
3. 用 `evaluate_json.py --json_path math500-first5.jsonl` 离线重算，确认三个数字（在线打印、手算、离线重算）一致。
4. 把 `max_new_tokens` 改成 64 再跑一遍到新文件，对比两份 jsonl 里 `generated_text` 的长度与准确率变化，体会「截断过短会切断推理/作答」的效应（结果**待本地验证**，方向不预设）。

**B 方案（零计算，验证评测骨架）**

仿照 [tests/test_ch03.py:366-418](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch03.py#L366-L418) 写一个脚本（示例代码）：

```python
# eval_stub.py（示例代码）：假生成器驱动评测主循环
from pathlib import Path
import reasoning_from_scratch.ch03 as ch03

outputs = iter([
    "Let me add: 1/2 + 1/6 = 2/3.\n\\boxed{2/3}",   # 应判对
    "I think the answer might be 5.\n\\boxed{5}",   # 应判错
])
ch03.generate_text_stream_concat = lambda *a, **k: next(outputs)

math_data = [
    {"problem": "Compute #1", "answer": "2/3"},
    {"problem": "Compute #2", "answer": "7"},
]
num_correct, num_examples, acc = ch03.evaluate_math500_stream(
    model=None, tokenizer=None, device="cpu", math_data=math_data,
    out_path=Path("stub-eval.jsonl"), max_new_tokens=8,
)
print(num_correct, num_examples, acc)   # 预期: 1 2 0.5
```

注意：B 方案里 `tokenizer=None` 之所以可行，是因为假生成器绕过了真实 encode 路径，但 `total_len` 一行仍会调用 `tokenizer.encode`（[ch03.py:530](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L530)）——运行前想想它会先在哪一行报错，再实际运行验证你的判断（这正是练习的价值所在；测试里的做法是传入真实的 `load_tokenizer_only` 分词器）。

最后回到规格中的思考题：对照 4.4.3 的 README 实测表，写 3 句话总结批处理与流式版的吞吐差异来源（参考答案见 4.4.4 第 5 点）。

## 6. 本讲小结

- **四步链路定型**：`渲染(render_prompt) → 生成(generate_text_stream_concat) → 提取(extract_final_candidate) → 判分(grade_answer)`，从 `mini_eval_demo` 的 4 行到 `evaluate_math500_stream` 的主循环逐行同构——这条链是后续所有章节（推理时扩展、GRPO、蒸馏）评测的公共底座。
- **jsonl 落盘六字段**：`index / problem / gtruth_answer / generated_text / extracted / correct`，逐行追加、`ensure_ascii=False` 保原文，批处理版与流式版字段完全一致，日志互通。
- **进度反馈**：`eta_progress_message` 用线性外推算 ETA，`\r` 原地刷新 + `ljust` 等宽补空格是两个可复用的终端技巧。
- **评测 = 库函数 + 脚本壳**：`ch03.py` 提供可编程入口并返回 `(num_correct, num_examples, acc)`；`ch03/02` 的三个脚本是 argparse 壳，`--checkpoint_path` 分支为第 6 章 RL 成果预留了评测通道。
- **批处理的收益取决于硬件**：GPU 上整批前向大幅提升吞吐（H100 约 4 倍），CPU 上计算量叠加反而更慢；高效模式让已停止的行停算，只影响效率不影响结果。
- **判分与生成解耦**：`evaluate_json.py` 可对旧日志离线重算准确率——生成是最贵的环节，升级解析器时无需重新生成。

## 7. 下一步学习建议

本讲结束了单元四（评测与验证器）。接下来进入**单元五：推理时扩展（第 4 章）**——在不改权重的前提下提升推理质量：

- **u5-l1 温度采样** 与 **u5-l2 top-p 过滤**：把贪心解码换成概率采样，这是自一致性「多次采样」的前提；
- **u5-l4 自一致性投票**：直接复用本讲的评测链路（`extract_final_candidate` + MATH-500 子集），把「一次生成判分」升级为「多次采样投票」；
- 若你对本讲的批量推理实现本身感兴趣，可提前查阅 **u10-l2（qwen3_batched 与吞吐优化）**，看 `generate_text_basic_batched_cache_stop` 与 `shrink_kv_cache_inplace` 的内部机制；
- 若你想强化判分精度，**u10-l4（高级混合 LaTeX 解析器）** 展示如何用 `bonus/parser.py` 重判本讲产出的 jsonl 日志。
