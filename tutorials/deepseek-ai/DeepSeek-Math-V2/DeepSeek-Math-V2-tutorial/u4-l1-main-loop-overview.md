# 主循环全貌：main.py 的轮次编排

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 `main.py` 轮次循环中 `proof_gen_R{R}`、`proof_verification_R{R}`、`meta_verification_R{R}` 三类子目录的 `input.jsonl` / `output.jsonl` 依赖图，并说出每条边上「谁生产、谁消费」。
2. 解释第 1 轮（R1）如何从原始 `json` / `jsonl` 输入构建证明生成请求：多输入逗号拼接、`source_name` 注入、`proof_generation` 模板渲染成 `messages`。
3. 说明 `range(args.start_round, args.max_rounds + 2)` 这个「多跑一轮」的循环边界，以及最后一轮只准备精炼输入、不执行生成的收尾逻辑。
4. 理解这套流水线的断点续跑设计：`main.py` 用 `os.path.exists` 判断中间文件是否已存在来跳过「准备阶段」，真正的幂等重跑则依赖 `generate.py` 的 `.meta` 批次档案，两层各司其职。

本讲是 u4 单元的第一讲：我们只看「编排层」——数据如何在轮次之间流动；至于每个准备函数内部的解析细节（`prepare_proof_verification`、`prepare_meta_verification`）和证明池聚合（`prepare_proof_refinement`），分别留给 u4-l2、u4-l3 和 u5 单元。

## 2. 前置知识

阅读本讲前，请确认你已理解以下概念（均在前面讲义中建立）：

- **流水线四阶段**（u1-l1）：生成器写证明 → 验证器打 0/0.5/1 分 → 元验证器复核「评价是否合理」→ 精炼阶段综合多份证明与评价产出新证明，循环往复。
- **模板即契约**（u3-l1）：`math_templates.py` 里的 `proof_generation` 模板接收 `{question}`，渲染成完整的用户提示词；模型被要求以 `## Solution` 与 `## Self Evaluation` 两个小节作答。
- **generate.py 的行为**（u2-l1、u2-l2）：它读入 `input.jsonl`，把每行复制 `--n` 份（多次采样），并发请求 API，把结果追加写入 `output.jsonl`；断点档案 `output.jsonl.meta`（pickle）记录 `complete_batches`，重启时跳过已完成批次，且用 `assert` 拦截 `n` / `batch_size` 的改动。
- **宽容解析**（u1-l3）：`main.py` 和 `generate.py` 都用 `parse_known_args()`，未注册的命令行参数会被静默忽略；而 `args.proof_gen_url`、`args.proof_rate_url`、`args.infer_script` 三个属性**被使用却未在 argparse 注册**，按 README 直接运行会在取值处抛 `AttributeError`。
- **`finish_reason` 与 `</think>` 双重判据**（u2-l1）：下游只把「`finish_reason == 'stop'` 且输出含 `</think>`」的样本当作完整结果。

一个值得先建立的心智模型：`main.py` 自己**从不调用 API**。它只做两件事——把上一阶段的输出「加工」成下一阶段的 `input.jsonl`（纯本地数据处理），以及用 `os.system` 把生成任务整体外包给 `generate.py`。理解了这一点，整个 `__main__` 块就读作一个「文件加工 + 外包调用」交替出现的脚本。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [inference/main.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py) | 流水线编排主程序 | `__main__` 轮次循环（L397-L523）与 argparse 参数（L19-L64） |
| [inference/run.sh](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh) | 官方启动脚本 | 它覆盖了哪些参数，决定了循环的实际行为 |
| [inference/generate.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py) | 唯一真正发 HTTP 请求的引擎 | 它的 `.meta` 断点档案如何与 `main.py` 的文件存在性检查配合 |
| [inference/math_templates.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py) | 四个提示词模板 | R1 初始化时渲染 `proof_generation` 模板 |

## 4. 核心概念与源码讲解

### 4.1 参数全景：循环会消费哪些 argparse 参数

#### 4.1.1 概念说明

`main.py` 的模块级代码先声明约 30 个命令行参数，再用 `parse_known_args()` 解析，并把一部分结果提升为模块级变量。轮次循环的「形状」——跑几轮、每轮生成几份、要不要元验证——完全由这些参数决定。先认清参数分组，后面读循环时才不会频繁回头查表。

#### 4.1.2 核心流程

参数按语义可分五组：

