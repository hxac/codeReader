# benchmark 目录总览与定位

## 1. 本讲目标

本讲是《SGLang benchmark 学习手册》的第一篇。读完本讲，你应当能够：

- 说清楚 `benchmark/` 目录在整个 SGLang 项目里扮演的角色——它是**一组评测与压测脚本**，本身不实现推理引擎，而是去**驱动一个正在运行的 SGLang 服务**。
- 把目录里几十个子目录归到**四大类**：精度评测、在线服务吞吐、内核微基准、基础设施微基准。
- 建立「**客户端脚本 + 服务端进程**」的心智模型，理解为什么几乎所有 README 都要先 `launch_server` 再跑 `bench_xxx.py`。
- 学会通过阅读每个子目录的 `README.md` 直接获得运行命令，而无需去翻源码猜参数。

本讲是「认知地图」级别的内容，不要求你跑通任何东西；它会为后续每一讲铺好坐标。

## 2. 前置知识

- **什么是推理服务（inference serving）**：把一个大语言模型（LLM）部署成一个常驻进程，对外提供 HTTP 接口（如 `/generate`、`/v1/chat/completions`），让客户端把 prompt 发过去、拿回生成结果。SGLang 的服务端通常用 `python -m sglang.launch_server` 启动。
- **客户端 vs 服务端**：在本手册的语境里，「服务端」指加载了模型权重、占着 GPU、一直监听端口的进程；「客户端」指本目录里的 Python 脚本，它连到服务端发请求、收响应、做统计。
- **准确率（accuracy）与吞吐（throughput）的区别**：前者关心「答得对不对」，是质量指标；后者关心「单位时间能处理多少请求/token」，是性能指标。两类指标需要完全不同的基准来衡量，这正是本目录分两大族系的根本原因。
- **微基准（microbenchmark）**：不启动完整服务，而是把某个**很小的算子或数据结构**单独拎出来计时。它追求的是「隔离变量、可重复」，常用于内核调优。
- **什么是 README 驱动**：本目录几乎每个子目录都带一个 `README.md`，里面直接给出「起服务 → 跑脚本」的命令行。阅读 README 是上手任何子目录最快的方式。

## 3. 本讲源码地图

本讲涉及的关键文件（均为真实存在，链接指向当前 HEAD `4eaa5ca6`）：

| 文件 | 作用 |
| --- | --- |
| [benchmark/mmlu/README.md](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/mmlu/README.md) | 精度评测（MMLU）的代表，演示「先 launch_server 再 bench_sglang.py」的客户端-服务端套路 |
| [benchmark/benchmark_batch/benchmark_batch.py](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/benchmark_batch/benchmark_batch.py) | 在线吞吐基准的代表，用随机合成 prompt 压测 `/generate` 端点 |
| [benchmark/kernels/fused_moe_triton/README.md](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/kernels/fused_moe_triton/README.md) | 内核微基准（MoE 调优）的代表，演示 torchrun + 调优产物落地 |
| [benchmark/hicache/README.md](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/hicache/README.md) | 基础设施微基准（分层缓存）的代表，演示不同服务端配置的横向对比 |

此外，本讲在「目录四分类」中会点名引用目录树中真实存在的子目录（见 4.1.3 的扫描结果）。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**目录结构与四分类**、**客户端-服务端模型**、**README 即运行说明**。

### 4.1 目录结构与四分类

#### 4.1.1 概念说明

`benchmark/` 目录里有五十多个条目（子目录 + 若干顶层 `.py`），表面看很杂：有的是人名（`hellaswag`、`mmlu` 是数据集名），有的是机制名（`hicache`、`lora`），有的是算子名（`kernels/all_reduce`）。但只要抓住「**这个脚本到底在量什么**」这一个维度，就能把所有条目归进四类：

