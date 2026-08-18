# 运行方式与参数总览：从 run.sh 到 main.py

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出整条启动链条：`inference/README.md` → `run.sh` → `main.py` →（`os.system` 拼命令）→ `generate.py`，以及每一环各自负责什么。
2. 逐项解释 `run.sh` 里出现的每个参数的含义，并能对照 `main.py` 的 argparse 默认值，看出「竞赛配置」比「代码默认配置」强在哪里。
3. 在 `generate.py` 中定位 API Key 与 base_url 的唯一填写位置（`APIModel.__init__`）。
4. 指出发布代码中的一个真实毛刺（待确认）：`args.proof_gen_url`、`args.proof_rate_url`、`args.infer_script` 三个属性在代码中被使用、却从未在 argparse 中注册，导致按 README 的说明直接运行 `main.py` 必然抛 `AttributeError`，并知道如何补上这三行注册代码。

本讲是「跑起来」的一讲：不深入任何函数内部逻辑（那是 u4、u5 的事），只解决「程序从哪里进、参数怎么流、卡在哪、怎么修」。

## 2. 前置知识

### 2.1 命令行参数与 argparse

Python 标准库 `argparse` 用来解析命令行参数。典型写法：

```python
parser = argparse.ArgumentParser()
parser.add_argument("--input_paths", required=True)   # 注册一个参数
args = parser.parse_args()                            # 解析后通过 args.xxx 访问
```

两个本讲会用到的细节：

- **`parse_known_args()` vs `parse_args()`**：本仓库两个脚本都用了 `args, _ = parser.parse_known_args()`。`parse_known_args` 遇到**未注册**的命令行参数时不报错，而是把它们收进返回值 `_` 里静默忽略。这个「宽容」正是后面 4.5 节毛刺能藏住一层的原因——命令行里多传参数没人管，但代码里访问 `args.某属性` 时，如果这个属性没注册，照样崩溃。
- **`--help` 提前退出**：`python main.py --help` 会在解析阶段打印帮助并直接退出，**不会执行**后面的模块级代码。这一点后面实践会用到。

### 2.2 OpenAI 兼容接口：api_key 与 base_url

本仓库不加载模型权重，而是通过 HTTP 请求调用一个「兼容 OpenAI Chat Completions 协议」的推理服务（可以是官方 API，也可以是 vLLM 等本地部署的服务）。连接这种服务需要两个信息：

- `api_key`：鉴权字符串；
- `base_url`：服务地址，例如 `https://api.openai.com/v1` 或本地 `http://127.0.0.1:8000/v1`。

这两个值在代码里是**硬编码占位符**，需要你自己填（见 4.4 节）。

### 2.3 shell 脚本与 os.system

- `run.sh` 是一个 bash 脚本：先把若干变量赋值，再把它们展开成一条 `python main.py ...` 长命令执行。
- `main.py` 内部则用 `os.system(命令字符串)` 再去启动 `generate.py` 子进程。注意 `os.system` **不检查返回值**——子进程失败了，`main.py` 也会若无其事地继续下一阶段。这是后面实践中要重点防范的坑。

### 2.4 一条贯穿全流水线的计数规则

`generate.py` 会把输入文件的每一行复制 \( n \) 份再发请求（`--n` 参数，见 4.3 节）。所以有一个通用公式：

\[ \text{某阶段 output.jsonl 条数} = \text{该阶段 input.jsonl 条数} \times n_{\text{该阶段}} \]

例如第一轮：输入 1 道题、`n_parallel_proof_gen = 2`，则证明生成输出 2 条；验证阶段对每条证明再复制 `n_verification_per_proof` 份。记住这个乘法，实践时核对输出条数就有底了。

## 3. 本讲源码地图