| 分组 | 参数（默认值） | 对循环的影响 |
| --- | --- | --- |
| 路径（必填） | `--input_paths`、`--output_dirname`、`--proof_pool_dirname` | 决定所有中间文件的根目录与 R1 读哪些原始文件 |
| 证明生成组 | `--proof_gen_template`（proof_generation）、`--proof_gen_max_len`（128K）、`--n_parallel_proof_gen`（128）、`--n_agg_trials`（32）、`--n_best_proofs_to_sample`（32）、`--n_proofs_to_refine`（1）、`--proof_gen_num_processes`（40）等 | R1 的采样数、R≥2 的采样数（整除得到）、精炼组合的规模 |
| 证明验证组 | `--proof_verification_template`、`--n_verification_per_proof`（4）、`--proof_verification_num_processes`（320）等 | 每份证明被验证几次 |
| 元验证组 | `--skip_meta_verification`（开关）、`--n_meta_verification_per_rating`（1）、`--meta_verification_num_processes`（320）等 | 元验证阶段是否执行 |
| 轮次控制 | `--start_round`（1）、`--max_rounds`（20） | 循环的起点与终点 |

对照 [inference/run.sh](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh) 可见，官方竞赛配置相对默认值真正改变的只有三处：验证强度 `--n_verification_per_proof` 从 4 提到 64（16 倍）、加上 `--skip_meta_verification`、`--max_rounds` 从 20 改为 16；其余采样类参数与默认值一致。

#### 4.1.3 源码精读

参数声明集中在 [inference/main.py:L19-L55](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L19-L55)：L20-L22 是三个必填路径参数；L31-L34 是采样规模参数；L44 的 `--skip_meta_verification` 用 `action='store_true'`（不传即为假）；L52-L53 是轮次控制。L55 的 `parse_known_args()` 返回 `(args, 未知参数列表)`，未知参数被丢弃——这就是「宽容解析」。

解析之后，模块级还有三行提升变量：[inference/main.py:L57-L64](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L57-L64) 把 `input_paths`、`output_dirname`、`proof_pool_dirname` 赋为短名，L61-L62 取 `args.proof_gen_url` 与 `args.proof_rate_url`——**这两个属性没有注册**，不修补参数就在此抛 `AttributeError`（u1-l3 已定位）。另外注意一个此前未点破的事实：`proof_rate_url` 在整个文件里只出现在 L62 这一次赋值，之后再未被使用，属于「声明了但没接线」的残留变量；真正被消费的只有 `proof_gen_url`（L453 拼进生成命令）。L64 还派生了一个开关 `proof_gen_with_self_eval`：只要生成模板是 `proof_generation` 就为真，供 `prepare_proof_verification` 决定是否解析自评小节。

#### 4.1.4 代码实践

1. **实践目标**：用 `--help` 把参数全景dump出来，并与 `run.sh` 对照出「官方配置改了什么」。
2. **操作步骤**：在 `inference/` 目录下运行 `python main.py --help`（u1-l3 讲过 `--help` 会提前退出，因此不会碰到未注册属性的 `AttributeError`）；再打开 `run.sh` 逐行比对。
3. **需要观察的现象**：帮助文本列出的参数总数；`--skip_meta_verification` 是否在列表中（它是开关，帮助文本里没有值占位符）。
4. **预期结果**：帮助文本列出约 30 个参数；`run.sh` 显式传入 9 个，其中与默认值不同的只有 `n_verification_per_proof`（64 对 4）、`skip_meta_verification`（开）、`max_rounds`（16 对 20）。若你的输出与此不符，先确认目录与 HEAD 是否正确。

#### 4.1.5 小练习与答案

**练习 1**：`parse_known_args()` 相比 `parse_args()`，对这条流水线的启动有什么实际影响？
**答案**：未注册的参数不报错而是被静默收集到返回值第二个元素里并被丢弃。因此 `main.py` 拼给 `generate.py` 的 `--api_url`（generate.py 同样未注册它）会被静默忽略；反过来，用户误传的拼错参数名也不会被拦截，容易造成「以为生效了其实没有」的隐患。

**练习 2**：`--skip_meta_verification` 为什么用 `action='store_true'` 而不是 `type=bool`？
**答案**：`type=bool` 对字符串做 `bool()` 转换，任何非空字符串（包括 `"False"`）都是真；`store_true` 则是「命令行里出现即为真、不出现为假」，才符合开关语义。

### 4.2 轮次循环骨架：为什么是 range(start_round, max_rounds + 2)

#### 4.2.1 概念说明

