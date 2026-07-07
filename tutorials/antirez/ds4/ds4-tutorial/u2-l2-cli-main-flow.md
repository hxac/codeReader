# CLI 主流程

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `ds4` 这个命令行程序从 `main` 入口到「吐出第一个 token」之间到底经历了哪些函数。
- 区分**一次性模式**（`-p "..."`，问完即退）和**交互模式**（不带 `-p`，进入 `ds4>` 多轮对话）两条分支的触发条件与代码路径。
- 理解 `cli_config` / `cli_generation_options` 这两个配置结构体如何把「命令行参数」翻译成「引擎调用」。
- 解释 ds4 的**协作式中断**（cooperative interrupt）模型：为什么 Ctrl+C 不会立刻杀死进程，而是等当前 token 算完才停下。

本讲承接 u2-l1（`ds4.h` 的 engine/session 边界）。u2-l1 讲的是「引擎对外暴露了什么」，本讲讲的是「`ds4` 这个前端二进制如何调用这些引擎接口」。

## 2. 前置知识

- **前端二进制**：在 u1-l3 里我们已经建立了这个概念——`ds4`、`ds4-server`、`ds4-agent` 等都是「自己的前端 `.o` + 公共 `CORE_OBJS`」拼出来的。本讲的主角是 `ds4` 这个前端，它的全部逻辑都在 `ds4_cli.c` 一个文件里。
- **engine 与 session**（u2-l1）：`ds4_engine` 是「已加载模型」（进程级、基本只读），`ds4_session` 是「一条可变推理时间线」（对话级、持有 KV 缓存）。本讲会看到 CLI 如何 open 一个 engine，再按需 create session。
- **prefill 与 decode**（u1-l5）：prefill 是「一次性把提示填进 KV 缓存」，决定首 token 延迟；decode 是「自回归逐个生成」。本讲会看到这两步在代码里分别对应 `ds4_session_sync` 和 `ds4_session_eval`。
- **C 语言的信号处理**：`SIGINT` 是按 Ctrl+C 时内核发给进程的信号；默认处置是「终止进程」。可以用 `sigaction` 注册一个自定义处理函数，把它改成「只设一个标志位」——这就是协作式中断的基础。
- **`sig_atomic_t`**：一种保证「读写不会被信号打断」的整数类型，专门用于信号处理函数和主循环之间共享的标志位。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [ds4_cli.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c) | `ds4` 前端的全部代码：参数解析、一次性生成、交互式 REPL、信号处理。本讲的核心。 |
| [ds4.h](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h) | 引擎公共 API。本讲引用其中的采样/生成函数声明与默认采样常量。 |
| [ds4_help.h](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_help.h) | 帮助文本打印接口。CLI 的 `usage()` 委托给它。 |
| [linenoise.h](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/linenoise.h) | antirez 自己写的轻量行编辑库（同 Redis）。交互模式靠它读入 `ds4>` 那一行输入。 |

## 4. 核心概念与源码讲解

### 4.1 配置结构体：cli_config 与 cli_generation_options

#### 4.1.1 概念说明

命令行程序最典型的两段式结构是：**先把所有命令行参数解析、归并到一个结构体里，再根据这个结构体决定做什么**。ds4 把这个「解析结果容器」拆成了两层：

- `cli_generation_options`：**生成相关的参数**——提示文本、系统提示、采样参数（温度/top_p/min_p）、思考模式、各种诊断开关。
- `cli_config`：**整个程序运行所需的全部配置** = 引擎配置（`ds4_engine_options`，u2-l1 讲过）+ 分布式配置（`ds4_dist_options`）+ 生成配置（`cli_generation_options`）+ 两三个 CLI 私有字段。

为什么要分两层？因为 `ds4_engine_options` 和 `ds4_dist_options` 是引擎层定义的、会被 `ds4-server`/`ds4-agent` 等其它前端复用的公共结构；而 `cli_generation_options` 是 `ds4` 这个前端私有的，只跟「一次性/交互式生成」有关。把它们分开，CLI 的临时字段不会污染公共 API。