1. **精度评测（accuracy eval）**：给模型一道道题，统计「答对率」。结果是一个**质量分数**（如准确率、WER、命中率）。代表：`mmlu`、`gsm8k`、`hellaswag`。
2. **在线服务吞吐（serving throughput）**：给一个已经启动的服务持续灌请求，统计「**单位时间能产出多少 token / 处理多少请求**」以及延迟分布。代表：`benchmark_batch`、`hicache`、`lora`。
3. **内核微基准（kernel microbenchmark）**：不启动服务，单独计时某个 CUDA/Triton 算子（如 all-reduce、fused MoE、attention）。追求**算子级**的延迟或带宽。代表：`kernels/*`。
4. **基础设施微基准（infra microbenchmark）**：衡量推理引擎**内部机制**本身的性能，而不是端到端服务——例如缓存层、存储后端、调度器热路径、请求校验。代表：`hicache`（同时也是吞吐）、`hf3fs`、`scheduler`、`io`。

注意第 2 类与第 4 类会有重叠：`hicache` 既能当成在线吞吐跑（端到端），也能当成「缓存机制本身」的研究对象（基础设施）。本手册在大纲里把 `hicache` 的吞吐脚本放在 Unit 5（服务吞吐），把缓存矩阵放在 Unit 6（基础设施），正是这个原因。

#### 4.1.2 核心流程

把一个陌生子目录归类，只需回答一个问题——**它把结果落成什么形态**：

```text
看子目录
   │
   ├── 产出是「准确率/分数/WER/命中率」？ ──► 精度评测
   │
   ├── 产出是「token/s、QPS、TTFT/TPOT 分布」且需要先起服务？ ──► 在线服务吞吐
   │
   ├── 产出是「某算子在固定形状下的延迟/带宽」，用 torchrun 直接跑、不需要 launch_server？ ──► 内核微基准
   │
   └── 衡量的是引擎内部某个机制（缓存/存储/调度/校验）？ ──► 基础设施微基准
```

还有一个更直接的**外观判据**：精度评测与服务吞吐脚本几乎都叫 `bench_sglang.py` 或 `bench_serving.py`，并且依赖一个跑着的服务；内核微基准通常叫 `benchmark_xxx.py` 或 `bench_xxx.py`，且出现在 `kernels/` 下、用 `torchrun` 启动。

#### 4.1.3 源码精读

先看目录顶层真实存在的条目（来自对 `benchmark/` 的扫描）：

```text
benchmark/
├── mmlu/  hellaswag/  boolq/  ceval/  gsm8k/  reasoning_benchmark/   # 精度评测：选择题/数学/推理
├── mmmu/  llava_bench/  ocr/  asr/                                  # 精度评测：多模态/语音
├── mtbench/  llm_judge/  react/  tree_of_thought_*/  dspy/          # 精度评测：主观/Agent 负载
├── line_retrieval/  multi_document_qa/  multi_chain_reasoning/      # 精度评测：长上下文/检索
├── json_decode_regex/  json_jump_forward/  json_schema/  long_json_decode/  # 精度评测：结构化输出
├── benchmark_batch/  multi_turn_chat/  lora/  prefill_only/         # 在线服务吞吐
├── bench_adaptive_speculative.py                                    # 在线服务吞吐（顶层脚本）
├── hicache/  hf3fs/  scheduler/  io/  bench_pynccl_allocator/  bench_in_batch_prefix/  # 基础设施微基准
├── bench_attention_sink/  bench_rope/  bench_linear_attention/  fla/ # 算子/机制微基准
└── kernels/
    ├── all_reduce/  all_gather/  deepep/                            # 通信集合内核
    ├── fused_moe_triton/  quantization/  deepseek/  lora_csgmv/     # MoE / 量化内核
    └── decoding_attention_triton/  sliding_window_attention_triton/ verify_splitkv_triton/  # 注意力内核
       scheduler_batch/  elementwise/  flashinfer_allreduce_fusion/
```

四类的「代表 README」分别体现了各自的工作方式：

- **精度评测代表**：[benchmark/mmlu/README.md](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/mmlu/README.md) 里给出的标准动作是「先起服务，再跑 `bench_sglang.py`」，参数 `--nsub 10` 控制每科目抽样的题目数。它的产出是一个**准确率**。