整个编排层只有一个 `for` 循环。理解它的第一道坎是循环边界：写的是 `max_rounds + 2` 而不是 `max_rounds + 1`，即 `R` 会取到 `max_rounds + 1`——比「直觉上的最后一轮」多跑一次。这个多出来的「收尾轮」不执行生成，只把第 `max_rounds` 轮的验证结果加工成一份精炼输入文件，作为流水线的最终产物落盘。

#### 4.2.2 核心流程

```
for R in range(start_round, max_rounds + 2):        # R 最大取到 max_rounds + 1
    ├─ 若 proof_gen_R{R}/input.jsonl 不存在：
    │    ├─ R == 1：从原始输入构建（见 4.3）
    │    └─ R ≥ 2：从 R-1 轮验证结果构建精炼输入（见 4.4）
    │         └─ 若 R == max_rounds + 1：break（收尾轮到此为止）
    ├─ 拼命令并 os.system：证明生成（n = n_parallel 或其整除 n_agg_trials）
    ├─ 若 proof_verification_R{R}/input.jsonl 不存在：准备验证输入
    ├─ 拼命令并 os.system：证明验证（n = n_verification_per_proof）
    └─ 若未 skip 元验证：
         ├─ 若 meta_verification_R{R}/input.jsonl 不存在：准备元验证输入
         └─ 拼命令并 os.system：元验证（n = n_meta_verification_per_rating）
```

每轮固定三类子目录：`proof_gen_R{R}`、`proof_verification_R{R}`、`meta_verification_R{R}`，各含 `input.jsonl` 与 `output.jsonl`。

#### 4.2.3 源码精读

循环头与路径模板在 [inference/main.py:L397-L401](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L397-L401)：L398 的 `range(args.start_round, args.max_rounds + 2)` 是整个编排的「总闸」；L399-L400 用 f-string 拼出本轮两个核心路径。L401 的 `if not os.path.exists(proof_gen_input_path):` 是第一处断点续跑检查——只要该轮生成输入已在磁盘上，初始化/精炼准备整个跳过，直接进入外包调用。

收尾轮的 `break` 在 [inference/main.py:L445-L446](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L445-L446)：`if R == args.max_rounds + 1: break`。注意它的嵌套位置——在 `if not os.path.exists(...)` 分支**内部**。这意味着：全新运行时，收尾轮的精炼输入必然不存在、刚被构建，于是 `break` 生效，流水线在第 `max_rounds` 轮的元验证之后只多产出一份 `proof_gen_R{max_rounds+1}/input.jsonl` 就结束；但如果这份文件已经存在（例如完整跑完后重跑同一命令），`break` 不会触发，循环会继续对收尾轮执行生成、验证、元验证，直到 `R` 自然越过边界退出。这是「文件存在性当控制流」带来的隐蔽行为差异，值得在读代码时留个心眼。

#### 4.2.4 代码实践

1. **实践目标**：用最小代码验证循环边界与收尾轮行为。
2. **操作步骤**：在 Python 里模拟循环头，把「构建输入」和「break」打印出来（示例代码，非项目源码）：

   ```python
   start_round, max_rounds = 1, 2
   for R in range(start_round, max_rounds + 2):
       print(f"R={R}", end=" ")
       if R == max_rounds + 1:
           print("-> 构建收尾轮精炼输入后 break")
           break
       print("-> 生成 + 验证 + 元验证")
   ```

3. **需要观察的现象**：`R` 依次取哪些值；`break` 发生在第几次迭代。
4. **预期结果**：输出 `R=1 -> 生成+验证+元验证`、`R=2 -> 生成+验证+元验证`、`R=3 -> 构建收尾轮精炼输入后 break`。即 `max_rounds=2` 时循环体实际进入 3 次，第 3 次只准备不执行。

#### 4.2.5 小练习与答案

**练习 1**：把 `range(args.start_round, args.max_rounds + 2)` 改成 `+ 1` 会少产出什么文件？
**答案**：少产出 `{output_dirname}/proof_gen_R{max_rounds+1}/input.jsonl`。这份文件由第 `max_rounds` 轮全部验证结果汇总而成的精炼请求构成，是流水线「还想再改一轮」的最终快照；`+2` 正是为了让循环进入这个收尾轮。

**练习 2**：`--start_round 3` 配合已存在的中间文件，循环会怎么走？
**答案**：`R` 从 3 开始。若 `proof_gen_R3/input.jsonl` 已存在则跳过准备直接重跑生成命令（幂等性由 generate.py 的 `.meta` 保证）；若不存在且 R>1，则用第 2 轮的验证输出构建。`start_round` 只是循环起点，不改变任何一轮内部的逻辑。