#### 4.1.2 核心流程

```
argv[]  ──parse_options()──▶  cli_config
                                  ├─ engine   (ds4_engine_options)
                                  ├─ dist     (ds4_dist_options*)
                                  ├─ gen      (cli_generation_options)
                                  ├─ prompt_owned  (-p 文件读入时持有的内存)
                                  └─ inspect       (--inspect)
```

`parse_options` 内部用一个 `for` 循环逐个 `argv` 处理，每命中一个选项就改写 `cli_config` 的某个字段，遇到不认识的选项就 `exit(2)`。

#### 4.1.3 源码精读

`cli_generation_options` 集中了「这一次生成要怎么跑」的全部旋钮：

[ds4_cli.c:28-52](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L28-L52) —— 定义了 `prompt`（提示文本指针）、`system`（系统提示）、`n_predict`（最多生成多少 token）、`ctx_size`（上下文窗口）、`temperature/top_p/min_p`（采样三件套）、`seed`、`think_mode`（思考模式枚举），以及一堆诊断开关（`dump_tokens`、`head_test`、`first_token_test`、各种 `metal_graph_*_test`）。

外层的 `cli_config` 把三类配置打包：

[ds4_cli.c:54-60](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L54-L60) —— 注意 `dist` 是一个**指针**（堆上分配，因为分布式选项体量较大且可选），而 `engine` 和 `gen` 是**内嵌结构体**（直接嵌在 `cli_config` 里）。`prompt_owned` 记录「当 prompt 是从文件读进来时，谁拥有这块 `malloc` 内存」，以便 `main` 末尾统一 `free`。

`parse_options` 的开头先给整个 `cli_config` 填**默认值**：

[ds4_cli.c:1392-1411](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1392-L1411) —— 默认模型路径是 `ds4flash.gguf`（u1-l5 提到的软链）；默认后端由 `default_backend()` 决定（macOS→Metal、Linux→CUDA、`DS4_NO_GPU`→CPU）；默认采样取自 `ds4.h` 的三个常量；默认思考模式是 `DS4_THINK_HIGH`（所以 README 说「CLI 默认开启思考」）。这里有个**关键默认值**：`temperature = DS4_DEFAULT_TEMPERATURE`。

默认采样常量定义在引擎头里：

[ds4.h:55-57](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L55-L57) —— `DS4_DEFAULT_TEMPERATURE = 1.0f`、`DS4_DEFAULT_TOP_P = 1.0f`、`DS4_DEFAULT_MIN_P = 0.05f`。请记住 `temperature` 默认是 `1.0`，这一点直接决定了 4.2 节里「默认走哪条生成分支」。

参数解析循环对每个选项用 `need_arg` 取下一个 `argv` 作为它的值（缺值则报错退出）：

[ds4_cli.c:1337-1343](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1337-L1343) —— 例如 `-p` 的处理就是把下一个参数赋给 `c.gen.prompt`：

[ds4_cli.c:1442-1447](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1442-L1447) —— 注意它先检查 `c.gen.prompt` 是否已经被设过（防止同时给 `-p` 和 `--prompt-file`）。`--prompt-file` 走另一条路径，把文件内容读进 `c.prompt_owned` 堆内存，再让 `c.gen.prompt` 指向它。

> 小贴士：`parse_options` 还会**优先**调用 `ds4_dist_parse_cli_arg` 抢先识别分布式专属参数（如 `--layers`），命中就 `continue`。这体现了「CLI 是个薄壳，把复杂子系统的参数解析下放给子系统自己」的设计。

#### 4.1.4 代码实践

1. **实践目标**：把 `cli_generation_options` 的字段按用途分类，建立「字段 → 命令行选项」的映射。
2. **操作步骤**：
   - 打开 [ds4_cli.c:28-52](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L28-L52)，把 18 个字段分成四类：「提示来源」「采样参数」「思考模式」「诊断/测试开关」。
   - 再对照 `parse_options` 的 `for` 循环（[ds4_cli.c:1420-1611](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1420-L1611)），找出每个字段对应哪个命令行选项（例如 `temperature` ← `--temp`，`ctx_size` ← `-c`/`--ctx`，`think_mode` ← `--think`/`--think-max`/`--nothink`）。