- **内核微基准代表**：[benchmark/kernels/fused_moe_triton/README.md](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/kernels/fused_moe_triton/README.md) 介绍的是 MoE 内核调优（第 26-43 行给出 `--tune` 用法）。它的关键特征是**不启动服务**，而是直接用 `python tuning_fused_moe_triton.py --tune` 在本地 GPU 上搜配置，产物是一个 JSON 配置文件，再手动拷到运行时目录被 SGLang 加载。这和精度评测「连服务」的模式完全不同。

- **基础设施微基准代表**：[benchmark/hicache/README.md](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/hicache/README.md) 第 4-14 行一次性给出四种**服务端配置**（关 radix 缓存 / fcfs 调度 / 默认 / 开启 hierarchical cache），然后用同一个 `bench_multiturn.py` 去压。它衡量的是「**缓存机制本身**」给端到端带来的收益，是典型的基础设施研究。

#### 4.1.4 代码实践

1. **实践目标**：亲手把目录归到四类，建立空间感。
2. **操作步骤**：
   - 进入 `benchmark/` 目录，浏览 `kernels/` 之外的子目录名。
   - 列出你认为属于「精度评测」的 5 个子目录、属于「内核微基准」的 5 个子目录（`kernels/` 下）。
   - 对每个写一句话说明它的评测目标（可以参考各自的 `README.md`）。
3. **需要观察的现象**：你会发现精度评测子目录几乎都叫数据集名（`mmlu`/`gsm8k`/`hellaswag`…），而内核微基准都叫算子名（`all_reduce`/`fused_moe_triton`/`decoding_attention_triton`…）——命名规律本身就是分类线索。
4. **预期结果**：得到一张两列、共 10 行的小表，例如：
   - 精度评测：`mmlu`（多选题准确率）、`gsm8k`（小学数学准确率）、`hellaswag`（常识完形准确率）、`asr`（语音识别 WER）、`ocr`（图文理解准确率）。
   - 内核微基准：`kernels/all_reduce`（集合通信延迟）、`kernels/fused_moe_triton`（MoE 算子调优）、`kernels/quantization`（量化内核调优）、`kernels/decoding_attention_triton`（解码注意力延迟）、`kernels/deepseek`（DeepGEMM FP8）。
5. 本练习是「阅读 + 归类」，不需要 GPU，可直接完成。

#### 4.1.5 小练习与答案

**练习 1**：`benchmark/bench_adaptive_speculative.py` 是顶层的一个独立脚本，它属于哪一类？为什么？

> **参考答案**：属于「在线服务吞吐」。从命名（`bench_adaptive_speculative`，针对投机解码）和大纲说明（u5-l4）可知它需要先起一个开了投机解码的服务，再压测 `accept_length`、吞吐与延迟，因此是服务端压测而非精度评测或纯算子微基准。

**练习 2**：`benchmark/hicache` 同时出现在「在线服务吞吐」和「基础设施微基准」两类里，这矛盾吗？

> **参考答案**：不矛盾。它是一个**族系**而非单脚本：其中 `bench_serving.py` 这类是端到端服务吞吐，而 `bench_long_context.py`/`bench_warm_cache.py` 这类更偏向研究「分层缓存这一机制本身」的收益。同一目录横跨两类，恰恰说明它内容丰富。

---

### 4.2 客户端-服务端模型

#### 4.2.1 概念说明

`benchmark/` 里绝大多数脚本自己**不会**加载模型、不会占 GPU。它们是「客户端」。真正干活的是另一个用 `python -m sglang.launch_server` 启动起来的「服务端」进程。客户端通过 HTTP（默认 `http://127.0.0.1:30000`）把请求送过去，再把结果收回来统计。

这个心智模型极其重要，因为它解释了三件事：

- 为什么所有 README 都是「先起服务，再跑脚本」两步走。
- 为什么脚本能换后端（sglang / vllm / lightllm / lmql…）——只要后端实现了同样的 HTTP 接口，客户端脚本就通用。
- 为什么脚本里常有 `endpoint`、`base_url`、`/generate` 这种字眼——它们就是 HTTP 客户端的标配。