### 4.3 第 1 轮初始化：读原始数据、注入 source_name、渲染模板

#### 4.3.1 概念说明

R1 是唯一「没有上一轮」的轮次，它的输入直接来自命令行 `--input_paths` 指向的原始竞赛题文件（u1-l2 讲过的五字段 JSON）。初始化要做三件事：把多个输入文件合并进同一条流水线、给每条记录打上来源标签 `source_name`（它将决定证明池的子目录名，u5-l1 展开）、把题面渲染成 `proof_generation` 提示词并塞进 `messages`——后两者共同把「原始题目」改造成 generate.py 可直接消费的请求。

#### 4.3.2 核心流程

```
input_paths 按 "," 切分为多个路径
对每个路径：
    source_name = 路径最后一段去掉扩展名        # ../IMO2025.json -> IMO2025
    按后缀读取：.json 整体 json.load；其余按行 jsonl 读
    为每条记录注入 item['source_name']
合并所有文件的数据
对每条记录：
    用 proof_generation 模板渲染 question -> 提示词
    item['messages'] = [{"role": "user", "content": 提示词}]
写入 {output_dirname}/proof_gen_R1/input.jsonl
```

#### 4.3.3 源码精读

R1 分支在 [inference/main.py:L402-L428](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L402-L428)。L404 按逗号切分多输入；L405 一行完成 `source_name` 提取：先 `split("/")[-1]` 取文件名，再 `split(".")[0]` 去扩展名——因此 `../IMO2025.json` 得到 `IMO2025`。L406-L412 按后缀分流：`.json` 走 `json.load` 整体读，其他（`.jsonl`）逐行解析。L414 给每条记录注入 `source_name` 字段，此后该字段会随数据一路流转到证明池阶段。L417-L424 逐条渲染：L418 用 `math_templates[args.proof_gen_template].format(question=...)` 把题面套进模板（模板要求模型输出 Solution 与 Self Evaluation 两个小节，u3-l1），L419-L423 把渲染结果写进 `messages`。L425-L428 建目录并以逐行 `print(json.dumps(...), file=...)` 的方式写 JSONL——与项目「中间产物一律 JSONL、支持追加写」的约定一致（u1-l2）。

注意原始记录的其余字段（`id`、`answer`、`contest`、`problem_idx` 等）原样保留在每行里，它们会在后续阶段继续随行流转，`problem_idx` 更是贯穿全程的主键。

#### 4.3.4 代码实践

1. **实践目标**：验证 `source_name` 推导与两种读取分支的选择逻辑。
2. **操作步骤**：运行下面这段与 L404-L412 等价的逻辑（示例代码，非项目源码）：

   ```python
   input_paths = "../IMO2025.json,../CMO2024.json,../CMO2025.json"   # run.sh L3 的原字符串
   for input_path in input_paths.split(","):
       source_name = input_path.split("/")[-1].split(".")[0]
       branch = "json.load" if input_path.endswith(".json") else "逐行 jsonl"
       print(f"{input_path:22s} -> source_name={source_name:8s} 走 {branch} 分支")
   ```

3. **需要观察的现象**：三个路径各自得到的 `source_name` 与读取分支。
4. **预期结果**：`IMO2025`、`CMO2024`、`CMO2025` 三个来源名，全部走 `json.load` 分支（仓库 inputs/ 下都是 `.json`）。可再试试把路径换成 `foo/bar.jsonl` 观察分支切换。若想更进一步，可用 u1-l2 的 `inspect_data.py` 输出的题目数，推算 R1 输入行数应等于三份文件题目数之和。

#### 4.3.5 小练习与答案

**练习 1**：如果两个输入文件名去掉扩展名后相同（如 `a/IMO.json` 与 `b/IMO.json`），会发生什么？
**答案**：两份数据的 `source_name` 都是 `IMO`，它们的证明会混入同一个证明池子目录 `{proof_pool_dirname}/IMO/`。由于证明池按 `problem_idx` 分文件（u5-l1），只要题号不冲突仍可运行，但来源信息被合并、可追溯性下降——这是 `source_name` 推导只看文件名的固有弱点。