`inference/` 目录下一共只有 6 个文件：

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [inference/README.md](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/README.md#L1-L2) | 两句话的运行说明 | 官方指定的启动步骤 |
| [inference/run.sh](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh#L1-L22) | bash 启动脚本 | 参数如何传给 main.py |
| [inference/main.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L19-L65) | 多轮流水线编排器 | argparse 段（第 19–65 行） |
| [inference/generate.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L15-L21) | 通用 API 批量生成引擎 | `APIModel.__init__` 里的 API 配置 |
| inference/math_templates.py | 四个提示词模板 | 本讲只当字符串常量用，u3-l1 精读 |
| inference/utils.py | boxed 提取、章节切分等 | 本讲只用到 `read_data`，u3-l2 精读 |

## 4. 核心概念与源码讲解

### 4.1 启动链条总览：从 README 到 generate.py

#### 4.1.1 概念说明

这个项目「怎么跑」的信息极少，官方说明只有两句话，完整链条需要从代码里挖出来。理解这条链条，你才知道改动哪个文件会影响哪个环节：

```
inference/README.md            （告诉你：先改 generate.py 填 key，再跑 run.sh）
        │
        ▼
inference/run.sh               （把几十个参数拼成一条命令）
        │  python main.py --input_paths ... --max_rounds 16 ...
        ▼
inference/main.py              （模块级：读参数；__main__：R 轮循环编排）
        │  os.system("python generate.py --input_data_path ... --n ...")
        ▼
inference/generate.py          （真正发 HTTP 请求的引擎，多进程 + 异步）
```

关键认知：`main.py` 自己**从不直接调用 API**。它只做两件事——准备各阶段的 `input.jsonl`（渲染模板、过滤解析），然后用 `os.system` 把生成任务外包给 `generate.py`。发请求的代码只在 `generate.py` 一处。

#### 4.1.2 核心流程

1. 使用者按 README 要求，先在 `generate.py` 里填好 `api_key` 与 `base_url`。
2. `run.sh` 定义变量并展开成 `python main.py ...` 命令，工作目录是 `inference/`（所以 `--input_paths ../IMO2025.json` 用的是相对上一级的路径）。
3. `main.py` 进入 `for R in range(start_round, max_rounds + 2)` 的轮次循环，每轮依次执行三个阶段：证明生成 → 证明验证 →（可选）元验证。
4. 每个阶段都遵循同一模式：
   - 若 `input.jsonl` 不存在 → 用上一阶段输出准备它（断点续跑的关键判断）；
   - 拼 `python generate.py ...` 命令字符串，`os.system` 执行；
   - `generate.py` 读 `input.jsonl`、逐条复制 \( n \) 份、并发请求、追加写 `output.jsonl`。

#### 4.1.3 源码精读

官方运行说明全文只有两行——[inference/README.md:L1-L2](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/README.md#L1-L2)：第 1 行说明这是「基于证明的评估（proof-based evaluation）」示例代码；第 2 行给出两个步骤：先在 `generate.py` 里填 api key，再运行 `run.sh`。

`main.py` 主循环的骨架在 [inference/main.py:L397-L448](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L397-L448)：第 398 行的循环上限是 `max_rounds + 2`（不是 `max_rounds`！多出的那次迭代用于准备「下一轮精炼输入」然后 `break`，见第 445–446 行）；第 399–400 行按 `f"{output_dirname}/proof_gen_R{R}/input.jsonl"` 模板拼路径；第 401 行用 `os.path.exists` 判断是否需要重新准备该文件——这就是断点续跑机制的全部秘密：**文件在，就跳过这个阶段**。

外包调用的三处 `os.system` 分别位于：

- 证明生成：[inference/main.py:L449-L462](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L449-L462)（第 450 行引用 `args.infer_script`，第 453 行把 `proof_gen_url` 塞进 `--api_url`，第 462 行 `os.system` 执行）；
- 证明验证：[inference/main.py:L480-L492](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L480-L492)；
- 元验证：[inference/main.py:L511-L523](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L511-L523)。

注意后两条命令**没有** `--api_url`——再次印证真正的 API 配置不在命令行参数里，而在 `generate.py` 内部。

#### 4.1.4 代码实践

1. **实践目标**：不运行任何东西，仅靠检索确认「main.py 通过 os.system 调用 generate.py 共 3 处、且自己不 import openai」。
2. **操作步骤**：
   - 在仓库根目录执行 `grep -n "os.system" inference/main.py`；
   - 再执行 `grep -n "openai\|requests\|aiohttp" inference/main.py`。
3. **需要观察的现象**：第一条命令恰好输出 3 行（L462、L492、L523）；第二条命令**无任何输出**。
4. **预期结果**：确认「编排」与「请求」完全分离——`main.py` 里没有任何网络库。
5. 若你的 grep 结果与上述不一致，请先核对仓库 HEAD 是否为 `665c840`。

#### 4.1.5 小练习与答案

**练习 1**：`inference/README.md` 说的「先填 api key 再跑 run.sh」，对应代码里哪两处动作？

答案：填 key 对应 [inference/generate.py:L17-L21](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L17-L21) 的 `AsyncOpenAI(api_key="xxx", ..., base_url="yyy")`；跑 `run.sh` 对应 [inference/run.sh:L9-L20](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh#L9-L20) 的 `python main.py ...` 命令。

**练习 2**：`main.py` 第 398 行为什么是 `range(args.start_round, args.max_rounds + 2)` 而不是 `+ 1`？

答案：循环最后一轮 \( R = \text{max\_rounds} + 1 \ 不执行，只把「第 max_rounds+1 轮的精炼输入文件」准备出来，然后在 [inference/main.py:L445-L446](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L445-L446) `break`。若只写 `+ 1`，这份额外输入永远不会被生成。

### 4.2 run.sh：官方竞赛配置长什么样

#### 4.2.1 概念说明

`run.sh` 是官方给出的「复现竞赛成绩」的参数模板。它本身没有逻辑，只有赋值和一条命令，但它的**取值**就是论文成绩背后的运行配置，值得逐行读懂。它也是你日后写自己启动脚本的抄写底版。

#### 4.2.2 核心流程

```
set -f                      # 关闭 bash 路径名展开（globbing）
input_path=三份竞赛题,逗号分隔
output_dirname=xxx          # 占位符，用户自己改
proof_pool_dirname=${output_dirname}/proof_pool
python main.py --input_paths ... （12 个参数）
set +f                      # 恢复 globbing
```

#### 4.2.3 源码精读

[inference/run.sh:L1-L22](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh#L1-L22) 全文逐段说明：

- **[L1](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh#L1) `set -f` / [L22](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh#L22) `set +f`**：关闭/恢复 bash 的路径名展开。因为 `input_path` 在第 10 行是**不带引号**展开的，若路径中含 `*` 或 `?`，bash 会先做 glob 展开再传给 python，这是防御性写法。
- **[L3](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh#L3)**：逗号分隔三份输入 `../IMO2025.json,../CMO2024.json,../CMO2025.json`。`main.py` 第 404 行会按逗号 split 逐份读取，第 405 行取文件名去扩展名作为 `source_name`（证明池会按它分子目录）。
- **[L5](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh#L5)**：`output_dirname=xxx` 是占位符，所有 `proof_gen_R*/`、`proof_verification_R*/` 子目录都会建在它下面。
- **[L7](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh#L7)**：证明池目录默认嵌在输出目录里。
- **[L9-L20](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh#L9-L20)**：核心命令。参数与 `main.py` 默认值的对照（差异加粗）：

| run.sh 传入值 | main.py 默认值 | 含义（取自 argparse help 或参数用途） |
| --- | --- | --- |
| `--input_paths`（三份文件） | 必填，无默认 | 逗号分隔的题目文件，支持 `.json` / `.jsonl` |
| `--output_dirname xxx` | 必填 | 结果输出目录 |
| `--proof_pool_dirname` | 必填 | 每道题历史证明的存放目录 |
| `--n_best_proofs_to_sample 32` | 32 | 精炼时考虑的最优证明数量上限 |
| `--n_proofs_to_refine 1` | 1 | 每次精炼引用几条证明 |
| `--n_agg_trials 32` | 32 | 每题最多生成多少种不同的证明组合 |
| `--n_parallel_proof_gen 128` | 128 | 第一轮每题并行生成多少条证明 |
| **`--n_verification_per_proof 64`** | **4** | 每条证明做几次验证（竞赛配置是默认值的 16 倍） |
| **`--skip_meta_verification`** | **未开启** | 跳过元验证阶段 |
| `--start_round 1` | 1 | 起始轮次（配合文件存在判断可从中途续跑） |
| **`--max_rounds 16`** | **20** | 精炼轮数上限 |

这张表透露的信息：**官方竞赛配置把钱花在验证上**——每条证明验证 64 次、生成「一条证明的精炼」而不是多证明聚合（`n_proofs_to_refine=1`）、且干脆关掉了元验证。这是 u6-l1「测试时算力扩展」的预告。

#### 4.2.4 代码实践

1. **实践目标**：体会 `set -f` 的作用，并制作一个自己的启动脚本底版。
2. **操作步骤**：
   - 在任意目录执行 `bash -c 'set -f; echo ../IMO*.json'`，再执行 `bash -c 'echo ../IMO*.json'`（在 `inference/` 目录下）；
   - 复制 `run.sh` 为 `my_run.sh`（放到你自己的工作区，不要改仓库文件），把 `input_path` 改成单文件、`--max_rounds` 改成 1、`--n_parallel_proof_gen` 改成 2、`--n_verification_per_proof` 改成 2、删掉 `--skip_meta_verification`。
3. **需要观察的现象**：第一条 echo 原样打印 `../IMO*.json`（未展开），第二条打印 `../IMO2025.json`（被 glob 展开）。
4. **预期结果**：理解 `set -f` 保护的是「不带引号的变量展开」这一场景；得到一个低成本的调试版启动脚本。
5. `my_run.sh` 的实际运行效果：待本地验证（依赖 4.5 节的修补）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `--n_verification_per_proof 64` 漏掉，行为会变成什么？

答案：`main.py` 的 argparse 默认值生效（[inference/main.py:L41](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L41) 默认 4），每条证明只验证 4 次，验证评分的统计噪声显著变大，聚合阶段可用的评价样本也变少。

**练习 2**：`run.sh` 里为什么 `proof_pool_dirname` 要引用 `output_dirname` 变量而不是写死？

答案：[L7](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh#L7) 用 `${output_dirname}/proof_pool` 保证证明池跟随输出目录一起更换——换一次实验名（`output_dirname`），全套产物包括证明池都互不污染；同时证明池也是断点续跑的状态载体，必须与输出目录同生命周期管理。

**练习 3**：`../IMO2025.json` 这个相对路径是相对谁的？

答案：相对执行 `run.sh` 时的当前工作目录，即 `inference/`。因为 `run.sh` 直接运行 `python main.py`，而 `main.py` 用 `open(input_path)` 读文件、用 `os.system("python generate.py ...")` 起子进程，全都基于同一工作目录。所以官方用法是 `cd inference && bash run.sh`。

### 4.3 main.py 的 argparse 参数全景

#### 4.3.1 概念说明

`main.py` 注册了 27 个命令行参数，初看眼花，其实按「流水线阶段」分成五组就清晰了：路径组、证明生成组、证明验证组、元验证组、轮次控制组。这一节建立一张参数地图，后续 u4、u5 讲函数逻辑时可以直接回来查。

#### 4.3.2 核心流程

参数的流动方向只有两条：

1. **留在 main.py 内部**：模板名（`--proof_gen_template` 等）、聚合参数（`--n_best_proofs_to_sample`、`--n_proofs_to_refine`、`--n_agg_trials`）、轮次参数——它们影响的是「怎么准备 input.jsonl」。
2. **透传给 generate.py**：`--batch_size`、各阶段的 `--num_processes`、`--temperature`、`--max_tokens`，以及由内部变量算出的 `--n`。对照 [inference/generate.py:L84-L92](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L84-L92) 的参数表，`generate.py` 只认这 8 个参数（`input_data_path`、`output_data_path`、`num_processes`、`batch_size`、`temperature`、`top_p`、`max_tokens`、`n`）。

另有一个**派生参数**：第 64 行 `args.proof_gen_with_self_eval = args.proof_gen_template in ['proof_generation']`——只要生成模板用的是 `proof_generation`，就认定输出带自我评价小节，验证准备阶段会去解析它。

#### 4.3.3 源码精读

argparse 段完整位于 [inference/main.py:L19-L53](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L19-L53)，按组分块：

- **路径组（L20–L24）**：[L20-L22](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L20-L22) 三个必填参数（`input_paths`、`output_dirname`、`proof_pool_dirname`），[L24](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L24) `--batch_size` 默认 160，透传给 generate.py 决定每批多少条。
- **证明生成组（[L26-L34](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L26-L34)）**：进程数（默认 40）、温度（1.0）、最大生成长度（128×1024 token，因为要产出完整证明加自我评价）、生成/精炼模板名，以及四个聚合参数（`n_best_proofs_to_sample=32`、`n_proofs_to_refine=1`、`n_agg_trials=32`、`n_parallel_proof_gen=128`）。argparse 的 `help` 文本只在 `n_best_proofs_to_sample`、`n_proofs_to_refine`、`n_agg_trials` 三处给出，本讲 4.2 的表格即综合 help 与用途整理。
- **证明验证组（[L37-L41](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L37-L41)）**：默认 320 进程、温度 1.0、64×1024 上限、`--n_verification_per_proof` 默认 4。
- **元验证组（[L44-L49](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L44-L49)）**：`--skip_meta_verification` 是 `action='store_true'` 开关（不传即 False，即默认**执行**元验证）；`--n_meta_verification_per_rating` 默认每条评价复核 1 次。
- **轮次控制（[L52-L53](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L52-L53)）**：`start_round` / `max_rounds`。

解析与模块级取值在 [inference/main.py:L55-L64](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L55-L64)：第 55 行 `parse_known_args()`（宽容解析，未知参数被忽略）；第 61–62 行取出两个 URL 变量——**这两行是 4.5 节毛刺的第一现场**；第 64 行派生 `proof_gen_with_self_eval`。

#### 4.3.4 代码实践

1. **实践目标**：不读函数体，仅凭 `--help` 输出重建参数分组表；验证「--help 能运行、真跑会崩」。
2. **操作步骤**：
   - `cd inference && python main.py --help`；
   - `python main.py --input_paths x --output_dirname y --proof_pool_dirname z`。
3. **需要观察的现象**：第一条命令打印全部 27 个参数的帮助并正常退出；第二条命令抛出 `AttributeError: 'Namespace' object has no attribute 'proof_gen_url'`。
4. **预期结果**：`--help` 在解析阶段就退出，永远到不了第 61 行；带必填参数的正常启动则一定崩在第 61 行（原因见 4.5 节）。
5. 第二条命令的报错信息原文：待本地验证（报错的属性名固定为 `proof_gen_url`，因为它是第 61 行第一个被访问的未注册属性）。

#### 4.3.5 小练习与答案

**练习 1**：`main.py` 的 27 个参数里，哪些会出现在 `generate.py` 的命令行里？

答案：只有 6 类会透传：`batch_size`、各阶段 `*_num_processes`、各阶段 `*_temp`、各阶段 `*_max_len`（以 `--max_tokens` 身份），以及由 `n_parallel_proof_gen`（第 1 轮）或 `n_parallel_proof_gen // n_agg_trials`（第 2 轮起，见 [L448](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L448)）换算出的 `--n`，验证阶段的 `n_verification_per_proof`，元验证阶段的 `n_meta_verification_per_rating`。模板名、聚合参数、轮次参数都只在 `main.py` 内部使用。

**练习 2**：为什么 `--skip_meta_verification` 不需要传值，而别的参数都要？

答案：它注册为 `action='store_true'`（[L44](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L44)），是布尔开关：命令行里出现即为 True，不出现为 False。

**练习 3**：第 2 轮起每题生成多少条证明？如果 `n_parallel_proof_gen=2`、`n_agg_trials=32` 会怎样？

答案：\( n_{\text{sample}} = \lfloor n_{\text{parallel\_proof\_gen}} / n_{\text{agg\_trials}} \rfloor \)（[L448](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L448)）。`2 // 32 = 0`，`generate.py` 的复制循环 `for i in range(n)`（[inference/generate.py:L141](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L141)）一次都不执行，第 2 轮会「安静地」产出 0 条记录。做小规模实验时要保证 `n_parallel_proof_gen` 是 `n_agg_trials` 的整数倍，或同步调小 `n_agg_trials`。

### 4.4 generate.py 的 APIModel.__init__：API 配置的唯一填写点

#### 4.4.1 概念说明

无论流水线有多少参数，**真正决定请求发到哪里、用什么身份鉴权的，只有一处**：`generate.py` 里 `APIModel` 类的构造函数。它硬编码了两个占位符 `api_key="xxx"` 和 `base_url="yyy"`。这也解释了 README 为什么说「先去 generate.py 填 key」。

#### 4.4.2 核心流程

`main.py` 拼的证明生成命令里虽然带了 `--api_url {proof_gen_url}`（[L453](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L453)），但这条参数链是**断的**：

```
main.py: --api_url {proof_gen_url}
   └─▶ generate.py 的 argparse 根本没有注册 --api_url
          └─▶ parse_known_args() 把它静默忽略
                 └─▶ 请求地址仍由 APIModel.__init__ 里的 base_url 决定
```

也就是说 `--api_url` 是个装饰品；验证和元验证两条命令（L480–490、L511–521）甚至根本不传它。**想让流水线跑通，改 `__init__` 里的两个占位符是唯一入口。**

#### 4.4.3 源码精读

[inference/generate.py:L15-L21](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L15-L21)：`APIModel.__init__` 构造一个 `AsyncOpenAI` 客户端——`api_key="xxx"` 是鉴权占位符，`base_url="yyy"` 是服务地址占位符，`timeout=300000` 是 300000 秒的超时（对生成 128K token 长证明的场景，宁大勿小）。

对照 [inference/generate.py:L84-L93](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L84-L93)：`generate.py` 自己的 argparse 只注册了 8 个参数，没有 `--api_url`；第 93 行同样用 `parse_known_args()`，因此任何多余的命令行参数都会被吞掉而不报错。这是「装饰性参数链」能一直藏着的直接原因。

顺带认识 `APIModel` 的职责边界（细节留给 u2）：[L23-L35](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L23-L35) 的 `generate_one` 把服务端分开返回的 `reasoning_content` 与 `content` 拼回 `<think>\n...\n</think>\n正文` 的单字符串——这解释了为什么 `main.py` 各处都在用 `'</think>' in output` 做切分判断。

#### 4.4.4 代码实践

1. **实践目标**：确认 `--api_url` 参数链断裂，并找到自己环境需要填的两个值。
2. **操作步骤**：
   - 执行 `grep -n "api_url\|api_key\|base_url" inference/generate.py`；
   - 执行 `grep -n "api_url" inference/main.py`；
   - 如果你手头有 OpenAI 兼容服务（例如 `vllm serve` 起的服务），记下其地址（形如 `http://127.0.0.1:8000/v1`）与 key。
3. **需要观察的现象**：`generate.py` 中 `api_key`/`base_url` 只出现在 L18、L20 两行，全文件没有 `api_url` 字样；`main.py` 中 `api_url` 只出现在 L453（拼进命令字符串）。
4. **预期结果**：证实 `--api_url` 在接收端无人认领。填值时改 L18 的 `"xxx"` 与 L20 的 `"yyy"` 两处即可。
5. 与真实服务的连通性：待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：既然 `--api_url` 被忽略，为什么代码还要传它？

答案：**待确认**。合理推测：官方内部版本的 `generate.py` 曾注册过 `--api_url` 并按阶段使用不同服务（`proof_rate_url` 这个从未被使用的变量名也暗示存在「评分服务」的旧设计），开源发布时删减了注册代码但留下了调用侧的拼接。这也是 4.5 节三个未注册属性同一类问题的缩影。

**练习 2**：把验证阶段的温度从 1.0 改成 0，应该改哪里？

答案：不能改 `APIModel`——温度不在硬编码里。运行时给 `main.py` 传 `--proof_verification_temp 0.0`，它会经 [L486](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L486) 透传为 `generate.py` 的 `--temperature`，最终进入 [inference/generate.py:L116-L121](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L116-L121) 的 `sampling_params`。

### 4.5 发布代码的毛刺：三个未注册的 args 属性（待确认）

#### 4.5.1 概念说明

这是本讲最重要的「实战知识」：按 README 的步骤直接运行，`main.py` **必然崩溃**。原因不是环境问题，而是发布代码里有三个属性「只被使用、从未被注册」。初学者在这里会怀疑自己环境装错了，实际只需补三行代码。标记为**待确认**，因为我们只能从代码推断意图，无法确认官方删减的原始原因。

#### 4.5.2 核心流程

崩溃与修复的位置对照：

| 属性 | 使用位置 | 访问时机 | 是否注册 |
| --- | --- | --- | --- |
| `args.proof_gen_url` | [main.py:L61](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L61) | 模块加载时立即执行 | 否 |
| `args.proof_rate_url` | [main.py:L62](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L62) | 模块加载时立即执行 | 否（且赋值后全文件未再使用） |
| `args.infer_script` | [main.py:L450](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L450) | 进入轮次循环、拼第一条命令时 | 否 |

时序：`python main.py --input_paths ... --output_dirname ... --proof_pool_dirname ...` → argparse 解析成功（三个必填都有了）→ 模块级代码执行到第 61 行 → `AttributeError: 'Namespace' object has no attribute 'proof_gen_url'`，**在任何文件被创建之前**就退出。即便侥幸过了这两行（比如手动注释），第 450 行拼 `proof_gen_cmd` 的 f-string 仍会再次崩在 `args.infer_script`。

#### 4.5.3 源码精读

第一现场 [inference/main.py:L61-L62](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L61-L62)：从 `args` 取 `proof_gen_url` 与 `proof_rate_url`，但上文 L19–L53 的 argparse 注册清单里没有这两项；由于 L55 用的是 `parse_known_args`，就算你在命令行里传 `--proof_gen_url http://...`，它也只会被塞进「未知参数」丢弃桶 `_`，`args` 命名空间上仍不存在这个属性。

第二现场 [inference/main.py:L449-L462](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L449-L462)：f-string 里 `{args.infer_script}.py` 决定生成引擎脚本名——注册并设为 `generate` 后，命令就是 `python generate.py ...`，与验证、元验证阶段硬编码的 `python generate.py` 保持一致。

修复方法（示例代码，加在 [L53](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L53) 之后、L55 解析之前）：

```python
# 示例代码：补注册 main.py 引用但未注册的三个参数
parser.add_argument("--proof_gen_url", type=str, default="http://127.0.0.1:8000/v1")
parser.add_argument("--proof_rate_url", type=str, default="http://127.0.0.1:8000/v1")
parser.add_argument("--infer_script", type=str, default="generate")
```

补上后：`--proof_gen_url` 会流进生成命令的 `--api_url`（仍被 `generate.py` 忽略，见 4.4 节）；`--proof_rate_url` 被赋值但无人使用，保留仅为不崩溃；`--infer_script` 默认 `generate` 指向本目录的生成引擎。

#### 4.5.4 代码实践

1. **实践目标**：在不动仓库源码的前提下（复制副本上操作），让 `main.py` 通过参数解析并进入主循环。
2. **操作步骤**：
   - `cp -r inference inference_mini && cd inference_mini`；
   - 用上面的示例代码给 `inference_mini/main.py` 补三个 `add_argument`；
   - `python main.py --input_paths x --output_dirname mini_out --proof_pool_dirname mini_out/proof_pool --infer_script generate`（故意用一个不存在的输入，观察报错点的前移）。
3. **需要观察的现象**：不再出现 `AttributeError`；报错变成 `FileNotFoundError`（因为输入文件 `x` 不存在，第 407 行 `json.load(open(input_path))` 打不开）。
4. **预期结果**：报错从「参数未注册」推进到「输入文件不存在」，说明三行修复生效、代码已进入真正的数据加载逻辑。
5. 具体报错文本：待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `parse_known_args` 不能救场？传 `--proof_gen_url xxx` 不就行了？

答案：不行。`parse_known_args` 的「宽容」只作用于**命令行多余 token**：未注册的 `--proof_gen_url xxx` 会被归入未知参数列表丢弃，`args` 命名空间上依然没有 `proof_gen_url` 属性，第 61 行照崩。宽容方向是「命令行 → 参数对象」，不是「参数对象 → 自动补全属性」。

**练习 2**：如果不修 `--infer_script`，只注释掉第 61–62 行，程序会在哪里崩？

答案：会在第 450 行（拼 `proof_gen_cmd` 的 f-string 求值时），此时 `proof_gen_R1/input.jsonl` **已经写出**（第 425–428 行在前）。这个对比说明：L61 崩在一切文件产生之前，L450 崩在第一份输入文件之后——通过「输出目录里有没有文件」就能反推崩点位置。

**练习 3**：`proof_rate_url` 赋值后从未使用，这说明什么？

答案：全文件检索可见它只出现在 L62。结合 `--api_url` 被忽略（4.4 节），可以推断开源版本删除了「按阶段调用不同服务」的配置层。对自己项目的启示：读开源代码时，「赋值但未使用」的变量往往指向被删减的功能，是理解代码演化史的线索。具体删减原因：待确认。

## 5. 综合实践：跑通单题第一轮（inference_mini 演练）

把本讲全部知识串起来：复制副本 → 备好单题输入 → 填 API → 补参数 → 低成本跑一轮 → 用 2.4 节的乘法公式核对输出。

### 5.1 准备

```bash
# 步骤 1：复制副本（所有改动只发生在副本上，不碰仓库源码）
cp -r inference inference_mini
```

```python
# 步骤 2（示例代码）：从 inputs/IMO2025.json 抽第 1 题做单题输入，写到仓库根目录
import json
data = json.load(open("inputs/IMO2025.json"))
json.dump(data[:1], open("mytest.json", "w"), ensure_ascii=False, indent=2)
```

五个字段（`id`、`question`、`answer`、`contest`、`problem_idx`，见 u1-l2）原样保留即可；流水线真正必需的是 `question`，`problem_idx` 会被用作证明池文件名。

### 5.2 修改副本（三处）

1. **填 API**：改 `inference_mini/generate.py` 第 18、20 行的 `api_key="xxx"` 与 `base_url="yyy"` 为你的真实服务信息（见 4.4 节）。
2. **补参数**：按 4.5.3 节的示例代码，给 `inference_mini/main.py` 补 `--proof_gen_url`、`--proof_rate_url`、`--infer_script`（默认 `generate`）三行注册。
3. **写启动命令**（等价于一个迷你版 `run.sh`）：

```bash
# 示例代码：在 inference_mini/ 目录内执行
cd inference_mini
python main.py \
    --input_paths ../mytest.json \
    --output_dirname mini_out \
    --proof_pool_dirname mini_out/proof_pool \
    --max_rounds 1 \
    --n_parallel_proof_gen 2 \
    --n_verification_per_proof 2 \
    --infer_script generate
```

### 5.3 观察什么

按文件产生顺序逐个检查（无 API 也能完成第一项；其余待本地验证）：

| 文件 | 产生者 | 预期内容 |
| --- | --- | --- |
| `mini_out/proof_gen_R1/input.jsonl` | main.py 第 425–428 行 | **1 条**：原始字段 + `source_name: "mytest"` + `messages`（`proof_generation` 模板渲染后的完整提示词） |
| `mini_out/proof_gen_R1/output.jsonl` | generate.py | \( 1 \times 2 = 2 \) 条，各含 `output`（`<think>...` 开头）与 `finish_reason` |
| `mini_out/proof_gen_R1/output.jsonl.meta` | generate.py | 断点续跑元数据（`n=2`、`batch_size=160`、`complete_batches`） |
| `mini_out/proof_verification_R1/input.jsonl` | prepare_proof_verification | ≤ 2 条：只有 `finish_reason` 为 `stop`、能切出 Self Evaluation 的证明 |
| `mini_out/proof_verification_R1/output.jsonl` | generate.py | 过滤后条数 × 2 |
| `mini_out/meta_verification_R1/...` | （未传 skip 开关） | 对低分评价的复核输入输出（可能为空文件） |
| `mini_out/proof_pool/mytest/{problem_idx}.jsonl` | 第 2 次迭代（R=2）写入 | 本轮证明入库记录 |
| `mini_out/proof_gen_R2/input.jsonl` | prepare_proof_refinement | 精炼提示，写出后立即 `break`（`max_rounds=1`，见 4.1 练习 2） |

两个必做的核对动作：

- **数行数**：`wc -l` 各 jsonl，与上表对照；对不上先查 `finish_reason` 分布。
- **防「安静失败」**：`main.py` 不检查 `os.system` 返回值。若 `generate.py` 在创建 `output.jsonl` 之前就崩溃（例如缺 `openai` 包），下一阶段 [utils.py:L8-L17](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/utils.py#L8-L17) 的 `read_data` 会抛 `FileNotFoundError`；若文件已建但为空，流水线会带着 0 条记录走完全程。**每阶段结束后必须确认条数非零**，否则空跑等于白烧时间。

### 5.4 预期结果

- 有可用 API：一天内可看到全部 8 类文件，且第一轮各文件条数满足上面的乘法关系。完整运行结果：待本地验证。
- 无 API：跑到 `proof_gen_R1/input.jsonl` 生成后，`generate.py` 报连接错误退出，`main.py` 继续空转产出空文件——这本身就是对 4.1 节「os.system 不检查返回值」最直观的一课。

## 6. 本讲小结

- 启动链条是四级：`README`（说明）→ `run.sh`（拼参数）→ `main.py`（编排轮次与数据准备）→ `generate.py`（唯一真正发 HTTP 请求的组件），`main.py` 通过三处 `os.system` 外包生成任务。
- `run.sh` 的竞赛配置相对默认值的三处差异：验证强度 ×16（64 vs 4）、开启 `--skip_meta_verification`、`max_rounds` 16——验证算力是官方配置的投入重点。
- API 配置的唯一真实入口是 [inference/generate.py:L17-L21](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L17-L21) 的 `api_key` / `base_url`；命令行的 `--api_url` 因接收端未注册而被 `parse_known_args` 静默忽略。
- 发布代码毛刺（待确认）：`proof_gen_url`（L61）、`proof_rate_url`（L62）、`infer_script`（L450）三个属性未注册，直接运行必崩 `AttributeError`，修复只需补三行 `add_argument`。
- 断点续跑的判断粒度是「文件是否存在」（`main.py` 各阶段的 `os.path.exists`）加「批次是否完成」（`generate.py` 的 `.meta` pickle），且 `os.system` 返回值不被检查，需人工核对各阶段条数。
- 通用计数公式：阶段输出条数 = 输入条数 × 该阶段 `--n`；第二轮起生成条数变为 \( \lfloor n_{\text{parallel}} / n_{\text{agg\_trials}} \rfloor \)，小参数组合可能算出 0。

## 7. 下一步学习建议

本讲只解决了「从哪里进、参数怎么流」。接下来两条线任选：

1. **先下到底层（推荐）**：u2-l1 精读 `generate.py` 的 `APIModel`——`asyncio.gather` 如何并发一批请求、`<think>` 拼接的具体规则、多进程队列与 `.meta` 断点续跑的实现。读完你会理解本讲 5.3 节表格里每个文件的内部结构。
2. **先横看模板**：u3-l1 逐段精读 `math_templates.py` 的四个提示词模板，弄清本讲反复出现的 `proof_generation` 模板渲染出的 `messages` 到底长什么样。

之后 u4-l1 会正式进入 `main.py` 的轮次循环，把本讲的参数地图与三个阶段的输入输出文件一一对应起来。