3. **需要观察的现象**：你会发现有些字段（如 `metal_graph_test`）一旦被设为 `true`，`parse_options` 还会**顺手改掉后端**（强制 Metal/CUDA），见 [ds4_cli.c:1575-1581](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1575-L1581)。
4. **预期结果**：得到一张「选项 → 字段 → 默认值」三列表，其中 `temperature` 的默认值是 `1.0`（来自 `DS4_DEFAULT_TEMPERATURE`）。

#### 4.1.5 小练习与答案

**练习 1**：用户同时指定了 `-p "A"` 和 `--prompt-file f.txt`，会发生什么？
**答案**：第二次设置 prompt 时，[ds4_cli.c:1443-1446](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1443-L1446) 会检测到 `c.gen.prompt` 已被占用，打印 `ds4: specify only one prompt source` 并 `exit(2)`。

**练习 2**：为什么 `cli_config.dist` 是指针，而 `engine`/`gen` 不是？
**答案**：`engine` 和 `gen` 是每次运行都必有的、体积可控的内嵌结构；`dist` 用指针是因为它由 `ds4_dist_options_create()` 在堆上构造（[ds4_cli.c:1413](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1413)），涉及分布式子系统较大的内部状态，用指针可以延迟构造、统一用 `ds4_dist_options_free` 释放，也避免 `cli_config` 结构体本身过大。

---

### 4.2 one-shot vs interactive：main 如何分发

#### 4.2.1 概念说明

`ds4` 有两种截然不同的运行姿态：

- **一次性模式（one-shot）**：带 `-p "..."`，渲染一条聊天提示 → prefill → 生成 → 打印 → 退出。适合脚本调用、批处理。
- **交互模式（interactive REPL）**：不带 `-p`，进入 `ds4>` 提示符，可以多轮对话、随时 `/think`、`/ctx N` 改参数，每轮复用同一个 live KV checkpoint。适合人工聊天。

区分这两者的唯一判据是：**`cfg.gen.prompt == NULL` 吗？** 是 NULL → 交互模式；非 NULL → 一次性模式。这是 `main` 里一行 `if` 决定的。

除了这两种主路径，`main` 还有一批**前置特例分支**（dump-tokens、inspect、imatrix、perplexity、分布式 worker），它们会提前 `return`，不走生成路径。

#### 4.2.2 核心流程

`main` 的整体分发改写为伪代码：

```
main():
    cfg = parse_options(argv)
    if cfg.gen.dump_tokens:          # 仅分词，不加载引擎权重做推理
        return ds4_dump_text_tokenization(...)
    engine = ds4_engine_open(cfg.engine)   # 昂贵：mmap 模型 + 初始化 GPU
    if cfg.dist.role == WORKER:
        return ds4_dist_run(engine, ...)   # 这个进程当分布式 worker，不本地生成
    if cfg.inspect:        return ds4_engine_summary(engine)
    if cfg.gen.imatrix_output_path:  return ds4_engine_collect_imatrix(...)
    if cfg.gen.perplexity_file_path: return run_perplexity_file(...)
    if cfg.gen.prompt == NULL:       return run_repl(engine, cfg)      # 交互
    else:                            return run_generation(engine, cfg) # 一次性
```

而 `run_generation` 内部还有一层二级分发：诊断模式（head/first-token/metal-graph 测试、dump logits/logprobs）会提前返回；否则按 **temperature** 选择生成内核：

```
run_generation(engine, cfg):
    prompt = build_prompt(engine, cfg.gen)        # 渲染+分词
    if 诊断开关:   return 对应诊断函数
    if temperature > 0  OR  分布式协调者  OR  mtp draft > 1:
        return run_sampled_generation(...)        # 采样路径（默认）
    else:
        return ds4_engine_generate_argmax(...)    # 贪婪 argmax 路径（--temp 0）
```