**练习 2**：为什么 R1 输出的是 `input.jsonl` 而不是直接调用生成？
**答案**：编排层把「准备请求」与「执行请求」解耦成两个文件阶段：所有输入先落盘成 JSONL，再由 generate.py 统一读取。这样准备逻辑可以单独重跑与检查（文件存在即跳过），也天然获得 generate.py 层面的批量并发与断点续跑能力。

### 4.4 第 2 轮起：精炼输入从上一轮验证结果汇合而来

#### 4.4.1 概念说明

R≥2 的生成输入不再来自原始题目，而是「精炼请求」：把上一轮每道题的所有证明、所有验证评价（以及元验证对评价的质量复核）汇总，挑选组合、拼成摘要，再套进 `proof_refinement` 组合模板。这份准备工作体量很大（涉及证明池读写、组合枚举、评价采样），被封装成 `prepare_proof_refinement` 函数——本讲只关注它在循环里的接线方式与轮次间的数据依赖，函数 internals 是 u5 单元三讲的主角。

#### 4.4.2 核心流程

```
R ≥ 2 时：
    读取 {output_dirname}/proof_verification_R{R-1}/output.jsonl     # 上一轮验证结果
    读取 {output_dirname}/meta_verification_R{R-1}/output.jsonl     # 上一轮元验证结果（若存在）
    prepare_proof_refinement(...)
    写出 {output_dirname}/proof_gen_R{R}/input.jsonl
    （R == max_rounds+1 则 break）
```

随后生成条数发生变化：

\[ n_{\text{sample}} = \begin{cases} n_{\text{parallel\_proof\_gen}} & R = 1 \\ \left\lfloor n_{\text{parallel\_proof\_gen}} / n_{\text{agg\_trials}} \right\rfloor & R \ge 2 \end{cases} \]

直觉解释：R1 对每道题发 `n_parallel_proof_gen`（如 128）份原始生成请求；R≥2 的输入文件里每道题已含约 `n_agg_trials`（如 32）条精炼请求，每条再采样 \( \lfloor 128/32 \rfloor = 4 \) 份，使每题每轮的证明产量大致守恒在 128。

#### 4.4.3 源码精读

R≥2 分支在 [inference/main.py:L429-L446](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L429-L446)：L430-L431 拼出上一轮两个输入路径——精炼准备同时消费验证输出与元验证输出，这正是循环里唯一一处「跨轮、跨阶段」的数据汇合点。L432-L444 调用 `prepare_proof_refinement`，关键字参数全部来自 argparse：`num_trials=args.n_agg_trials`、`n_best_proofs_to_sample`、`n_proofs_to_refine` 由命令行透传，`max_rating_per_score=4` 与 `drop_thought=True` 则硬编码；`use_old_proofs_for_refinement=True` 表示精炼时还会并入历史证明池里的旧证明（u5-l1）。L445-L446 是上节讲过的收尾轮 `break`。

采样数切换在 [inference/main.py:L448](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L448)：一行条件表达式决定 `n_sample`。结合 generate.py 的实现看边界情况——它对每行输入执行 `for i in range(n)` 复制（[inference/generate.py:L141](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L141)），若 \( n_{\text{parallel}} < n_{\text{agg\_trials}} \) 则 `n_sample=0`、`range(0)` 一次都不复制，将产出一份额外的空 `output.jsonl`。按 run.sh 的 128/32 配置则为 4，无此问题。

#### 4.4.4 代码实践

1. **实践目标**：亲手算出各轮的采样数，并找出会让 R≥2 完全不生成的参数组合。
2. **操作步骤**（示例代码，非项目源码）：

   ```python
   def n_sample(R, n_parallel, n_agg_trials):
       return n_parallel if R == 1 else n_parallel // n_agg_trials

   for cfg in [(128, 32), (128, 16), (8, 32)]:
       print(cfg, "->", [n_sample(R, *cfg) for R in (1, 2, 3)])
   ```

3. **需要观察的现象**：三种配置下 R1 与 R≥2 的采样数。
4. **预期结果**：`(128,32) -> [128, 4, 4]`；`(128,16) -> [128, 8, 8]`；`(8,32) -> [8, 0, 0]`。第三组说明小算力调试时若 `n_agg_trials` 大于 `n_parallel_proof_gen`，第二轮起将静默产出空输出——调试参数时要避开这个组合。

#### 4.4.5 小练习与答案

**练习 1**：`prepare_proof_refinement` 的两个输入路径分别来自哪个阶段？为什么元验证路径用 `os.path.exists` 容错（在函数内部检查）而验证路径不检查？
**答案**：验证输出来自上一轮 `proof_verification_R{R-1}`，元验证输出来自上一轮 `meta_verification_R{R-1}`。开了 `--skip_meta_verification` 时元验证文件根本不会生成，所以函数内部（L298）用存在性判断容错；验证输出是精炼的必需原料，缺失即应尽快报错暴露问题。

