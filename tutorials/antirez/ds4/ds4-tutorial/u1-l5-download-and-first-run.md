# 下载模型与首次运行

## 1. 本讲目标

本讲是「让 ds4 真正跑起来」的那一步。学完后你应当能够：

- 说清楚 `./download_model.sh <target>` 会下载什么、下到哪里、断了怎么续、下完做了什么。
- 用 `./ds4 -p "..."` 跑一次 one-shot（一次性）推理，并读懂启动日志里的速度数字。
- 用 `./ds4`（不带 `-p`）进入交互式 REPL，掌握 `/help`、`/think`、`/ctx`、`/read` 等斜杠命令。

本讲只讲「下载 + 两种运行姿态」，不展开推理内核、采样算法或后端实现——那些在后续单元。本讲的源码主角是一个 shell 脚本、一份帮助文本和 CLI 主流程，门槛很低，是后续所有讲义的「起跑线」。

## 2. 前置知识

在进入本讲前，请确认你已经具备以下认知（来自前置讲义）：

- **你已经能编译出二进制**（u1-l4）：ds4 在 Linux 上裸 `make` 只打印帮助，必须显式选择后端目标，例如 `make cuda-generic` 或诊断用的 `make cpu`；macOS 上 `make` 默认走 Metal。
- **你知道该下哪个 GGUF**（u1-l2）：96/128GB 内存机器选 `q2-imatrix`，≥256GB 选 `q4-imatrix`，512GB 才上 PRO。本讲的下载脚本正是按这套内存档位组织 target 的。
- **两个基本术语**：
  - **prefill（预填）**：把提示词一次性喂给模型、计算并填充 KV 缓存的过程。提示越长，prefill 越久，第一个 token 出现得越晚。
  - **decode（解码/生成）**：prefill 之后，逐个吐出新 token 的自回归过程。

一个贯穿本讲的直觉：**「首 token 延迟」主要由 prefill 决定，而「生成速度」由 decode 决定**。ds4 在每次 one-shot 运行结束时会同时报告这两项，本讲会教你怎么读这行日志。

> 说明：本讲编写环境是一台没有 GPU、也没有下载模型的 Linux CI 机器，因此涉及「实际运行推理」的步骤会标注**待本地验证**，并给出等价的「源码阅读型实践」作为替代。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `download_model.sh` | 从 HuggingFace 下载 GGUF 的 shell 脚本 | target 分发、断点续传、`ds4flash.gguf` 软链 |
| `ds4_help.c` | 五个二进制共用的 `--help` 文本生成器 | `-p`/`-m`/后端 选项的官方描述、交互命令清单 |
| `ds4_cli.c` | `ds4` 命令行前端的主流程 | main 分支：one-shot vs REPL；斜杠命令分发 |
| `README.md` | 项目主文档 | 下载说明、CLI 用法、后端选择 |

其中 `ds4_cli.c` 是 `ds4` 这个二进制的「前端 `.o`」（u1-l3 已建立这个概念），它只负责策略（参数解析、交互循环、统计打印），把真正的推理/缓存机制留给引擎 API。

## 4. 核心概念与源码讲解

### 4.1 模块一：`download_model.sh` 的行为

#### 4.1.1 概念说明

ds4 的模型权重托管在 HuggingFace 仓库 `antirez/deepseek-v4-gguf`。这些 GGUF 文件非常大（2bit 的 Flash 约 81GB，4bit 约 153GB，PRO 单文件约 430GB），所以下载脚本要解决三个现实问题：

1. **选哪个文件**——按内存档位给出有语义的名字（`q2-imatrix`、`q4-imatrix` 等），避免让你去记一长串文件名。
2. **断了怎么办**——几十到几百 GB 的文件，中途断网几乎必然发生，必须能续传。
3. **下完怎么用**——ds4 的默认模型路径是 `./ds4flash.gguf`，脚本要在下载完成后把这个名字指向刚下好的文件。

#### 4.1.2 核心流程

`download_model.sh` 的执行流程可以概括为：