**关键细节**：因为默认 `temperature = 1.0 > 0`，所以**默认的 `./ds4 -p "..."` 走的是 `run_sampled_generation`（带 min_p/top_p 的采样路径），而不是 `ds4_engine_generate_argmax`**。只有显式 `--temp 0`（或处于分布式协调者角色、或开了多 token 投机）时才会落到 argmax 这条更快的确定性路径。这一点很重要，因为 min_p=0.05 已经会过滤掉大量低概率 token，采样和纯 argmax 在长文本上结果可能不同。

#### 4.2.3 源码精读

先看 `main` 的特例处理与最终分发：

[ds4_cli.c:1637-1707](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1637-L1707) —— 注意三件事：(1) `--dump-tokens` 是个**短路**，它甚至不调用 `ds4_engine_open`，只做纯分词（[ds4_cli.c:1639-1651](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1639-L1651)）；(2) 引擎打开后，若本进程是分布式 **worker**，立刻把控制权交给 `ds4_dist_run`（[ds4_cli.c:1659-1679](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1659-L1679)），它不本地生成；(3) 真正的一次/交互分发在 [ds4_cli.c:1698-1702](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1698-L1702)，判据就是 `cfg.gen.prompt == NULL`。

`run_generation` 的二级分发：

[ds4_cli.c:873-951](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L873-L951) —— 先 `build_prompt` 渲染分词，然后依次检查诊断开关（head/first-token/metal-graph 测试、dump logits/logprobs）。最后的关键 if/else 在 [ds4_cli.c:922-947](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L922-L947)：`temperature > 0.0f` 走采样，否则走 argmax。

`build_prompt` 自己也有一层小分支——判断用户给的 `-p` 是「裸文本」还是「已经渲染好的完整聊天串」（以特殊 BOS 标记开头）：

[ds4_cli.c:412-419](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L412-L419) —— 这让 `ds4` 既能接受「`-p "解释 Redis"`」（由 CLI 套上系统提示和聊天模板），也能接受「`-p "<｜begin▁of▁sentence｜>..."`」（用户自己渲染好的完整串，常用于复现/测试）。

采样路径 `run_sampled_generation` 是默认 `-p` 真正干活的地方，它展示了「prefill → decode 循环」的标准骨架：

[ds4_cli.c:421-543](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L421-L543) —— 三段式：(1) `ds4_session_create` 建一条时间线（[L423](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L423)）；(2) `ds4_session_sync` 做 prefill（[L454](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L454)）；(3) 一个 `while` 循环反复 `ds4_session_sample` → `ds4_session_eval`（[L476-L511](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L476-L511)），直到 EOS、达到 `n_predict`、或被中断。注意循环条件 `!cli_interrupt_requested()`——这就是下一节的协作式中断入口。

贪婪路径（`--temp 0`）则把整个 prefill+decode 循环打包进一个引擎函数，CLI 只提供两个回调（每生成一个 token 调一次 `print_generated_token`，结束时调 `generation_done`）：

[ds4_cli.c:940-946](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L940-L946) —— 调用 `ds4_engine_generate_argmax`，其签名见 [ds4.h:176-182](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L176-L182)。这条路径更省（不在每个 token 之间回 CLI 的 Python 式解释层），是 README 里「greedy 基准」用的路径。

交互模式 `run_repl` 的主循环用 linenoise 读入每一行，再按「是否以 `/` 开头」分流：

[ds4_cli.c:1221-1235](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1221-L1235) —— 先安装 SIGINT 处理器（下节细讲）、配置 linenoise 多行模式和历史文件，然后进入循环。[ds4_cli.c:1259-1329](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1259-L1329) 是斜杠命令分发（`/think`、`/ctx N`、`/power N`、`/read FILE`、`/quit`），普通文本则交给 `run_chat_turn`。

`run_chat_turn` 是「REPL 里一轮对话」的核心，它和 `run_sampled_generation` 长得很像，但多了**前缀复用**和**回滚**：