**练习 2**：为什么 R≥2 要做整除而不是让每条精炼请求都采样 `n_parallel` 份？
**答案**：R≥2 的输入行数已从「每题 1 行」膨胀为「每题约 `n_agg_trials` 行」。若每行再采样 `n_parallel` 份，每题每轮请求量会放大 32 倍；整除把总量拉回与 R1 同一量级，是控制测试时算力的关键设计（u6-l1 展开）。

### 4.5 三条命令与两层断点续跑

#### 4.5.1 概念说明

每轮循环拼三条命令并各执行一次 `os.system`。三条命令结构高度相似（都是调用生成脚本 + 采样与并发参数），但有两处不对称值得注意：生成命令引用了未注册的 `args.infer_script` 与 `proof_gen_url`，而两条验证命令直接硬编码 `python generate.py` 且不带 API 地址参数。断点续跑则分两层：`main.py` 用文件存在性跳过「准备」，`generate.py` 用 `.meta` 跳过「已完成批次」——前者管编排幂等，后者管请求幂等。

#### 4.5.2 核心流程

三条命令的拼装与执行：

| 阶段 | 输入 → 输出 | 脚本 | 采样 `--n` | 并发进程 |
| --- | --- | --- | --- | --- |
| 证明生成 | `proof_gen_R{R}/input` → `output` | `{args.infer_script}.py` + `--api_url` | `n_sample`（4.4） | `proof_gen_num_processes` |
| 证明验证 | `proof_verification_R{R}/input` → `output` | 硬编码 `generate.py` | `n_verification_per_proof` | `proof_verification_num_processes` |
| 元验证 | `meta_verification_R{R}/input` → `output` | 硬编码 `generate.py` | `n_meta_verification_per_rating` | `meta_verification_num_processes` |

两层续跑协作：

```
重启 main.py 后，对每一轮 R：
  input.jsonl 已存在？ ──是──> 跳过准备函数（main.py 层）
        │否
        ▼
  重新执行准备函数（确定性重建同一份文件）
  os.system 生成命令 ——> generate.py 读 .meta：
        complete_batches 中的批次直接跳过（generate.py 层）
        n / batch_size 与档案不一致 → assert 拒绝运行
```

#### 4.5.3 源码精读

生成命令在 [inference/main.py:L449-L462](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L449-L462)：L450 用 `{args.infer_script}.py` 指定脚本名（`infer_script` 未注册，修补时应设为 `generate`）、L453 拼入 `--api_url {proof_gen_url}`（接收端未注册该参数，实际被静默忽略，u2-l1）、L459 填入 `n_sample`。L461-L462 先打印完整命令再 `os.system` 执行——**返回值未被检查**：子进程失败不会中断主流程，主流程随后会在准备下一阶段输入时因读不到完整的 `output.jsonl` 而崩溃（文件缺失时 `read_data` 打不开文件抛异常；文件半成品时则会被静默消费，风险更隐蔽）。

验证阶段的准备与命令在 [inference/main.py:L472-L492](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L472-L492)：L474 的存在性检查守护 `prepare_proof_verification`（u4-l2 主角）；L481 起的命令硬编码 `python generate.py`、无 `--api_url`，与生成命令不对称。元验证阶段在 [inference/main.py:L502-L523](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L502-L523)：L504 先判 `--skip_meta_verification` 开关（run.sh 开了它，所以官方竞赛跑法没有这个阶段），L505 再做存在性检查，L511-L523 拼命令并执行，结构与验证阶段完全平行。

第二层续跑的锚点在 generate.py：断点档案路径 `{output}.meta`、参数一致性 `assert` 与批次跳过逻辑位于 [inference/generate.py:L104-L114](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L104-L114)（u2-l2 已精读）。两层各管一段：`main.py` 的检查避免重复重建输入文件，`generate.py` 的 `.meta` 保证重启后不重复发已完成的请求。特别注意 `main.py` 从不检查 `output.jsonl` 是否完整——重启后 `os.system` 无条件重跑，正确性完全托付给 `.meta`。

#### 4.5.4 代码实践