#### 4.2.2 核心流程

```text
1. 启动服务端：python -m sglang.launch_server --model-path <模型> --port 30000
        │  （加载权重，占 GPU，监听端口）
        ▼
2. 运行客户端脚本：python3 bench_xxx.py
        │  （构造 prompt / 采样请求）
        │  ─── HTTP POST /generate ───► 服务端
        │  ◄── 返回生成结果 / 流式 token ── 服务端
        ▼
3. 客户端统计指标：准确率 / 吞吐 / 延迟，打印或写 JSONL
```

#### 4.2.3 源码精读

看吞吐脚本 [benchmark/benchmark_batch/benchmark_batch.py](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/benchmark_batch/benchmark_batch.py) 是理解这个模型最快的方式。

- **服务端地址写死成常量**：[benchmark_batch.py:12-24](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/benchmark_batch/benchmark_batch.py#L12-L24) 里，`from sglang.lang.backend.runtime_endpoint import RuntimeEndpoint` 导入了一个端点封装，而 `ENDPOINT_URL = "http://127.0.0.1:30000"` 直接指向本机 30000 端口——这正是上面 `launch_server --port 30000` 监听的端口。`NUM_TOKENS`/`BATCH_SIZE`/`GEN_TOKENS` 等则是合成负载的配置。

- **构造一个端点对象**：[benchmark_batch.py:167-169](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/benchmark_batch/benchmark_batch.py#L167-L169) 的 `main()` 里 `endpoint = RuntimeEndpoint(ENDPOINT_URL)`，把 URL 包成一个可复用的客户端对象。

- **真正发 HTTP 请求**：[benchmark_batch.py:75-98](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/benchmark_batch/benchmark_batch.py#L75-L98) 的 `send_batch_request()` 用 `requests.post(endpoint.base_url + "/generate", json=data, timeout=3600)` 向服务的 `/generate` 端点 POST 一批 prompt。第 87 行那句 `endpoint.base_url + "/generate"` 就是「客户端拼 URL 打服务端」最直白的证据。

- **在客户端算吞吐**：[benchmark_batch.py:149-151](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/benchmark_batch/benchmark_batch.py#L149-L151) 里 `throughput = total_prompts / total_time`，吞吐是**客户端**根据自己测量的墙钟时间算出来的，服务端并不主动汇报。

精度评测这边也是同一套模型：[benchmark/mmlu/bench_sglang.py:12-16](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/mmlu/bench_sglang.py#L12-L16) 从 `sglang.test.test_utils` 导入 `add_common_sglang_args_and_parse` / `select_sglang_backend` / `dump_bench_raw_result`，其中 `select_sglang_backend` 就是用来「选一个连到哪个后端」的——同样是客户端连服务端。

#### 4.2.4 代码实践

1. **实践目标**：在不真正发请求的情况下，验证「客户端脚本连的是 30000 端口的服务」。
2. **操作步骤**：
   - 打开 [benchmark_batch.py](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/benchmark_batch/benchmark_batch.py)，找到 `ENDPOINT_URL` 常量（第 17 行）。
   - 把它改成 `http://127.0.0.1:29999`（一个你确信没有进程监听的端口）。
   - 准备一个最小的 tokenizer 目录路径赋给 `TOKENIZER_DIR`（可指向本地任意可用的小模型，仅用于随机 prompt 生成，与服务端无关）。
3. **需要观察的现象**：脚本会在 `requests.post(...)` 处抛连接异常（如 `ConnectionRefusedError` 或 `requests.exceptions.ConnectionError`），打印 `[Request] Error for request 1: ...`。
4. **预期结果**：这恰恰反证了「客户端必须有一个能连得上的服务端」——只要端口空着，客户端再正确也无法工作。验证完后**务必把改动还原**，本讲要求不修改源码。
5. 若手头没有 GPU/模型，可只做「阅读源码确认 `base_url + "/generate"`」这一步，标注「待本地验证」即可，不要假装运行过。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `benchmark_batch.py` 可以用 `requests`（标准 HTTP 库）直接打，而不依赖任何 SGLang 运行时？

> **参考答案**：因为服务端对外暴露的就是普通 HTTP 接口（`/generate`）。`requests.post(...)` 只是在发 HTTP，后端用 vllm 还是 sglang 都无所谓，只要接口一致。`RuntimeEndpoint` 只是个把 URL 包起来的薄封装。

**练习 2**：脚本里的 `temperature`/`max_new_tokens` 是在哪里组装的？它是给谁的？

> **参考答案**：在 [benchmark_batch.py:77-82](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/benchmark_batch/benchmark_batch.py#L77-L82) 的 `sampling_params` 里组装，随请求体一起 POST 给服务端，由服务端的采样器使用。客户端只负责「带上参数」，不负责真正采样。

---

### 4.3 README 即运行说明

#### 4.3.1 概念说明

`benchmark/` 的设计哲学是「**每个子目录自包含、自带说明书**」。几乎每个子目录都有一个 `README.md`，里面用 Markdown 代码块直接给出「起服务 → 跑脚本」的完整命令。这意味着你**不需要读 Python 源码就能上手**：复制 README 里的命令，改一下 `--model-path`，就能跑。

这一点很关键：本目录的 README 不是「项目介绍」，而是「**可复现的操作手册**」。

#### 4.3.2 核心流程

```text
拿到一个子目录
   │
   ├── 先看 README.md ──► 找到 Server 块（起服务）和 Client 块（跑脚本）
   │
   ├── 改 --model-path / --port 等参数为你的环境
   │
   └── 按顺序执行：先 Server，等它 ready，再 Client
```

#### 4.3.3 源码精读

- [benchmark/mmlu/README.md:8-15](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/mmlu/README.md#L8-L15) 典型的两段式：第一个代码块 `python -m sglang.launch_server ... --port 30000` 起服务，第二个代码块 `python3 bench_sglang.py --nsub 10` 跑评测。同一份 README 接着还给出 vllm / lightllm / guidance / lmql 的对应命令（第 22-59 行），演示了「同一客户端脚本，换不同后端服务」。

- [benchmark/gsm8k/README.md:1-30](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/gsm8k/README.md#L1-L30) 进一步说明 README 会带「数据集说明」（GSM8K Platinum 是更稳定的修订版）和参数含义（`--num-shots`、`--num-questions`、`--platinum`）。

- [benchmark/kernels/fused_moe_triton/README.md:22-43](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/kernels/fused_moe_triton/README.md#L22-L43) 是内核微基准的 README 范式：它给出 `--model` / `--tp-size` / `--dtype` / `--tune` 等参数，并在第 148-154 行说明「调优产物是 JSON，要拷到 `sglang/srt/layers/moe/moe_runner/.../configs/` 才被加载」——README 把「产物怎么用」也讲清楚了。

- [benchmark/hicache/README.md:1-22](https://github.com/sgl-project/sglang/blob/4eaa5ca6510622cb0006bcfee5947b17859ac8c7/benchmark/hicache/README.md#L1-L22) 给出的是「**对照实验**」范式：四条不同的 `launch_server` 命令对应四种缓存/调度配置，让你用同一负载横向对比；第 22 行还点明了「分层缓存的收益取决于可复用 token 与显存的比例」——README 顺手把**结论**也告诉你了。

#### 4.3.4 代码实践

1. **实践目标**：训练「读 README 即可上手」的能力。
2. **操作步骤**：
   - 任选三个精度评测子目录（如 `mmlu`、`gsm8k`、`hellaswag`），只读它们的 `README.md`。
   - 从每个 README 里摘出「起服务命令」和「跑脚本命令」各一条，整理成一张表。
3. **需要观察的现象**：三份 README 的结构高度一致（Server 块 + Client 块 + 其他后端块），可复用性强。
4. **预期结果**：得到一张三行的小表，例如 `mmlu` → `launch_server Llama-2-7b-chat-hf:30000` + `bench_sglang.py --nsub 10`。你会确认：**不读 Python 源码，仅凭 README 就能组织一次评测**。
5. 无需 GPU，纯阅读即可完成。

#### 4.3.5 小练习与答案

**练习 1**：如果一个子目录的 README 写着 `python3 bench_other.py --backend vllm`，这说明 `bench_other.py` 相比 `bench_sglang.py` 多承担了什么角色？

> **参考答案**：`bench_other.py` 是「**多引擎对比入口**」。`bench_sglang.py` 通常默认连 sglang 后端，而 `bench_other.py` 通过 `--backend` 在 vllm / lightllm / guidance / lmql 等之间切换，用于把同一数据集打到不同引擎上做公平对比（见 u2-l5）。

**练习 2**：内核微基准的 README（如 fused_moe_triton）和精度评测的 README 在「产物」上有什么本质不同？

> **参考答案**：精度评测的产物是**分数/JSONL 结果**；内核微基准（调优类）的产物是**一份被运行时加载的配置 JSON**（决定 SGLang 实际用哪个 kernel 配置）。前者衡量现状，后者直接改变运行时行为。

---

## 5. 综合实践

把本讲三个模块串起来，完成一张「**benchmark 目录四分类地图**」：

1. **任务**：用本讲的判据（4.1.2），对 `benchmark/` 下的条目做一次完整归类，填入下表。每类至少列 5 个，并各挑 1 个写出「它需要先起服务吗？」和「它的产物是什么形态？」。

   | 类别 | 子目录示例（≥5） | 是否先 launch_server | 产物形态 |
   | --- | --- | --- | --- |
   | 精度评测 | mmlu, gsm8k, … |  |  |
   | 在线服务吞吐 | benchmark_batch, … |  |  |
   | 内核微基准 | kernels/all_reduce, … |  |  |
   | 基础设施微基准 | hicache, hf3fs, … |  |  |

2. **验证**：随机挑 2 个你归类的子目录，打开它的 `README.md`，核对：①它的命令是否匹配你判断的「是否先起服务」；②它统计的指标是否匹配你写的「产物形态」。
3. **反思**：找出 1 个让你犹豫、可能横跨两类的条目（如 `hicache`、`prefill_only`、`bench_in_batch_prefix`），用一句话解释它为什么横跨两类。
4. **预期结果**：得到一张可保存的地图，它将作为你阅读后续每一讲的「导航」。后续每讲开头的「源码地图」都可以挂回这张表。

## 6. 本讲小结

- `benchmark/` 是 SGLang 的**评测与压测脚本集合**，自身不含推理引擎，而是去驱动一个运行中的服务。
- 抓住「量什么」这一维度，目录可归为四类：**精度评测、在线服务吞吐、内核微基准、基础设施微基准**。
- 几乎所有脚本都是「**客户端**」，通过 HTTP（默认 `127.0.0.1:30000` 的 `/generate` 等端点）连到用 `launch_server` 启动的「**服务端**」。
- README 是「**可复现的操作手册**」：先 Server 块、后 Client 块，多数情况下改 `--model-path` 即可上手，无需读源码。
- 精度评测与吞吐脚本的产物是**结果**（分数/延迟分布），内核调优脚本的产物是**配置 JSON**，会被运行时加载。
- 命名规律本身是分类线索：数据集名 → 精度评测；算子名 → 内核微基准；机制名 → 基础设施微基准。

## 7. 下一步学习建议

- 想立刻跑通一个精度评测？→ 下一讲 **u1-l2《精度评测的通用骨架：以 MMLU 为例》**，会拆解 `bench_sglang.py` 的标准模板（`add_common_sglang_args_and_parse` / `select_sglang_backend` / `sgl.function` / `run_batch`）。
- 想先跑一次吞吐？→ **u1-l3《第一次吞吐基准：合成数据的批量请求》**，深入 `benchmark_batch.py` 的随机 prompt 预生成与延迟/吞吐统计。
- 对内核调优更感兴趣？→ 可跳到 **u7-l1**，但建议先打下 Unit 1 的整体认知再进入高级内容。
- 阅读建议：把本讲的「四分类地图」留在手边，后续每讲都对应地图上的一格，边学边定位。