[ds4_cli.c:1082-1098](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1082-L1098) —— 它先在 transcript 末尾追加 user 消息和 assistant 前缀，再用 `ds4_session_common_prefix` 算出「当前 KV 里已经缓存了多少前缀」，只对**新增后缀**做 prefill（这就是多轮对话不从头重算的关键，u2-l3 会专题展开）。

帮助文本走的是 `ds4_help.h` 声明的统一接口，CLI 的 `usage()` 只是个薄包装：

[ds4_cli.c:131-133](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L131-L133) 委托给 [ds4_help.h:6-14](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_help.h#L6-L14) 的 `ds4_help_print(fp, DS4_HELP_DS4, topic)`。`DS4_HELP_DS4` 这个枚举值区分了「这是 `ds4` 工具的帮助」而不是 `ds4-server`/`ds4-agent` 的——同一套帮助打印代码服务五个二进制。

#### 4.2.4 代码实践（本讲核心任务）

1. **实践目标**：追踪 `-p "Hello"` **贪婪**一次性推理（`--temp 0`）从命令行参数到 `ds4_engine_generate_argmax` 的完整调用链，写出关键函数名。
2. **操作步骤**：
   - 从 [ds4_cli.c:1637](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1637) `main` 开始。
   - 跟进 `parse_options`（[L1638](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1638)）：`-p "Hello"` 把 `cfg.gen.prompt` 设为 `"Hello"`，`--temp 0` 把 `cfg.gen.temperature` 设为 `0.0f`。
   - 跟进 `ds4_engine_open`（[L1654](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1654)）加载模型。
   - 因为 `prompt != NULL`，分发到 `run_generation`（[L1701](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1701)）。
   - 在 `run_generation` 里：先 `build_prompt`（[L875](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L875)）渲染分词；因 `temperature == 0` 且无 MTP，跳过 `run_sampled_generation`，落到 [L940](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L940) 的 `ds4_engine_generate_argmax`。
3. **需要观察的现象**：把链路写成函数序列：`main → parse_options → ds4_engine_open → run_generation → build_prompt → ds4_engine_generate_argmax`，回调 `print_generated_token`/`generation_done` 沿途把 token 打到 stdout。
4. **预期结果**：关键函数名清单 = `main`、`parse_options`、`ds4_engine_open`、`run_generation`、`build_prompt`（内部还会调 `ds4_encode_chat_prompt`）、`ds4_engine_generate_argmax`。
5. **待本地验证**：在有模型的环境运行 `./ds4 -p "Hello" --temp 0`，观察它是否走 argmax 路径（输出末尾的 `ds4: prefill: ... t/s, generation: ... t/s` 时间统计来自 [ds4_cli.c:535-539](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L535-L539) 那段；argmax 路径的时间统计则在引擎内部）。无 GPU 环境可改用 `--cpu` 但需先 `make cpu`。

> 提醒：如果你想追踪的是**默认** `./ds4 -p "Hello"`（不带 `--temp 0`），它实际走的是 `run_sampled_generation` 而非 `ds4_engine_generate_argmax`，因为默认 `temperature=1.0`。这是初学者最容易踩的坑。

#### 4.2.5 小练习与答案

**练习 1**：`./ds4 --dump-tokens -p "hi"` 会加载模型权重吗？会生成 token 吗？
**答案**：都不会加载完整推理路径。它在 `main` 最开头就短路（[ds4_cli.c:1639-1651](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1639-L1651)），只调 `ds4_dump_text_tokenization` 做纯分词并 `return`，连 `ds4_engine_open` 都不进。

**练习 2**：为什么 README 的速度基准要用「greedy」？对应代码里哪条分支？
**答案**：greedy（`--temp 0`）走 `ds4_engine_generate_argmax`（[ds4_cli.c:940](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L940)），它把 prefill+decode 收进引擎内部紧凑循环，避免每个 token 回到 CLI 层，速度更稳定、可复现，适合做基准。

**练习 3**：交互模式下，第二轮对话为什么不用重新 prefill 整段历史？
**答案**：`run_chat_turn` 用 `ds4_session_common_prefix`（[ds4_cli.c:1096](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1096)）算出 KV 里已缓存的前缀长度，只对新增后缀调用 `ds4_session_sync` 做 prefill。这是 KV 复用的核心，u2-l3 会深入。

---

### 4.3 信号处理与协作式中断

#### 4.3.1 概念说明

「中断生成」这件事在 LLM 推理引擎里有个陷阱：**你不能在 token 生成到一半时硬杀进程**。因为一次 `ds4_session_eval` 会改写 KV 缓存，如果在它中途被 `SIGINT` 杀掉，KV 会停留在不一致的半成品状态，下一次对话就会出错。

ds4 的解法叫**协作式中断**（cooperative interrupt）：

- Ctrl+C 触发 `SIGINT`，但处理函数**不退出进程**，只是把一个全局标志 `cli_interrupted` 设为 1。
- 生成循环在**每个 token 之间**（而不是 token 内部）检查这个标志：`while (... && !cli_interrupt_requested())`。当前这一个 token 总会算完，下一个 token 不再开始，于是循环干净退出。
- 因为检查点都在「KV 处于合法 token 前缀」的安全边界上，所以中断后 KV 仍然可用——交互模式下可以直接回 `ds4>` 继续下一轮。

引擎层在 [ds4.h:229-232](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L229-L232) 有一段注释专门说明这个原则：`ds4_session_sync()` 只在「checkpoint 要么未变、要么代表合法 token 前缀」的安全边界上检查取消请求。

对于**分布式协调者**，中断语义更温和：Ctrl+C 不立即停，而是打印一条「等集群算完当前 token/chunk 再停」的提示，把中断延迟到分布式任务的自然停顿点。

#### 4.3.2 核心流程

```
用户按 Ctrl+C
     │ SIGINT
     ▼
cli_sigint_handler()        # 不退出！
     │ 设置 cli_interrupted = 1
     │ 若分布式忙: 打印 "stopping after cluster finishes..."
     ▼
（当前 token 继续算完）
     │
生成循环 while 顶部检查:
     while (generated < max && !cli_interrupt_requested())
                                        │ 命中 → 退出循环
                                        ▼
                                 generation_done() 收尾
```

#### 4.3.3 源码精读

三个全局 `sig_atomic_t` 标志：

[ds4_cli.c:62-67](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L62-L67) —— `cli_interrupted`（是否收到 Ctrl+C）、`cli_dist_busy`（当前是否在等分布式集群）、`cli_dist_notice_printed`（避免把那条「等集群」提示重复打印）。`volatile sig_atomic_t` 是信号安全的标志位标准写法。

信号处理函数本身只做「设标志 + 写一条提示」两件最小动作：

[ds4_cli.c:69-79](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L69-L79) —— 注意它在分布式忙碌时用 `write(STDERR_FILENO, ...)` 输出提示。为什么用 `write` 而不是 `printf`？因为 `printf` 不是**异步信号安全**（async-signal-safe）的——信号处理函数里只能调用 `write` 这类少数信号安全函数。`cli_dist_drain_msg` 就是那条提示文本（[ds4_cli.c:66-67](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L66-L67)）。

标志位的读/清由两个小 helper 封装：

[ds4_cli.c:81-88](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L81-L88) —— 主循环用 `cli_interrupt_requested()` 读、一轮结束后用 `cli_interrupt_clear()` 清。

**关键事实**：这套信号处理器**只在交互模式 `run_repl` 里安装**，一次性模式不安装：

[ds4_cli.c:1225-1231](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1225-L1231) —— `sigaction(SIGINT, &sa, &old_int)` 注册 `cli_sigint_handler`，并把旧的处置存进 `old_int`。退出 REPL 时恢复（[ds4_cli.c:1332](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1332)）。

> 含义：在交互模式下 Ctrl+C = 「停掉这一轮生成，回到 `ds4>`」；在一次性模式下 Ctrl+C = 默认处置（直接终止进程），因为一次性任务跑完就退出，没必要协作式停止。`run_sampled_generation` 的循环里仍写了 `!cli_interrupt_requested()` 检查（[ds4_cli.c:476](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L476)），这是一种**防御性**写法——万一将来有别的调用者装了处理器，它也能正确响应。

REPL 循环里的中断检查点：

[ds4_cli.c:1143](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1143) —— `while (generated < max_tokens && !cli_interrupt_requested())`。每个 token 算完才检查，所以当前 token 永远完整。

中断后还要做**收尾决策**——REPL 里如果一行都没生成就被中断，要回滚 transcript 并失效 session：

[ds4_cli.c:1202-1208](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1202-L1208) —— `generated == 0` 时回滚到 `rollback_len`（这一轮开始前的 transcript 长度，[ds4_cli.c:1091](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1091)）并 `ds4_session_invalidate`，保证 KV 与 transcript 重新一致；否则把 EOS 推进 transcript 作为正常回合结束。

分布式协作中断的「忙碌标志」由 `cli_dist_busy_set` 在每次进入/离开引擎调用时切换：

[ds4_cli.c:94-98](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L94-L98) —— 只有协调者进程才设这个标志。于是 [ds4_cli.c:72-78](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L72-L78) 的处理器才能判断「Ctrl+C 到来时是否正卡在等远程 worker」，从而给出更准确的「稍候」提示，而不是让用户以为程序卡死。

#### 4.3.4 代码实践

1. **实践目标**：亲手验证「Ctrl+C 不会在 token 中途杀死 REPL，而是回到 `ds4>`」。
2. **操作步骤**：
   - 运行 `./ds4`（无 `-p`）进入交互模式。
   - 输入一段会触发长思考的提示，例如 `证明素数有无穷多个`，让它开始生成。
   - 在生成中途按一次 Ctrl+C。
3. **需要观察的现象**：生成在「当前 token 打完」后停止，光标回到新的 `ds4>` 提示符；终端打印 `ds4: prefill: ... t/s, generation: ... t/s` 的计时行（[ds4_cli.c:1213-1217](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1213-L1217)）。下一轮对话仍能正常继续，证明 KV 没损坏。
4. **预期结果**：进程**不退出**，回到提示符；这正是因为 [ds4_cli.c:1229](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1229) 把 SIGINT 换成了标志位处理。
5. **待本地验证**：无 GPU/模型时无法跑推理，可改为「源码阅读型实践」——在 [ds4_cli.c:69-79](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L69-L79) 的处理函数里追踪：如果删掉 `cli_interrupted = 1` 这一行，循环条件 `!cli_interrupt_requested()` 永远为真，Ctrl+C 将完全失效（进程不会停，因为没有别的退出途径）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `cli_sigint_handler` 里用 `write` 而不是 `fprintf` 打印提示？
**答案**：信号处理函数只能调用**异步信号安全**的函数。`fprintf`/`printf` 不是信号安全的（它们操作 stdio 内部缓冲区、可能持锁），在被信号打断的上下文里调用可能死锁或损坏缓冲区；`write` 是信号安全的。

**练习 2**：在一次性模式 `./ds4 -p "..."` 下按 Ctrl+C，会发生什么？为什么和交互模式不一样？
**答案**：一次性模式没有 `sigaction` 注册处理器（只在 `run_repl` 里注册，见 [ds4_cli.c:1230](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1230)），所以 SIGINT 走默认处置——直接终止进程。一次性任务本就是「跑完即退」，没有「回到提示符」的需求，所以不需要协作式停止。

**练习 3**：分布式协调者收到 Ctrl+C 时，`cli_dist_busy` 标志的作用是什么？
**答案**：它告诉信号处理器「此刻正卡在等远程 worker 算完一个 token/chunk」。如果是，处理器打印 `cli_dist_drain_msg`（「等集群算完当前 token/chunk 再停」，[ds4_cli.c:66-67](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L66-L67)），让用户知道程序没死、只是延迟到自然停顿点。这个标志由 `cli_dist_busy_set` 在每次引擎调用前后切换（[ds4_cli.c:485/494/501/503](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L485-L503)）。

## 5. 综合实践

把本讲三节串起来，做一个「`-p` 路径全景追踪」任务：

1. **配置层（4.1）**：用 `./ds4 -p "Hello" --temp 0 --nothink -n 50 -c 8192` 这条命令，逐个参数指出它改写了 `cli_generation_options` / `ds4_engine_options` 的哪些字段，默认值分别是什么（参考 [ds4_cli.c:1392-1411](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1392-L1411)）。
2. **分发层（4.2）**：画出这条命令从 `main` 到 `ds4_engine_generate_argmax` 的函数调用图，标注每一处分支判据（`prompt != NULL`、`temperature > 0`、诊断开关）。说明为什么这条命令**最终走 argmax 而不是采样**。
3. **对比层**：把命令改成 `./ds4 -p "Hello"`（去掉 `--temp 0`），重新追踪——这次它走 `run_sampled_generation`。在 [ds4_cli.c:421-543](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L421-L543) 里标出 prefill（`ds4_session_sync`）和 decode（`ds4_session_sample` + `ds4_session_eval`）两步分别在哪几行。
4. **中断层（4.3）**：最后改成不带 `-p` 的 `./ds4`，在 REPL 里生成中途按 Ctrl+C，对照 [ds4_cli.c:69-79](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L69-L79) 和 [ds4_cli.c:1143](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1143) 解释「为什么 token 不会算到一半」。

产出物：一张包含「命令 → 字段 → 分支 → 函数链」的表格，加一段对默认采样路径 vs argmax 路径差异的说明。

## 6. 本讲小结

- `ds4` 的配置分两层：`cli_config`（= engine + dist + gen + CLI 私有字段）是总容器，`cli_generation_options` 是「这一次生成怎么跑」的旋钮集，全部在 `parse_options` 里从 `argv` 一次性填充。
- `main` 的分发判据是 `cfg.gen.prompt == NULL`：非空走一次性 `run_generation`，空走交互 `run_repl`；之前还有 dump-tokens / inspect / imatrix / perplexity / 分布式 worker 等短路特例。
- **默认 `temperature=1.0`，所以默认 `-p` 走的是采样路径 `run_sampled_generation`**；只有 `--temp 0`（或分布式协调者、或开 MTP）才落到更快的 `ds4_engine_generate_argmax`。
- 一次性采样路径是「`ds4_session_create` → `ds4_session_sync`(prefill) → `while(sample→eval)`」三段式；REPL 的 `run_chat_turn` 在此基础上多了前缀复用和中断回滚。
- 交互模式用 linenoise 读行、用斜杠命令运行时改参；帮助文本统一委托给 `ds4_help_print`。
- **协作式中断**：Ctrl+C 只设 `cli_interrupted` 标志，生成循环在每个 token 之间检查，从而保证 KV 永远停在合法前缀；该处理器只在 `run_repl` 里安装。

## 7. 下一步学习建议

- **u2-l3 Session 同步与前缀复用**：本讲多次提到 `ds4_session_sync` 和 `ds4_session_common_prefix`，下一讲会专题讲解「多轮对话如何只增量 prefill 后缀、何时判定需要重建」。
- **u3-l3 分词器与聊天模板渲染**：本讲的 `build_prompt` 调用了 `ds4_encode_chat_prompt`/`ds4_tokenize_rendered_chat`，如果你想搞懂「裸文本怎么变成带 `<｜begin▁of▁sentence｜>` 的 token 序列」，去读分词器那一讲。
- **延伸阅读源码**：直接打开 [ds4_cli.c:421](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L421) 的 `run_sampled_generation` 通读一遍，它是理解 ds4 推理循环最浓缩的样本；之后可以对比 [ds4_bench.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_bench.c) 里类似的「prefill + decode」骨架，看不同前端如何复用同一套引擎 API。