1. **实践目标**：通过阅读命令字符串与 `.meta` 逻辑，推演「中断—重启」两种场景各发生什么。
2. **操作步骤**：假设 `max_rounds=2`、元验证开启，分别推演：(a) 第 1 轮生成进行到一半被 Ctrl-C；(b) 第 1 轮全部完成、第 2 轮尚未开始时中断。写下重启后每一步是「跳过准备」「重跑生成但跳过已完成批次」还是「从头执行」，再对照 [inference/main.py:L401](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L401)、[L474](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L474) 与 [inference/generate.py:L144-L148](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L144-L148) 核对。
3. **需要观察的现象**：你的推演里有多少步依赖 `.meta` 而不是 `os.path.exists`。
4. **预期结果**：场景 (a)：`proof_gen_R1/input.jsonl` 已存在 → 跳过准备；生成命令重跑，已完成批次被 `.meta` 跳过、仅补跑剩余批次；后续阶段正常首跑。场景 (b)：R1 三个阶段全部跳过准备、三个生成命令重跑但全部批次命中 `.meta`（等效空转），R2 从精炼准备开始真正工作。若重启时改了 `n` 或 `batch_size`，generate.py 的 `assert`（L113）会直接拒绝。实际运行效果待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：生成命令带 `--api_url` 而验证命令不带，这个不对称有什么实际后果？
**答案**：几乎没有后果——`generate.py` 用 `parse_known_args` 且未注册 `--api_url`，参数本就被静默忽略；API 地址真正的唯一入口是 `APIModel.__init__` 里硬编码的 `api_key` / `base_url`（u2-l1）。这个不对称更多是发布代码的毛刺：生成命令还额外依赖两个未注册属性，导致不修补就无法按 README 直接跑通。

**练习 2**：如果 `os.system` 的生成子进程失败了，`main.py` 会立刻报错吗？
**答案**：不会。返回值未被检查，主流程继续走到下一个准备函数；那里 `read_data(proof_gen_output_path)` 打不开缺失文件时才抛 `FileNotFoundError`。若失败前已写入部分行，则更糟——残缺数据会被静默消费，因此长跑前建议先小规模验证配置（u6-l2 的加固实践会处理这一点）。

## 5. 综合实践

把本讲全部内容串成一张可复算的依赖图：编写 `flow_diagram.py`（示例代码，非项目源码），把 `main.py` L397-L523 的轮次循环抽象成「事件序列」，打印 ASCII 依赖图与文件产生顺序清单。

```python
"""flow_diagram.py —— 把 main.py 的轮次循环抽象成依赖图（不调用 API、不写业务文件）"""
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--output_dirname", default="outputs_run")
parser.add_argument("--input_paths", default="../IMO2025.json")
parser.add_argument("--start_round", type=int, default=1)
parser.add_argument("--max_rounds", type=int, default=2)
parser.add_argument("--skip_meta_verification", action="store_true")
parser.add_argument("--n_parallel_proof_gen", type=int, default=128)
parser.add_argument("--n_agg_trials", type=int, default=32)
args = parser.parse_args()

events = []  # (序号, 动作, 产物路径)

def prepare(desc, path):
    events.append(("准备", desc, path))

def infer(stage, R, n):
    path = f"{args.output_dirname}/{stage}_R{R}/output.jsonl"
    events.append(("生成", f"{stage}_R{R} 经 generate.py (--n {n})", path))

for R in range(args.start_round, args.max_rounds + 2):
    gen_input = f"{args.output_dirname}/proof_gen_R{R}/input.jsonl"
    if R == 1:
        src = ", ".join(p.split("/")[-1] for p in args.input_paths.split(","))
        prepare(f"R1 读取原始输入 [{src}] 并渲染 proof_generation 模板", gen_input)
    else:
        prepare(f"R{R} 汇合 R{R-1} 验证与元验证结果（prepare_proof_refinement）", gen_input)
        if R == args.max_rounds + 1:
            break  # 收尾轮：只准备，不执行
    n_sample = args.n_parallel_proof_gen if R == 1 else args.n_parallel_proof_gen // args.n_agg_trials
    infer("proof_gen", R, n_sample)
    prepare(f"R{R} 解析证明输出（prepare_proof_verification）",
            f"{args.output_dirname}/proof_verification_R{R}/input.jsonl")
    infer("proof_verification", R, 4)  # n_verification_per_proof
    if not args.skip_meta_verification:
        prepare(f"R{R} 筛选低分评价（prepare_meta_verification）",
                f"{args.output_dirname}/meta_verification_R{R}/input.jsonl")
        infer("meta_verification", R, 1)  # n_meta_verification_per_rating

print("=== 依赖图（-> 表示数据流）===")
prev = "原始输入"
for kind, desc, path in events:
    print(f"{prev}\n  --[{kind}] {desc}-->\n{path}")
    prev = path
print("\n=== 文件产生顺序清单 ===")
for i, (kind, _, path) in enumerate(events, 1):
    print(f"{i:2d}. [{kind}] {path}")
```