```text
解析第 1 个参数 → MODEL（如 q2-imatrix）
        │
        ▼
case 分发：把语义名映射成真实文件名 MODEL_FILE
        │
        ▼
解析 --token（可选），否则读 HF_TOKEN，再否则读本地 HF token 缓存
        │
        ▼
download_one(file):
   ├─ 若是 PRO 大文件 → download_one_hf()：用官方 hf CLI 下载
   └─ 否则            → curl -fL -C -（断点续传）下载到 file.part，成功后 mv 成正式文件
        │
        ▼
若 LINK_MODEL=1：在项目根创建符号链接 ds4flash.gguf → 下载目录/MODEL_FILE
```

关键点：**小文件用 `curl`，PRO 大文件用 HuggingFace 官方 CLI**。原因是 PRO 文件太大，脚本作者认为 curl 路径不够稳健，强制走官方 `hf download`。

#### 4.1.3 源码精读

**语义名 → 真实文件名的映射**，集中在脚本顶部的变量定义和一处 `case`：

[download_model.sh:5-11](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/download_model.sh#L5-L11) 定义了各个 target 对应的真实 GGUF 文件名（如 `Q2_IMATRIX_FILE`）。

[download_model.sh:105-127](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/download_model.sh#L105-L127) 是核心 `case` 分发：把 `q2-imatrix` 这类语义名翻成 `MODEL_FILE`，并设置 `LINK_MODEL` 标志。注意 PRO 的拆分 target（`pro-q4-layers00-30` 等）和 `mtp` 把 `LINK_MODEL=0`，即**不**更新 `ds4flash.gguf`——因为它们不是「主模型」，要用 `--layers`/`--mtp` 显式指定。

**断点续传**是 `download_one()` 的核心。普通文件走 curl 路径：

[download_model.sh:234-250](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/download_model.sh#L234-L250) 先检查是否已下完（`-s "$out"` 表示文件非空就直接跳过），否则用 `curl -fL --progress-meter -C -` 下载到 `$part`（`.part` 后缀），成功后 `mv "$part" "$out"`。这里的 `-C -` 就是 curl 的「自动断点续传」开关——这也是 README 里「resumes partial downloads with `curl -C -`」的出处（[README.md:121-123](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L121-L123)）。所以**中断后重跑同一条命令即可续传**，无需任何额外参数。

**PRO 文件强制走官方 CLI**：

[download_model.sh:151-160](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/download_model.sh#L151-L160) 用 `needs_hf_download()` 判定三个 PRO 文件。

[download_model.sh:170-212](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/download_model.sh#L170-L212) 的 `download_one_hf()` 检查是否安装了 `hf` 命令，没有就直接报错并给出安装命令 `python3 -m pip install -U huggingface_hub hf_xet`；并且它会**拒绝**接管 curl 留下的 `.part` 文件（[download_model.sh:182-187](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/download_model.sh#L182-L187)），因为官方下载器无法续传 curl 的分片格式——这是一个容易踩的坑。

**下载完成后的软链**：

[download_model.sh:269-273](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/download_model.sh#L269-L273) 在 `LINK_MODEL=1` 时执行 `ln -sfn "$OUT_DIR/$MODEL_FILE" ds4flash.gguf`，把项目根目录下的 `ds4flash.gguf` 指向刚下好的模型。这一步是「下完即可用」的关键——`ds4` 和 `ds4-server` 的默认模型路径都是 `ds4flash.gguf`。

**下载目录与环境变量**：

[download_model.sh:13-18](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/download_model.sh#L13-L18) 解析 `OUT_DIR`：默认 `$ROOT/gguf`（即项目根下的 `gguf/`），可用环境变量 `DS4_GGUF_DIR` 覆盖；如果是相对路径会自动补成绝对路径。

#### 4.1.4 代码实践

**实践目标**：在不真的下载 81GB 的前提下，验证你对脚本控制流的理解。

**操作步骤（源码阅读型）**：

1. 阅读 [download_model.sh:95-127](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/download_model.sh#L95-L127)，回答：不带任何参数运行 `./download_model.sh` 会发生什么？（提示：看 `if [ $# -eq 0 ]` 分支。）
2. 阅读 [download_model.sh:214-250](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/download_model.sh#L214-L250)，追踪 `download_one q2-imatrix` 的调用链：先判 `needs_hf_download`（返回否），再检查 `$out` 是否已存在，再 curl 到 `$part`，再 mv。
3. （可选，真实运行）在一台 96/128GB 的 Mac 上执行：

   ```sh
   ./download_model.sh q2-imatrix
   ```

**需要观察的现象**：

- 脚本打印 `Downloading <文件名>` 和 `from https://huggingface.co/antirez/deepseek-v4-gguf`。
- 文件先以 `.part` 后缀增长，完成后被 `mv` 成正式文件名。
- 结束时打印 `Linked ./ds4flash.gguf -> gguf/<文件名>` 和 `Done.`。

**预期结果**：项目根出现 `ds4flash.gguf` 软链，`gguf/` 下出现完整 GGUF。若中途中断，重跑同一条命令会从 `.part` 续传，而不是从头开始。

**待本地验证**：实际下载耗时、是否触发限速需要 token 等现象，需在有模型访问条件的机器上验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `pro-q4-split` target 不会更新 `ds4flash.gguf`？

**参考答案**：因为它是分布式推理用的「两个半模型」，由 `--layers` 在两台机器上分别加载，不是一个能被默认路径 `ds4flash.gguf` 直接指向的单文件主模型。脚本在 [download_model.sh:112-115](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/download_model.sh#L112-L115) 把它的 `LINK_MODEL` 设为 0。

**练习 2**：如果你先跑了 `./download_model.sh pro-q2-imatrix`（用 `hf` 下了一半，留下 `.part`？），再想改用 curl 路径，脚本会怎么做？

**参考答案**：这是一个陷阱。PRO 文件强制走 `download_one_hf`，它根本不用 curl 的 `.part`；反过来若已有 curl 的 `.part`，`download_one_hf` 会直接报错退出（[download_model.sh:182-187](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/download_model.sh#L182-L187)），因为官方下载器无法续传 curl 分片。你需要先删掉 `.part` 再重试。

---

### 4.2 模块二：one-shot 推理（`./ds4 -p`）

#### 4.2.1 概念说明

ds4 的 CLI 有两种姿态：

- **one-shot（一次性）**：用 `-p "提示词"` 给一个提示，模型生成回答后进程退出。适合脚本化、批处理、快速冒烟测试。
- **interactive（交互式）**：不带 `-p`，进入多轮对话 REPL。这是模块三的内容。

one-shot 的核心问题是：**从命令行参数到「屏幕上逐字吐出回答」之间，CLI 做了哪几步？** 答案藏在 `ds4_cli.c` 的 `main()` 分支和一个叫 `run_generation` 的函数里。

#### 4.2.2 核心流程

one-shot 的主链路：

```text
main() 解析参数 → cli_config
   │
   ├─ cfg.gen.prompt == NULL ?  → run_repl()      （交互模式，模块三）
   │
   └─ 否则（有 -p）             → run_generation()
                                     │
                                     ├─ build_prompt()：把 -p 文本渲染成 DeepSeek 聊天 token 序列
                                     │
                                     ├─ 若 temperature>0 或分布式或 MTP draft>1 → run_sampled_generation()
                                     │
                                     └─ 否则（默认贪婪）→ ds4_engine_generate_argmax()
                                                            ├─ 内部 prefill（首 token 延迟来源）
                                                            └─ 内部 decode（逐 token 生成）
                                     │
                                     ▼
                  结束时打印 "ds4: prefill: X t/s, generation: Y t/s"
```

关于「首 token 延迟」的数学关系：设提示长度为 \(N\) 个 token、prefill 吞吐为 \(R_p\) tokens/s，则从回车到第一个 token 出现的时间近似为

\[
T_{\text{first}} \approx \frac{N}{R_p}
\]

因为 prefill 必须把整个提示处理完、KV 缓存填满，才能开始产出第一个新 token。而生成速度 \(R_g\)（decode 的 t/s）与 prefill 是两个独立指标——一个长提示可能 prefill 很久但生成很快，反之亦然。

#### 4.2.3 源码精读

**main 的关键分支**——决定走 REPL 还是 one-shot：

[ds4_cli.c:1698-1702](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1698-L1702) 显示：`cfg.gen.prompt == NULL` 时调用 `run_repl`，否则调用 `run_generation`。这正是「有没有 `-p`」的分界。注意上面还有 `--inspect`、imatrix、perplexity 等其它分支，本讲只关注 prompt 分支。

**one-shot 的入口 `run_generation`**：

[ds4_cli.c:873-882](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L873-L882) 先用 `build_prompt` 把提示渲染成 token 序列。注意函数开头一连串 `if` 是各种**诊断模式**的短路（`metal_graph_test`、`dump_logits`、`dump_tokens` 等）——这些是后续讲义（u11-l3 测试向量）的内容，正常推理不会进入。

[ds4_cli.c:922-947](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L922-L947) 是默认（贪婪）推理路径：当 `temperature>0`、分布式 coordinator、或 MTP draft>1 都不成立时，直接调用引擎 API `ds4_engine_generate_argmax`，把 `print_generated_token` 回调作为「每生成一个 token 就往 stdout 写一个片段」的输出器，`cli_prefill_progress_cb` 作为 prefill 进度回调。

**速度统计是怎么算出来的**（采样路径 `run_sampled_generation`，逻辑与贪婪路径同理）：

[ds4_cli.c:448-465](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L448-L465) 用 `cli_now_sec()` 在 `ds4_session_sync`（即 prefill）前后各打一个时间戳 `t_prefill0`/`t_prefill1`，差值就是 prefill 耗时。

[ds4_cli.c:533-539](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L533-L539) 打印最终的速度行：

```c
ds4_log(stderr, DS4_LOG_TIMING,
        "ds4: prefill: %.2f t/s, generation: %.2f t/s\n",
        prefill_s > 0.0 ? (double)prompt->len / prefill_s : 0.0,
        decode_s   > 0.0 ? (double)generated   / decode_s   : 0.0);
```

注意这两个值是**吞吐率**（tokens/s），不是延迟；prefill 的吞吐 = 提示 token 数 / prefill 秒数，生成的吞吐 = 生成 token 数 / decode 秒数。首 token 延迟要用「提示 token 数 ÷ prefill t/s」自己算。

**官方选项描述**（来自帮助系统）：

[ds4_help.c:200-202](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_help.c#L200-L202) 描述了 `-p, --prompt TEXT`（one-shot 提示文本）和 `--prompt-file FILE`（从文件读长提示）。

[ds4_help.c:149](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_help.c#L149) 说明默认模型路径是 `ds4flash.gguf`——这正是下载脚本软链指向的名字，所以下载完就能直接 `./ds4 -p "..."` 而不用加 `-m`。

#### 4.2.4 代码实践

**实践目标**：完成一次 one-shot 推理，并从输出日志里读出 prefill/生成两项速度，换算首 token 延迟。

**操作步骤**：

1. 确认模型已下载且 `ds4flash.gguf` 软链存在（模块一的产物）。
2. 确认已编译二进制（u1-l4）。在 macOS：`make`；在 Linux CUDA：`make cuda-generic`；纯诊断：`make cpu`。
3. 运行：

   ```sh
   ./ds4 -p "Explain Redis streams in one paragraph."
   ```

   （README 的示例，见 [README.md:670-676](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L670-L676)）。

4. 若想明确后端，加 `--metal` / `--cuda` / `--cpu`（见 [README.md:1178-1201](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L1178-L1201)）。
5. 若只是想验证模型能加载、不想真跑推理，可以用诊断开关 `--inspect`，它只加载模型并打印摘要（对应 [ds4_cli.c:1687-1688](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1687-L1688) 的 `ds4_engine_summary` 分支）。

**需要观察的现象**：

- 终端先出现一个 prefill 进度条/计数（提示越长越明显）。
- 随后开始逐字吐出回答。
- 结束后 stderr 打印一行 `ds4: prefill: X.XX t/s, generation: Y.YY t/s`。

**预期结果**：记录这两个数字。若提示约 \(N\) 个 token，则首 token 延迟约为 \(N / X\) 秒。例如 prefill=250 t/s、提示 11700 token，则首 token 延迟约 47 秒——这与 README 速度表里长提示的量级吻合（[README.md:162-175](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L162-L175)）。

**待本地验证**：本编写环境无 GPU 且未下载模型，上述具体数字需在读者机器上实测。

#### 4.2.5 小练习与答案

**练习 1**：为什么 README 速度表里「短提示」的 prefill t/s（如 58.52）远低于「长提示」（如 250.11）？是模型变快了吗？

**参考答案**：不是。短提示的 prefill 总耗时里，固定开销（图构建、首批 kernel 启动、缓存初始化）占比很大，分摊到很少的 token 上就显得吞吐低；长提示把这些固定开销摊薄，吞吐更接近 GPU 的峰值。所以 prefill t/s 强烈依赖提示长度，不能脱离长度比较。

**练习 2**：默认情况下（`temperature` 未设）`run_generation` 走的是 `ds4_engine_generate_argmax` 还是 `run_sampled_generation`？

**参考答案**：走 `ds4_engine_generate_argmax`（贪婪/argmax）。因为采样路径的触发条件是 `temperature > 0` 或分布式 coordinator 或 MTP draft>1（[ds4_cli.c:922-925](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L922-L925)），而默认 temperature 为 0（贪婪），单机非分布式且未开 MTP，所以走 argmax 快速路径。

---

### 4.3 模块三：交互式 REPL 与斜杠命令

#### 4.3.1 概念说明

不带 `-p` 运行 `./ds4`，会进入一个交互式多轮对话界面，提示符是 `ds4>`。它不是简单的「问答完就忘」，而是一个**真正的多轮聊天**：内部维护一份「已渲染的聊天 token 转录本（transcript）」和「一个活的 KV 缓存会话（session）」，所以你第二轮说的话，模型是在第一轮的上下文基础上继续回答——这一点和 ds4-server 的前缀复用是同一套机制（u2-l3 会深入讲）。

REPL 用 `linenoise` 这个轻量行编辑库（Redis 同款）提供上下箭头历史、行编辑，历史持久化到 `~/.ds4_history`。

#### 4.3.2 核心流程

REPL 主循环：

```text
run_repl():
   repl_chat_init()：建 transcript + 建 session（带 ctx_size）
   装 SIGINT 处理器（Ctrl+C 中断生成、回到提示符）
   加载历史 ~/.ds4_history
   print_repl_help() 打印命令清单
   loop:
      line = linenoise("ds4> ")      ← 阻塞等输入
      若是 / 开头 → 斜杠命令分发
         /help       → print_repl_help
         /think...   → 切换思考模式（可能改写 transcript 前缀 → invalidate session）
         /ctx N      → 销毁旧 session、按新 ctx 重建
         /power N    → 调 GPU 占空比
         /read FILE  → 读文件内容当下一轮 user 消息
         /quit       → 退出
      否则 → 当作 user 消息 → run_chat_turn()
               ├─ 把 user 文本追加进 transcript
               ├─ ds4_session_sync()：前缀复用，只增量 prefill 后缀
               └─ 逐 token 生成并打印
```

#### 4.3.3 源码精读

**REPL 主循环与 linenoise**：

[ds4_cli.c:1221-1238](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1221-L1238) 是 `run_repl` 的开头：初始化 `repl_chat`、装 SIGINT 处理器（`cli_sigint_handler`）、设置 linenoise 多行与历史长度（512 条）、从 `~/.ds4_history` 加载历史，然后打印帮助。

[ds4_cli.c:1241-1257](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1241-L1257) 是循环主体：`linenoise("ds4> ")` 读一行，trim 后非空则加入历史并保存。

**斜杠命令分发**：

[ds4_cli.c:1259-1313](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1259-L1313) 是一长串 `if/else` 把命令分发出去。值得注意的细节：

- `/think`、`/think-max`、`/nothink` 切换思考模式后会调用 `repl_chat_apply_max_prefix`——因为 Think Max 要在 transcript 开头插入一段特殊前缀，这会让之后所有 token 位置错位，所以必须 `ds4_session_invalidate` 丢弃当前 KV（[ds4_cli.c:1026-1039](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1026-L1039)）。
- `/ctx N` 直接销毁旧 session 并按新 ctx 重建（[ds4_cli.c:1291-1309](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1291-L1309)），因为 KV 缓存容量和 ctx 绑定。
- `/power N` 调 `ds4_session_set_power`，把 GPU 占空比在运行时改掉（[ds4_cli.c:1276-1290](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1276-L1290)）。

**命令清单文本**：

[ds4_cli.c:961-972](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L961-L972) 是 `print_repl_help` 的全部内容，列出了 `/help`、`/think`、`/think-max`、`/nothink`、`/ctx N`、`/power N`、`/read FILE`、`/quit`/`/exit` 以及 `Ctrl+C`。帮助系统里也有一份对应清单（[ds4_help.c:265-275](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_help.c#L265-L275)）。

**多轮会话状态**：

[ds4_cli.c:988-993](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L988-L993) 定义了 `repl_chat` 结构：一个 `ds4_session *session`（活 KV 缓存）+ 一份 `ds4_tokens transcript`（已渲染聊天转录本）。每一轮对话靠 `run_chat_turn` 把 user 文本追加进 transcript，再让 `ds4_session_sync` 决定是增量 prefill 还是重建（[ds4_cli.c:1078-1090](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1078-L1090)）。

#### 4.3.4 代码实践

**实践目标**：进入 REPL，体验多轮上下文复用，并观察 `/ctx` 切换带来的 session 重建。

**操作步骤**：

1. 确保模型已下载、二进制已编译。
2. 运行（不加 `-p`）：

   ```sh
   ./ds4
   ```

3. 在 `ds4>` 提示符下输入 `/help`，对照 [ds4_cli.c:961-972](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L961-L972) 确认命令清单一致。
4. 输入 `/nothink` 关闭思考模式（默认是开的），让回答更直接。
5. 进行两轮有上下文关联的对话，例如第一轮 `我叫小明`，第二轮 `我叫什么名字？`——观察第二轮能否正确回答（说明上下文被复用）。
6. 输入 `/ctx 8192` 改变上下文窗口，观察日志（应提示 context buffers 重建）。
7. 用 `Ctrl+C` 在生成过程中中断，确认能回到 `ds4>` 而不是退出。
8. `/quit` 退出。

**需要观察的现象**：

- 第二轮提问时，prefill 只处理「新增的少量 token」，而不是把整段历史重算——因为前缀 KV 被复用了（详见 u2-l3）。
- `/ctx N` 后会打印一行类似 `ds4: context buffers ... MiB (ctx=N, ...)` 的内存估算。
- `Ctrl+C` 中断后回到提示符，历史文件 `~/.ds4_history` 里能看到刚才输入的行。

**预期结果**：多轮对话连贯；`/ctx` 与 `/power` 能在运行时生效；历史在下次启动 `./ds4` 时仍可用（上下箭头）。

**待本地验证**：具体内存数字、第二轮 prefill 是否明显变快，需在有模型的机器上观察。

#### 4.3.5 小练习与答案

**练习 1**：为什么切换 `/think-max` 会让当前会话的 KV 缓存失效，而普通 `/think` 不会（在已经没有 max 前缀时）？

**参考答案**：Think Max 需要在 transcript 最开头（BOS 之后、system 之前）插入一段「最大努力前缀」token。一旦插入，其后所有 token 的位置都偏移了，原来的 KV 缓存对不上新的位置，必须 `ds4_session_invalidate` 丢弃重算（[ds4_cli.c:1026-1039](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1026-L1039)）。普通 `/think` 不改前缀结构，所以无需失效。

**练习 2**：`/read FILE` 和直接在 `ds4>` 粘贴大段文本，效果上有区别吗？

**参考答案**：本质都是把一段文本作为下一轮 user 消息提交。`/read FILE` 的价值在于绕开终端粘贴大文件的不便与长度限制，并保证内容按文件原样读入。两者最终都走 `run_chat_turn` → `ds4_session_sync` 的同一条路径。

---

## 5. 综合实践

把本讲三块内容串起来，设计一个端到端的「首次运行」小任务：

**场景**：你要在一台 128GB 的 Mac 上第一次跑 ds4，目标是验证「下载 → one-shot 冒烟 → 多轮对话」整条链路通畅。

**步骤**：

1. **下载**：执行 `./download_model.sh q2-imatrix`。完成后用 `ls -l ds4flash.gguf` 确认它是指向 `gguf/` 下真实文件的软链（对应 [download_model.sh:269-273](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/download_model.sh#L269-L273)）。
2. **编译**：`make`（macOS Metal）。
3. **冒烟（one-shot）**：`./ds4 -p "用一句话解释什么是 KV 缓存。"`。记下 stderr 末尾的 `prefill: X t/s, generation: Y t/s`，并用提示长度估算首 token 延迟。
4. **若失败排查**：若 one-shot 行为可疑，改用 `./ds4 --inspect` 只加载模型打印摘要（[ds4_cli.c:1687-1688](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1687-L1688)），把「能不能加载模型」与「能不能推理」两个问题分离。
5. **多轮（REPL）**：`./ds4` 进入 `ds4>`，先 `/nothink`，再问两轮有依赖关系的问题，确认上下文复用；最后 `/ctx 8192` 观察 session 重建日志，`/quit` 退出。
6. **断点续传演练**（可选）：删除 `ds4flash.gguf` 软链与 `gguf/` 下文件（或在一个小 target 上模拟），重跑下载命令，中途 `Ctrl+C`，再重跑，观察它从 `.part` 续传而非重头下（对应 [download_model.sh:234-250](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/download_model.sh#L234-L250)）。

**完成标志**：你能用一句话说清「`ds4flash.gguf` 是怎么来的、`-p` 走哪条代码路径、REPL 怎么复用上下文」这三件事。

## 6. 本讲小结

- `download_model.sh` 用语义 target（`q2-imatrix` 等）屏蔽真实文件名；普通文件用 `curl -C -` 断点续传，PRO 大文件强制走官方 `hf` CLI；主模型下完后软链到 `ds4flash.gguf`，分布式分片与 mtp 不更新该软链。
- `./ds4 -p "..."` 是 one-shot：`main` 里 `prompt==NULL` 决定走 REPL 还是 `run_generation`；默认贪婪路径直接调 `ds4_engine_generate_argmax`，结束时打印 `prefill / generation t/s`。
- 首 token 延迟 ≈ 提示 token 数 ÷ prefill t/s；prefill 与生成是两个独立指标，比较速度时不能脱离提示长度。
- `./ds4`（无 `-p`）进入多轮 REPL：内部用 `repl_chat`（transcript + 一个活 session）实现上下文复用，斜杠命令 `/think`、`/ctx`、`/power`、`/read` 等可在运行时调整。
- 切换 Think Max 或 `/ctx` 会让当前 KV 失效/重建，因为它们改变了 transcript 结构或缓存容量。
- 排查「模型能不能用」可以先用 `--inspect` 只加载不推理，把问题分层。

## 7. 下一步学习建议

本讲只是把 ds4「跑起来」。接下来建议：

- **u2-l1（ds4.h：引擎边界与生命周期）**：本讲反复出现的 `ds4_engine` / `ds4_session` 到底是什么、有哪些生命周期函数，下一讲正式定义这对公共边界。
- **u2-l2（CLI 主流程）**：本讲只看了 `run_generation` 和 `run_repl` 两个分支，下一讲会完整讲 `cli_config`、参数解析与信号中断机制。
- **u2-l3（Session 同步与前缀复用）**：本讲提到 REPL 第二轮「只增量 prefill 后缀」，其内部原理就是 `ds4_session_sync` 的前缀匹配——这是理解 ds4 速度的关键一讲。
- 若你对「为什么 prefill 这么慢、能不能调」更感兴趣，可以先跳到 u6-l1（分块 prefill 主路径），但建议先读完 u2 建立引擎 API 词汇表。