操作步骤：

1. 运行 `python flow_diagram.py --max_rounds 2`（默认开启元验证），对照打印结果与本讲 4.2-4.5 的讲解逐条核对。
2. 再运行 `python flow_diagram.py --max_rounds 2 --skip_meta_verification`，观察元验证两个事件消失。
3. 把 `--max_rounds` 换成 3、把 `--n_parallel_proof_gen` 换成 8，观察收尾轮位置与 `--n` 的变化。

预期结果（`--max_rounds 2`、元验证开启时）应得到 13 个事件，即完整文件产生顺序：

| # | 动作 | 文件 |
| --- | --- | --- |
| 1 | 准备 | `{out}/proof_gen_R1/input.jsonl` |
| 2 | 生成 | `{out}/proof_gen_R1/output.jsonl`（含 `.meta`） |
| 3 | 准备 | `{out}/proof_verification_R1/input.jsonl` |
| 4 | 生成 | `{out}/proof_verification_R1/output.jsonl`（含 `.meta`） |
| 5 | 准备 | `{out}/meta_verification_R1/input.jsonl` |
| 6 | 生成 | `{out}/meta_verification_R1/output.jsonl`（含 `.meta`） |
| 7 | 准备 | `{out}/proof_gen_R2/input.jsonl`（同时向证明池追加写入） |
| 8 | 生成 | `{out}/proof_gen_R2/output.jsonl`（`--n` 从 128 变为 4） |
| 9-12 | …… | 第 2 轮验证、元验证的输入与输出，同 3-6 |
| 13 | 准备 | `{out}/proof_gen_R3/input.jsonl`（收尾轮，只准备不执行） |

若脚本输出与此清单一致，说明你已能把 `main.py` 的编排逻辑完整复述出来。证明池文件在第 7、13 步的追加写入细节属 u5-l1 范围，本讲只需知道它发生在精炼准备阶段。

## 6. 本讲小结

- `main.py` 的编排层是一个 `for R in range(start_round, max_rounds + 2)` 循环：`+2` 让 `R` 多取一个收尾轮，该轮只构建 `proof_gen_R{max_rounds+1}/input.jsonl` 便 `break`，不执行生成。
- 每轮三类子目录 `proof_gen_R{R}` / `proof_verification_R{R}` / `meta_verification_R{R}`，各含 `input.jsonl` 与 `output.jsonl`；准备函数在本地加工上一阶段输出，`os.system` 把执行整体外包给 generate.py。
- R1 初始化：多输入逗号拼接、`source_name` 取文件名去扩展名、`proof_generation` 模板渲染进 `messages`，原始字段随行流转。
- R≥2 的精炼输入由上一轮验证与元验证两路输出汇合而成；采样数从 `n_parallel_proof_gen` 切换为 \( \lfloor n_{\text{parallel}} / n_{\text{agg\_trials}} \rfloor \)，整除为零时第二轮起会静默产出空输出。
- 断点续跑分两层：`main.py` 用 `os.path.exists` 跳过输入准备，`generate.py` 用 `.meta` 的 `complete_batches` 跳过已完成批次；`os.system` 返回值不检查，失败会在下一个准备函数处暴露。
- 生成命令依赖未注册的 `args.infer_script` 与 `args.proof_gen_url`（且 `proof_rate_url` 赋值后从未使用），两条验证命令硬编码 `generate.py`——按 README 直接运行需先补参数（u1-l3）。

## 7. 下一步学习建议

下一讲 u4-l2 进入第一个准备函数 `prepare_proof_verification`（L66-L116）：看它如何过滤被截断的样本、用 `</think>` 切出证明正文、从 Self Evaluation 小节解析自评分，并渲染 `proof_verification` 模板。建议先复习 u3-l2 的 `extract_solution` / `extract_self_eval` / `extract_boxed_answers` 三个解析函数，它们是那一讲的工具箱。若想先攻聚合侧，也可跳到 u5 单元读 `prepare_proof_refinement` 的证明池部分，但建议按 u4-l2 → u4-l3 → u5 的顺序推进，数据流更连贯。
