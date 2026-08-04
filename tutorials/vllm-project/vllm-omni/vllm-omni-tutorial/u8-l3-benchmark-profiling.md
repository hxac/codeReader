# 基准测试与性能剖析

## 1. 本讲目标

学完本讲，你应该能够：

- 用 `benchmarks/diffusion/diffusion_benchmark_serving.py` 对一个已经启动的 `--omni` 服务做压测，读懂它输出的吞吐（QPS）、延迟分位数（p50/p95/p99）和可选的 SLO 达成率。
- 说清 `vllm_omni/benchmarks/serve.py` 与 `vllm_omni/benchmarks/patch/patch.py` 是如何「不改上游代码、用 monkey-patch 把 omni 的多模态数据集与多模态后端」接到 vLLM 的基准脚本上的。
- 用 `bench_attention_backends.py` 对比不同注意力后端（SDPA / FlashInfer / FA4）在同一形状下的单 kernel 耗时，并用 `quantization_quality.py` 用 LPIPS 评估量化带来的感知质量损失。
- 理解流式 TTS 的「连续性」指标（`audio_continuity.py`）——为什么 RTF 合格还不够、还要看分块到达会不会让播放器「断流」。
- 按 `docs/contributing/profiling.md` 抓取一次 torch profiler 的 `trace.json`，并用 `/start_profile`、`/stop_profile` 在在线服务里定位耗时瓶颈。

## 2. 前置知识

本讲属于专家层（advanced），默认你已经学完入门层的在线服务（[u1-l5](u1-l5-online-quickstart.md)，知道 `vllm serve --omni` 怎么启动、OpenAI 兼容端点长什么样）以及 U7 的 Diffusion 加速（[u7-l1](u7-l1-attention-backends.md) 注意力后端、[u7-l3](u7-l3-cache-acceleration.md) 缓存加速）。下面是几个本讲反复用到的概念，先用大白话过一遍：

- **压测（benchmark / load test）**：给一个已经在跑的服务，按一定速率并发地发一批请求，记录每个请求的延迟、整体吞吐，最后算出统计量。关键是「请求从哪来、按什么节奏发、结果怎么算」三件事。
- **吞吐（throughput）**：单位时间完成多少个请求（req/s，也叫 QPS）或产出多少内容（如每秒生成多少音频秒数、多少张图）。
- **延迟分位数（latency percentile）**：把所有请求的延迟排序，p50 表示一半请求比它快、p99 表示 99% 的请求比它快。p50 看平均体感，p99 看最差体感。
- **TTFT / ITL / TPOT**：文本生成的延迟分解——TTFT 是「首 token 时延」，ITL 是「相邻 token 间隔」，TPOT 是「每 token 平均耗时」。TTS 还有一个对应的 **audio TTFP（time to first audio packet）**，即第一个音频块到达的时间。
- **RTF（real-time factor）**：生成耗时与音频实际时长的比值。RTF < 1 说明服务器「追得上实时」，是 TTS 吞吐的常见指标。但本讲会告诉你它还不够。
- **monkey-patch**：在运行时替换别人模块里的函数/属性，u2-l1 讲过。本讲会再次看到 vLLM-Omni 怎么用这套手法「寄生」在 vLLM 的基准脚本上。
- **profiler / trace**：性能剖析器。torch profiler 会记录每一步算子的 CPU/CUDA 耗时，导出 `trace.json`，用 Perfetto 打开就能看到一条条算子的时间线，定位「哪一步最慢」。

一句话定位本讲：**vLLM-Omni 没有从零写一套基准与剖析工具，而是「复用上游 vLLM + patch 多模态扩展 + 自带若干专门诊断脚本」**——这和它「修改（🟡）+ 新增（🔴）」的整体哲学完全一致。

## 3. 本讲源码地图

本讲涉及的文件分两类：**顶层 `benchmarks/`**（可独立运行的脚本，照着 README 跑就行）和 **包内 `vllm_omni/benchmarks/`**（被脚本 import 的扩展库）。

| 文件 | 作用 |
|---|---|
| `benchmarks/diffusion/diffusion_benchmark_serving.py` | Diffusion 在线服务压测主脚本：准备数据集 → 并发发请求 → 算吞吐/延迟/SLO |
| `benchmarks/diffusion/bench_attention_backends.py` | 注意力后端单 kernel 诊断：用合成 Q/K/V 对比 SDPA/FlashInfer/FA4 谁快 |
| `benchmarks/diffusion/quantization_quality.py` | 量化质量评估：同 seed 跑 BF16 与量化，用 LPIPS 算感知距离 |
| `vllm_omni/benchmarks/serve.py` | 通用在线服务压测的 omni 入口，委托上游 `vllm.benchmarks.serve.main_async` |
| `vllm_omni/benchmarks/patch/patch.py` | 压测扩展核心：注册 omni 数据集/后端、`MixRequestFuncOutput` 多模态指标、替换上游 `get_samples`/`benchmark` |
| `vllm_omni/benchmarks/audio_continuity.py` | 流式音频连续性指标：模拟实时播放器，算最坏「断流」秒数 |
| `docs/contributing/profiling.md` | 性能剖析指南：torch/cuda profiler、`/start_profile`、orchestrator monitor |

此外，`vllm_omni/benchmarks/metrics/metrics.py` 与 `vllm_omni/metrics/definitions.py` 提供「多模态指标定义」，`benchmarks/diffusion/backends.py` 提供压测脚本的请求函数表。它们本讲会引用但不展开。

## 4. 核心概念与源码讲解

### 4.1 在线服务压测主脚本：diffusion_benchmark_serving.py

#### 4.1.1 概念说明

`diffusion_benchmark_serving.py` 是面向**已经起好的 `--omni` 服务**的压测器。它自己不加载模型，而是像普通客户端一样向 OpenAI 兼容端点（`/v1/chat/completions`、`/v1/images/generations`、`/v1/images/edits`、`/v1/videos`）发请求，记录延迟、统计吞吐。

它要解决的核心问题是：**「我要测的请求千差万别，但我想让它们可复现、可对比」**。为此它把「请求从哪来」抽象成**数据集（Dataset）**，把「按什么节奏发」抽象成**到达过程（request rate / Poisson）**，把「结果怎么算」抽象成 `calculate_metrics`。这套设计让你能用同一个脚本测纯文本生图（t2i）、图生视频（i2v）、图编辑（ti2i/i2i）等不同任务，只要换 `--task`、`--dataset`、`--endpoint` 即可。

#### 4.1.2 核心流程

脚本的执行流可以概括为五步：

```text
1. 解析参数 → 决定 task_type（2i 图 / 2v 视频）和 endpoint
2. 选数据集类（VBench/Trace/Custom/Random）→ 生成 requests_list
3. （可选）warmup 预热，推断 SLO 基准时间
4. 按 Poisson 过程 / 全量并发地投递请求（受 max_concurrency 信号量约束）
5. calculate_metrics 算吞吐、延迟分位数、SLO 达成率，打印 + 落盘 JSON
```

其中「按什么节奏发」由 `iter_requests` 控制：若 `request_rate=inf` 则所有请求在 t=0 一次性发出（压满），否则相邻请求的到达间隔服从指数分布 \( \text{interval} \sim \text{Exp}(\lambda) \)，其中 \(\lambda = \text{request\_rate}\)（这是泊松到达过程的经典模型）。

「并发上限」由 `max_concurrency` 的 `asyncio.Semaphore` 控制：即使你 `--request-rate inf` 一次性放出 50 个请求，若 `--max-concurrency 1`，也只会一个接一个跑。**这是新手最容易踩的坑**：想测吞吐却忘了把 `--max-concurrency` 调大，结果测成了串行。

#### 4.1.3 源码精读

先看「task → endpoint → 请求函数」的路由。脚本先把任务分成视频组与图像组，再决定默认端点：

[benchmarks/diffusion/diffusion_benchmark_serving.py:1210-1217](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/benchmarks/diffusion/diffusion_benchmark_serving.py#L1210-L1217) —— `_default_endpoint_for_task`：视频任务默认打 `/v1/videos`，图编辑打 `/v1/images/edits`，纯生图打 `/v1/chat/completions`。这是「我不传 `--endpoint` 时脚本替我选哪个 API」的依据。

接着用 task_type 去查请求函数表：

[benchmarks/diffusion/diffusion_benchmark_serving.py:1225-1256](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/benchmarks/diffusion/diffusion_benchmark_serving.py#L1225-L1256) —— 把 task 归类为 `2v` 或 `2i`，校验 endpoint 是否合法，最后从 `backends_function_mapping[task_type][endpoint]` 拿到「真正发请求的函数 + 路径后缀」。注意 line 1246-1252 的 fail-fast：endpoint 不合法直接报错并列出所有合法选项，避免默默跑错。

再看数据集选择与请求准备：

[benchmarks/diffusion/diffusion_benchmark_serving.py:1258-1283](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/benchmarks/diffusion/diffusion_benchmark_serving.py#L1258-L1283) —— 按 `--dataset` 实例化 `VBenchDataset` / `TraceDataset` / `RandomDataset` / `CustomDataset`，调 `get_requests()` 得到 `requests_list`；随后把 `--extra-body` 与 `return_stage_metrics` 注入到每个请求的 `extra_body`。

请求到达过程是关键：

[benchmarks/diffusion/diffusion_benchmark_serving.py:982-1000](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/benchmarks/diffusion/diffusion_benchmark_serving.py#L982-L1000) —— `iter_requests`：`request_rate != inf` 时用 `random.expovariate(request_rate)` 采样间隔再 `await asyncio.sleep`，实现泊松到达；`request_rate == inf` 则不等，立即 yield 全部请求。

主循环与并发控制：

[benchmarks/diffusion/diffusion_benchmark_serving.py:1317-1324](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/benchmarks/diffusion/diffusion_benchmark_serving.py#L1317-L1324) —— `start_time` 之后再开始计时，每个请求被包进 `limited_request_func`（受信号量约束），最后 `asyncio.gather` 收齐所有输出，用 `time.perf_counter() - start_time` 得到总时长。注意：**预热（warmup）发生在计时之前**，不会污染指标。

最后是指标计算：

[benchmarks/diffusion/diffusion_benchmark_serving.py:1049-1088](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/benchmarks/diffusion/diffusion_benchmark_serving.py#L1049-L1088) —— `calculate_metrics`：吞吐 = `num_success / total_duration`，延迟取 mean/median/p50/p95/p99，peak memory 取 max/mean/median，还有跨请求聚合的 per-stage 时长。SLO 达成率单独处理（见下面 1090-1111）。

SLO（服务等级目标）是本脚本的高级特性：

[benchmarks/diffusion/diffusion_benchmark_serving.py:945-979](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/benchmarks/diffusion/diffusion_benchmark_serving.py#L945-L979) —— `_populate_slo_ms_from_warmups`：先用 warmup 请求反推「16×16、单帧、单步」的基础耗时 `base_time_ms`，再按面积×帧数×步数线性放大估算每个请求的期望耗时，最后 `slo_ms = expected_ms * slo_scale`（默认 ×3）。它假设延迟随像素面积、帧数、步数**线性**缩放，这在扩散模型上是个合理的一阶近似。

[benchmarks/diffusion/diffusion_benchmark_serving.py:1090-1111](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/benchmarks/diffusion/diffusion_benchmark_serving.py#L1090-L1111) —— SLO 达成率：对每个定义了 `slo_ms` 的请求，看实际延迟是否达标，`slo_attainment_rate = slo_met_success / slo_defined_total`。

#### 4.1.4 代码实践

**实践目标**：对本地一个文生图 `--omni` 服务做一次最小压测，观察吞吐与延迟，并体会 `--max-concurrency` 的影响。

**操作步骤**：

1. 启动服务（参考 [u1-l5](u1-l5-online-quickstart.md)）：

   ```bash
   vllm serve Qwen/Qwen-Image --omni --port 8099
   ```

2. 串行压测（max-concurrency=1）：

   ```bash
   python3 benchmarks/diffusion/diffusion_benchmark_serving.py \
     --base-url http://localhost:8099 --model Qwen/Qwen-Image \
     --task t2i --dataset vbench --num-prompts 4 \
     --height 1024 --width 1024 --num-inference-steps 20 \
     --output-file /tmp/bench_serial.json
   ```

3. 并发压测（max-concurrency=4）：

   ```bash
   python3 benchmarks/diffusion/diffusion_benchmark_serving.py \
     --base-url http://localhost:8099 --model Qwen/Qwen-Image \
     --task t2i --dataset vbench --num-prompts 4 \
     --height 1024 --width 1024 --num-inference-steps 20 \
     --max-concurrency 4 --request-rate inf \
     --output-file /tmp/bench_concurrent.json
   ```

**需要观察的现象**：两次运行的 `Request throughput (req/s)` 与 `Latency` 表。串行时四个请求几乎是一个跑完再跑下一个，吞吐约等于 `1/单请求延迟`；并发时如果服务端支持批处理（`max_num_seqs>1`，见 [u7-l5](u7-l5-diffusion-batching.md)），吞吐应明显上升，单请求延迟可能略升（批处理分摊 vs 排队）。

**预期结果**：待本地验证。可对照 `performance_dashboard/qwen_image_serving_performance.md` 里的连续批处理示例。

> 若没有 GPU / 没装好环境，本实践可降级为「源码阅读型」：阅读 `_run_warmups`（[L1019-L1046](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/benchmarks/diffusion/diffusion_benchmark_serving.py#L1019-L1046)）与 `benchmark` 主循环，回答：为什么 warmup 必须在 `start_time` 之前？

#### 4.1.5 小练习与答案

**练习 1**：`--request-rate inf` 配 `--max-concurrency 1`，与 `--request-rate 1` 配 `--max-concurrency 4`，两种配置测出来的「吞吐」语义有什么不同？

**参考答案**：前者是「全部请求瞬间发出但被信号量卡成串行」，测的是单请求串行吞吐（≈1/单请求延迟）；后者是「每秒放出一个、最多 4 个并发」，测的是「1 req/s 注入速率下的实际处理能力」，若服务处理快于 1 req/s 则吞吐≈1，若慢于 1 req/s 则请求会越积越多。前者测上限，后者测给定负载下的表现。

**练习 2**：为什么 warmup 默认 `--warmup-num-inference-steps 2`（而不是 1）？

**参考答案**：见源码注释 [L1467-L1472](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/benchmarks/diffusion/diffusion_benchmark_serving.py#L1467-L1472)——某些模型（如 BAGEL）跑 `num_timesteps - 1` 步去噪，步数=1 会导致实际执行 0 步而报错，故默认 2 保证至少跑一步。

### 4.2 压测的 omni 扩展：serve.py 与 patch.py

#### 4.2.1 概念说明

上一节的 `diffusion_benchmark_serving.py` 是**专门为 diffusion 写的、自包含的**压测脚本。但 vLLM-Omni 还有大量「文本/多模态/全双工」的服务要测，这时复用上游 vLLM 自带的 `vllm.benchmarks.serve` 才划算。问题是：上游的基准脚本根本不认识 omni 的数据集（如 `daily-omni`、`seed-tts`）和 omni 的多模态后端（如 `openai-chat-omni`、`openai-audio-speech`）。

vLLM-Omni 的解法和 [u2-l1](u2-l1-patch-mechanism.md) 的 patch 哲学一脉相承：**写一个 `serve.py` 入口，import 一段 `patch.py`，这段 patch 在上游脚本「还没用」之前，把 omni 的数据集加载器和后端请求函数「注射」进上游模块的注册表里**。于是上游脚本跑起来时，以为自己一直就有这些能力。

#### 4.2.2 核心流程

```text
serve.py::main(args)
  ├── import patch.py （触发副作用：注册数据集 + 后端 + 替换 get_samples/benchmark）
  ├── maybe_enable_stage_metrics(...)  （把 return_stage_metrics 注入 extra_body）
  └── asyncio.run(main_async(args))   （调用上游 vllm.benchmarks.serve.main_async）
```

patch.py 的副作用可以分三块：

1. **替换 `datasets.get_samples`**：包一层 `get_samples`，先判断是不是 omni 相关的数据集/后端，是则走 omni 的加载逻辑（Daily-Omni / Seed-TTS / random-mm），否则回退到上游原函数 `get_samples_old`。
2. **注册 omni 后端**：把 `async_request_openai_chat_omni_completions`、`async_request_openai_audio_speech`、`async_request_openai_image_edits_omni` 塞进上游的 `ASYNC_REQUEST_FUNCS` 与 `OPENAI_COMPATIBLE_BACKENDS`。
3. **替换 `serve.benchmark`**：用增强版的 `benchmark`（支持多模态指标、stage metrics、Seed-TTS WER、Daily-Omni 准确率）覆盖上游同名函数。

#### 4.2.3 源码精读

先看入口有多薄：

[vllm_omni/benchmarks/serve.py:1-31](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/benchmarks/serve.py#L1-L31) —— `serve.py` 总共 30 行：import patch（注释强调「必须在任何 vllm.benchmarks 模块使用之前 import」）、设几个环境变量、调 `maybe_enable_stage_metrics`，最后 `asyncio.run(main_async(args))`。真正的活全在 patch.py 的 import 副作用里。

再看 patch 怎么「劫持」数据集加载：

[vllm_omni/benchmarks/patch/patch.py:224-243](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/benchmarks/patch/patch.py#L224-L243) —— `get_samples` 的总分流：先用 `args.dataset_name` / `args.backend` 判断是否 omni 相关，**不是就直接 `return get_samples_old(args, tokenizer)`**（完全不影响上游）。这是「向后兼容」的关键——非 omni 场景行为与上游完全一致。

[vllm_omni/benchmarks/patch/patch.py:402-406](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/benchmarks/patch/patch.py#L402-L406) —— 把 `get_samples` 写回 `datasets.get_samples`，并且**连 `vllm.benchmarks.serve` 模块里的同名引用也一并替换**（`_serve_mod.get_samples = get_samples`）。因为上游 `serve.py` 是 `from ...datasets import get_samples`，已经把名字拷到自己模块里了，只改 `datasets` 不够，必须双写。这正是 monkey-patch 的典型坑。

后端注册同样直接改上游全局表：

[vllm_omni/benchmarks/patch/patch.py:1252-1268](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/benchmarks/patch/patch.py#L1252-L1268) —— 把四个 omni backend 名字写进 `ASYNC_REQUEST_FUNCS`（请求函数字典）与 `OPENAI_COMPATIBLE_BACKENDS`（白名单）。注意 `daily-omni` 复用了 `openai-chat-omni` 的请求函数（注释 L1264-L1265 说明：音视频推理 benchmark 复用 chat completions 通道）。

多模态指标靠扩展输出结构承载：

[vllm_omni/benchmarks/patch/patch.py:409-436](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/benchmarks/patch/patch.py#L409-L436) —— `MixRequestFuncOutput` 继承上游 `RequestFuncOutput`，新增 `audio_ttfp`、`audio_rtf`、`audio_underrun_s`、`audio_continuity_ok`、`image_count`、`image_pixels`、`denoise_step_latency_ms`、`stage_metrics` 等字段。这是「omni 要测的东西比上游多」的落点——把多模态结果挂在一个兼容父类的子类上，上游代码完全无感。

最后，整个 `benchmark` 函数被替换：

[vllm_omni/benchmarks/patch/patch.py:1713](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/benchmarks/patch/patch.py#L1713) —— `serve.benchmark = benchmark`：模块末尾用增强版覆盖。这版 `benchmark`（[L1299-L1710](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/benchmarks/patch/patch.py#L1299-L1710)）额外支持：`--profile` 时调 `/start_profile`、`/stop_profile`；SSE 流式解析（音频/图像/文本 delta）；Daily-Omni 准确率与 Seed-TTS WER 后处理。

#### 4.2.4 代码实践

**实践目标**：验证「patch 真的改变了上游行为」，而不是空跑。

**操作步骤（源码阅读型）**：

1. 打开 `vllm_omni/benchmarks/patch/patch.py`，定位 `get_samples` 的分流逻辑（L224-L243）。
2. 追踪一个 omni backend 的请求函数，例如 `async_request_openai_audio_speech`（[L1130](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/benchmarks/patch/patch.py#L1130)），阅读它在流式响应里如何累积 PCM 字节、记录 `audio_ttfp`、调用 `compute_continuity_stats`。
3. 在 [L402-L406](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/benchmarks/patch/patch.py#L402-L406) 处确认「双写」：如果只保留 `datasets.get_samples = get_samples` 而删掉对 `_serve_mod.get_samples` 的赋值，上游 `serve` 还会用到旧函数吗？

**需要观察的现象**：patch 不是「新增一个独立脚本」，而是「替换上游已有符号」；替换发生在 import 时（模块顶层语句），所以「谁先 import 谁」至关重要。

**预期结果**：能用自己的话解释「为什么 serve.py 必须在使用上游 `main_async` 之前 import patch」。待本地验证：可写一个 5 行的 Python 脚本，先 `import vllm.benchmarks.patch.patch`，再 `import vllm.benchmarks.datasets as d`，打印 `d.get_samples.__module__`，应看到它已不是上游原始模块。

#### 4.2.5 小练习与答案

**练习 1**：为什么 patch 要同时替换 `datasets.get_samples` 和 `vllm.benchmarks.serve.get_samples`？

**参考答案**：上游 `serve.py` 用 `from ...datasets import get_samples` 把名字绑定到本模块命名空间，此后 `serve` 内部调用的是本模块的 `get_samples` 引用，不再回头查 `datasets` 模块。只改 `datasets.get_samples` 不会影响已绑定的引用，故必须双写。

**练习 2**：`MixRequestFuncOutput` 为什么要继承 `RequestFuncOutput` 而不是新建一个类？

**参考答案**：上游 `benchmark` 与各处工具函数都按 `RequestFuncOutput` 类型操作输出对象（鸭子类型 + isinstance 检查）。继承保证 omni 的增强输出能被上游代码「当成父类」无缝处理，同时多出来的字段由 omni 自己的请求函数与指标计算读取，实现「上游无感、omni 增强」。

### 4.3 注意力后端与量化质量评测

#### 4.3.1 概念说明

[U7-l1](u7-l1-attention-backends.md) 讲过：diffusion 注意力有多个后端（FLASH_ATTN / CUDNN / FlashInfer / SDPA / TRTLLM …），平台会按优先级自动选一个。但「自动选」未必最优——某个 (GPU 型号, head_dim, seq_len) 组合下，CUDNN 可能没有调优过的 kernel 而悄悄退化到慢速 MATH 后端。`bench_attention_backends.py` 就是用来**单独、可控地**对比这些后端在固定形状下的单 kernel 耗时，把「谁慢」量化出来。

[U8-l1](u8-l1-quantization.md) 讲过量化能省显存、提速。但量化会损失精度，扩散模型损失精度会直接导致生成的图/视频「糊了」。`quantization_quality.py` 用来量化这种**感知质量损失**：同 seed 跑 BF16 基线与量化版，用 **LPIPS**（Learned Perceptual Image Patch Similarity，一个用深度网络衡量两张图「人眼差异」的指标）算距离，LPIPS 越小质量越接近。

#### 4.3.2 核心流程

`bench_attention_backends.py` 的流程很直白：

```text
对每个形状 preset（hv15/wan22/flux 或自定义）：
  1. 造合成 Q/K/V 张量（randn）+ 可选 attn_mask
  2. 遍历每个后端：warmup 几次 → 计时 N 次取中位数（_time_call）
  3. 以 CUDNN_ATTENTION 为 baseline，算每个后端的「倍率」ratio
  4. 打印表格，标出 >1.5× baseline 慢的行（这些就是要从自动路由里关掉的）
```

`_time_call` 的计时方法很标准：先 warmup，再每次 `torch.accelerator.synchronize()` 同步后用 `time.perf_counter()` 测墙钟时间，取中位数降低抖动。任何后端抛异常都不中断整轮（`except` 后返回 `nan` + 错误字符串），保证「一张表跑完」。

`quantization_quality.py` 的流程：

```text
对每个量化方法：
  1. 用 BF16 在固定 seed 下生成基线输出（baseline/）
  2. 用量化配置在同 seed 下生成（<method>/）
  3. 成对计算 LPIPS（图像逐张、视频逐帧取均值）
  4. 输出 Markdown 表格（results.md），可直接贴 PR
```

#### 4.3.3 源码精读

预设形状来自真实模型的「热点注意力形状」：

[benchmarks/diffusion/bench_attention_backends.py:38-55](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/benchmarks/diffusion/bench_attention_backends.py#L38-L55) —— `_PRESETS`：`hv15` 对应 HunyuanVideo-1.5 480p/33f 的 latent 长度 + 文本 token，`wan22` 对应 Wan 2.2，`flux` 是更小的图形状便于冒烟测试。`_SDPA_BACKENDS` 列出要遍历的 torch SDPA 子后端，其中 `CUDNN_ATTN_CHAIN` 模拟上游 PR 里 CUDNN 的「降级链」（CUDNN→FLASH→MATH）。

计时核心：

[benchmarks/diffusion/bench_attention_backends.py:93-112](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/benchmarks/diffusion/bench_attention_backends.py#L93-L112) —— `_time_call`：warmup 后逐次同步计时，`times.sort()` 取中位数。`except Exception` 保证某后端不兼容（dtype/缺 JIT 模块/不支持参数）时返回 `nan` 而非炸掉整个 sweep——注释明说这是 probe 脚本，要保住整张表。

结果呈现与判定：

[benchmarks/diffusion/bench_attention_backends.py:215-235](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/benchmarks/diffusion/bench_attention_backends.py#L215-L235) —— `_print_table`：以 `baseline_name`（默认 CUDNN_ATTENTION）为基准，算每个后端 `ratio = baseline_ms / ms`，`>1.0×` 表示比 baseline 快。脚本开头的 docstring（[L24-L25](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/benchmarks/diffusion/bench_attention_backends.py#L24-L25)）给出了判定准则：**某个后端比 SDPA baseline 慢 >1.5×，就是要在自动路由里关掉它的信号**。

LPIPS 计算（量化质量）：

[benchmarks/diffusion/quantization_quality.py:72-102](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/benchmarks/diffusion/quantization_quality.py#L72-L102) —— `compute_lpips_images`：成对加载基线与量化图，统一 resize 到 256×256 并归一化，用 `lpips.LPIPS(net="alex")` 算距离。LPIPS 用 AlexNet 提特征比「像素 MSE」更贴近人眼，是扩散质量的事实标准之一。视频版 `compute_lpips_video` 逐帧算后取均值（[L105+](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/benchmarks/diffusion/quantization_quality.py#L105)）。

#### 4.3.4 代码实践

**实践目标**：跑一次注意力后端对比，体会「同一形状下不同后端可能差好几倍」。

**操作步骤**（需要 GPU）：

```bash
python benchmarks/diffusion/bench_attention_backends.py --preset flux --sweep
```

`--sweep` 会跑所有预设并在末尾打印「每个 preset 的赢家」。`flux` 形状最小（seq=4096），跑得最快。

**需要观察的现象**：表格里每个后端的 `median (ms)` 与 `vs baseline` 倍率。注意 `status` 列——没装 flashinfer 时会显示 `FAILED (import-ModuleNotFoundError)`，这正是 `_time_call` 容错的价值。

**预期结果**：待本地验证（取决于 GPU 型号与已装库）。典型现象：在支持的形状上 FLASH_ATTENTION / FlashInfer 常快于 MATH；cuDNN 在无调优 kernel 的形状上可能退化。脚本末尾的 Notes（[L320-L324](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/benchmarks/diffusion/bench_attention_backends.py#L320-L324)）提示：要拿端到端时延，需在跑 `text_to_video.py` / `text_to_image.py` 时改 `DIFFUSION_ATTENTION_BACKEND` 环境变量对比。

> 无 GPU 时降级为源码阅读：阅读 `_run_sdpa_variants`（[L115-L131](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/benchmarks/diffusion/bench_attention_backends.py#L115-L131)），说明 `sdpa_kernel(backends)` 上下文管理器如何强制 torch 用指定子后端。

#### 4.3.5 小练习与答案

**练习 1**：脚本为什么用「中位数」而不是「均值」汇总每次计时？

**参考答案**：GPU 计时会受后台活动、调度抖动影响，偶发尖峰会拉高均值；中位数对离群值鲁棒，更能反映「典型一次」的耗时。

**练习 2**：LPIPS 为什么比直接比像素（MSE）更适合评估扩散生成质量？

**参考答案**：人眼对结构、纹理的感知不等于逐像素差。LPIPS 用深度网络的中间层特征度量「感知差异」，对小位移、轻微色调变化不敏感，更贴近「看起来像不像」，这正是扩散模型关心的。

### 4.4 TTS 连续性指标：audio_continuity.py

#### 4.4.1 概念说明

流式 TTS 服务有两个常见指标：**RTF**（real-time factor，生成耗时/音频时长）和 **audio TTFP**（首音频块时延）。但它们都不够——一个反直觉的失败模式是：**总体 RTF < 1（服务器追得上实时），但音频块是「一阵一阵」到达的，播放器按实时速率消费时会发生「断流」（underrun），听众听到卡顿**。

`audio_continuity.py` 就是来量化这个失败模式的。它的思路极其朴素：**给定每个音频块的到达时间线和 PCM 格式，模拟一个「从第一个块开始按实时速率消费」的播放器，找出最坏情况下缓冲被掏空的深度**。它刻意不依赖 vllm（docstring 明说），这样能独立单元测试。

#### 4.4.2 核心流程

播放器模拟的数学很简单。设采样率为 `sample_rate`、每样本 `sample_width` 字节、`channels` 声道，则实时消费速率为：

\[
\text{bytes\_per\_s} = \text{sample\_rate} \times \text{sample\_width} \times \text{channels}
\]

在时刻 \(t_i\)（第 i 个块到达，\(i>0\)），播放器理论上已消费的字节数为：

\[
\text{played} = (t_i - t_0) \times \text{bytes\_per\_s}
\]

而此刻实际已收到的字节数为 `received_before`（前 i-1 个块之和）。若 `played > received_before`，说明播放器想播的比手头有的多，出现赤字（deficit）：

\[
\text{deficit\_s} = \frac{\text{played} - \text{received\_before}}{\text{bytes\_per\_s}}
\]

赤字一旦超过阈值（默认 0.1s，即 100ms——流式 TTS 公认的「可听卡顿」阈值），就算一次 audible underrun。最终返回「最大赤字秒数」`max_underrun_s`、「赤字事件数」`underrun_event_count`、「是否全程连续」`is_continuous`。

#### 4.4.3 源码精读

结果容器：

[vllm_omni/benchmarks/audio_continuity.py:23-36](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/benchmarks/audio_continuity.py#L23-L36) —— `ContinuityStats`：frozen dataclass，三个字段含义如上。`is_continuous` 即 `max_underrun_s <= threshold_s`。

核心算法：

[vllm_omni/benchmarks/audio_continuity.py:39-91](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/benchmarks/audio_continuity.py#L39-L91) —— `compute_continuity_stats`：L68 算 `bytes_per_s`；L72 取首个到达时刻 `t0`；L76-L85 主循环：i>0 时算 `deficit_bytes`，正则累加 `max_underrun_s` 与 `event_count`；L85 把当前块字节累加进 `received_before`。注意 L85 在 deficit 判断**之后**才加当前块——因为播放器在「块 i 到达的瞬间」只能消费此前已到手的字节，当前块刚开始到达、还没算入可用量。这个顺序是模拟正确性的关键。

阈值默认值与可配置：

[vllm_omni/benchmarks/audio_continuity.py:44-46](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/benchmarks/audio_continuity.py#L44-L46) —— 函数签名默认 `threshold_s=0.1`。压测里实际从 `defs.AUDIO_CONTINUITY_DEFAULT_THRESHOLD_S`（[vllm_omni/metrics/definitions.py:264](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/metrics/definitions.py#L264) = 0.1）取，并可用环境变量 `VLLM_OMNI_BENCH_AUDIO_CONTINUITY_THRESHOLD_S` 覆盖（见 patch.py 的 `_audio_continuity_threshold_s`，[L90-L110](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/benchmarks/patch/patch.py#L90-L110)）。

集成进压测：

[vllm_omni/benchmarks/patch/patch.py:1209-1219](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/benchmarks/patch/patch.py#L1209-L1219) —— 在 `async_request_openai_audio_speech` 里，流式收集每个 PCM 块的到达时间与大小（`chunk_arrival_times_s` / `chunk_sizes`，L1188-L1189），结束时调 `compute_continuity_stats`，把 `max_underrun_s` 等写进 `MixRequestFuncOutput`。这就是「连续性指标」在端到端压测里的接入点。

#### 4.4.4 代码实践

**实践目标**：用纯 Python 构造一组「块到达时间」，亲手验证 RTF 合格但连续性不合格的场景。

**操作步骤**（无需 GPU，纯算法）：

1. 写一段示例代码（标注为「示例代码」，非项目原有）：

   ```python
   # 示例代码：模拟「bursty 到达」——前半秒憋着不发，后半秒一股脑发完
   from vllm_omni.benchmarks.audio_continuity import compute_continuity_stats
   # 24kHz, s16le, mono => bytes_per_s = 24000*2*1 = 48000
   # 总共 2 秒音频 = 96000 字节，分成 2 个块
   arrival = [0.0, 1.5]      # 第 2 块迟到 1.5s 才到
   sizes = [48000, 48000]     # 各 1 秒
   print(compute_continuity_stats(arrival, sizes, sample_rate=24000))
   ```

2. 再构造「平稳到达」对照：`arrival = [0.0, 1.0]`，块大小不变。

**需要观察的现象**：第一种 `max_underrun_s` 应接近 0.5（在 t=1.0 时播放器已消费 1 秒=48000 字节，但只收到第 1 块 48000 字节，刚好不亏；在 t=1.0~1.5 之间持续亏损，到 t=1.5 时已消费 72000 字节但只收到 48000，赤字 24000 字节 = 0.5s），`is_continuous=False`（>0.1）。第二种应 `is_continuous=True`。

**预期结果**：待本地验证。两种场景的总耗时与总字节相同（即 RTF 相同），但连续性截然相反——这正是这个指标存在的意义。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `received_before += chunk_bytes[i]` 必须放在 deficit 判断**之后**？

**参考答案**：模拟的是「块 i 到达瞬间播放器的状态」。此刻块 i 刚到、还未被消费计入可用量，播放器只能用前 i-1 个块的累积字节去匹配「按实时速率已应消费」的字节。若先加再判断，会把「刚到尚未消费」的当前块也算进可用量，低估赤字。

**练习 2**：如果一个服务的 p50 RTF=0.5 但连续性频繁失败，你会优先怀疑什么？

**参考答案**：怀疑「块到达抖动」过大——可能是上游 stage 产出音频 latent 后，解码/网络传输存在突发性（如批处理把多个请求的块攒一起 flush），或调度优先级导致某请求被长时间挂起。可结合 4.5 的 orchestrator monitor 排查是否编排线程成为瓶颈。

### 4.5 性能剖析：profiling.md 与 torch/cuda profiler

#### 4.5.1 概念说明

压测告诉你「整体有多慢」，但「到底慢在哪一步」要靠**性能剖析（profiling）**。vLLM-Omni 的 `docs/contributing/profiling.md` 给了一套完整指南，支持两个后端：

- **torch profiler**：生成 `trace.json`（用 Perfetto 打开看算子时间线）、`ops_rank*.xlsx`（算子耗时表）、可选的内存快照。开销大，仅用于调试。
- **cuda profiler**：低开销的 CUDA range 控制，配合 NVIDIA Nsight Systems（`nsys`）抓内核时间线。

diffusion 是多进程架构（worker 在子进程，见 [u5-l3](u5-l3-diffusion-worker-loader.md)），所以剖析有个细节：**worker 子进程自己开关 CUDA 捕获范围**，`nsys` 才能抓到真实 GPU 工作而非只看父进程。在线服务还提供 `/start_profile`、`/stop_profile` 两个 HTTP 端点，让你「先开始剖析、再发请求、再停止」，精确控制捕获窗口。

除了 torch profiler，profiling.md 还介绍了几个**互补的诊断工具**：diffusion pipeline profiler（轻量逐函数计时）、AR profiler、orchestrator monitor（编排线程忙闲比）、Prometheus `/metrics`。它们各有侧重，profiling.md 用一张表（L329-L337）讲清了边界。

#### 4.5.2 核心流程

在线服务剖析的典型流程：

```text
1. 启动服务时带 --profiler-config '{"profiler": "torch", "torch_profiler_dir": "./perf", ...}'
2. curl -X POST /start_profile     （开始捕获）
3. curl 发若干生成请求              （被捕获的窗口）
4. curl -X POST /stop_profile      （停止并 flush 产物到 torch_profiler_dir）
5. 用 Perfetto 打开 trace_rank*.json 定位瓶颈
```

`profiler_config` 的字段（如 `record_shapes`、`with_stack`、`with_memory`、`active_iterations` 等）控制「捕获多详细、捕获哪个迭代窗口」。

#### 4.5.3 源码精读

profiler_config 全字段表：

[docs/contributing/profiling.md:22-39](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/contributing/profiling.md#L22-L39) —— 字段表。重点：`profiler` 选后端，`torch_profiler_dir` 必填（torch），`record_shapes`/`with_stack`/`with_memory` 决定额外产物（形状表/调用栈/内存快照），`delay_iterations`/`active_iterations` 控制捕获第几个 worker 迭代（跳过预热）。

torch profiler 的离线用法：

[docs/contributing/profiling.md:133-151](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/contributing/profiling.md#L133-L151) —— 单阶段 diffusion 用 `Omni(...)` 构造时传 `profiler_config`，再用 `start_profile()`/`stop_profile()` 包住生成调用。脚本只在「启动后停止」才写产物。离线示例脚本支持 `--profiler-config` JSON 参数（L153-L168）。

在线服务的剖析端点：

[docs/contributing/profiling.md:197-262](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/contributing/profiling.md#L197-L262) —— 当 `profiler_config.profiler` 被设置，服务暴露 `POST /start_profile` 与 `POST /stop_profile`。L246-L262 的 Qwen-Image 例子：先 start，再发 `/v1/images/generations` 请求，再 stop。这正是本讲综合实践要复刻的流程。

nsys（cuda 后端）用法：

[docs/contributing/profiling.md:175-195](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/contributing/profiling.md#L175-L195) —— 用 `nsys profile --capture-range=cudaProfilerApi ...` 包住进程，Python 端 `profiler_config={"profiler": "cuda"}`，worker 子进程自己开关捕获范围。L194-L195 解释了为什么要这样：diffusion worker 在子进程，只有子进程开 range，nsys 才看得到真实 GPU 工作。

互补诊断工具的边界：

[docs/contributing/profiling.md:329-337](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/contributing/profiling.md#L329-L337) —— 一张表区分五种工具的 scope 与 output：pipeline profiler 看 stage 函数（vae.decode/diffuse）、AR profiler 看 AR 生成耗时、`profiler_config` 看 GPU/CPU kernel、Prometheus `/metrics` 看 SLO 与跨 stage 传输、orchestrator monitor 看编排线程忙闲与副本队列积压。**选错工具会白费功夫**：编排瓶颈用 torch profiler 看不到（它不在 worker 里），要用 orchestrator monitor。

orchestrator monitor：

[docs/contributing/profiling.md:290-339](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/contributing/profiling.md#L290-L339) —— 多阶段流水线的所有客户端输出与跨 stage 连接器流量都过单一编排进程（见 [u3-l2](u3-l2-orchestrator.md)）。编排线程饱和时，即使 GPU 健康，TTFT 也会劣化。`--enable-orch-monitor`（L298-L302）每秒采样忙闲比与副本队列深度，关停时写一个 JSON。L327-L339 强调它不用 `torch.profiler`，因为瓶颈信号在编排进程（poll 循环占空比、队列深度），不在 stage worker 内。

#### 4.5.4 代码实践

**实践目标**：对一个 diffusion 服务抓一次 torch profiler trace，并用 Perfetto 打开定位最耗时的算子。

**操作步骤**：

1. 带 profiler 配置启动服务：

   ```bash
   vllm serve Qwen/Qwen-Image --omni --port 8099 \
     --profiler-config '{
       "profiler": "torch",
       "torch_profiler_dir": "/tmp/omni_perf",
       "torch_profiler_record_shapes": true,
       "torch_profiler_with_stack": true,
       "active_iterations": 1
     }'
   ```

2. 开另一个终端，控制捕获窗口：

   ```bash
   curl -X POST http://localhost:8099/start_profile
   curl http://localhost:8099/v1/images/generations -H "Content-Type: application/json" \
     -d '{"model":"Qwen/Qwen-Image","prompt":"a red bicycle by a canal at sunset"}'
   curl -X POST http://localhost:8099/stop_profile
   ```

3. 打开 [Perfetto](https://ui.perfetto.dev/)，加载 `/tmp/omni_perf/trace_rank*.json`。

**需要观察的现象**：trace 时间线里最宽（耗时最长）的算子条。对 diffusion 通常是 transformer 的 attention/matmul（`diffuse` 循环，见 [u5-l4](u5-l4-diffusion-pipeline.md)），其次是 `vae.decode`。对照 `ops_rank*.xlsx` 的 `by_stack` 表可看到具体调用栈。

**预期结果**：待本地验证（依赖 GPU 与模型）。若发现 attention 占比异常高，可回到 4.3 用 `bench_attention_backends.py` 验证是否选错了后端；若发现编排/排队占比高（trace 里 GPU 空闲段长），改用 `--enable-orch-monitor` 复测。

> 无 GPU 时降级为源码阅读：阅读 profiling.md L329-L337 的工具对照表，回答「如果 TTFT 高但 GPU 利用率低，该用哪个工具」。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `profiler_config` 字段里要有 `delay_iterations` 和 `active_iterations`？

**参考答案**：前几个 worker 迭代含编译、CUDA graph 捕获、缓存预热等一次性开销，不代表稳态。`delay_iterations` 跳过这些，`active_iterations` 只捕获稳态的若干迭代，让 trace 干净且代表真实运行。

**练习 2**：某次剖析发现 GPU 大段空闲、TTFT 却很高。torch profiler 能定位根因吗？该用什么工具？

**参考答案**：不能直接定位。torch profiler 看 worker 内的 kernel，GPU 空闲说明瓶颈不在 worker 算子，而在「请求到达 worker 之前」——可能是编排线程饱和或跨 stage 连接器/队列积压。应用 `--enable-orch-monitor`（见 [u3-l2](u3-l2-orchestrator.md)）采样编排线程忙闲比与副本队列深度来定位。

## 5. 综合实践

把本讲四条线索串起来，完成一次「**测 → 评 → 剖**」的小型性能调优闭环。假设你已有一个本地 `Qwen/Qwen-Image` 服务：

1. **测（4.1）**：用 `diffusion_benchmark_serving.py` 以 `--max-concurrency 4 --request-rate inf --num-prompts 8` 压测，记录基线吞吐与 p99 延迟，存到 `baseline.json`。

2. **剖（4.5）**：重启服务并带上 `--profiler-config`，用 `/start_profile` + 一个生成请求 + `/stop_profile` 抓 trace。在 Perfetto 里找出 `diffuse` 循环中占比最高的算子类别（attention? matmul? vae?）。

3. **评（4.3）**：
   - 若瓶颈是 attention，跑 `bench_attention_backends.py --preset flux`，看你这台机器上哪个后端最快，再用 `DIFFUSION_ATTENTION_BACKEND` 环境变量切换重测端到端。
   - 若考虑量化提速，用 `quantization_quality.py --quantization fp8` 算 LPIPS，确认质量损失可接受。

4. **复盘**：把「基线 vs 切后端/量化后」的吞吐、p99、LPIPS 填一张三列表，写出一句结论（如「切到 FLASH_ATTN 后吞吐 +X%，LPIPS 不变」）。

这个闭环演示了 vLLM-Omni 基准与剖析工具的协作方式：**压测定方向、profiler 定位置、后端/质量评测验方案**。

## 6. 本讲小结

- vLLM-Omni 的性能工具遵循「复用上游 + patch 扩展」哲学：`diffusion_benchmark_serving.py` 是自包含的 diffusion 压测脚本；通用在线压测则靠 `serve.py` + `patch.py` 把 omni 数据集与多模态后端「注射」进上游 vLLM 基准。
- 压测三要素是「数据集（请求从哪来）、到达过程（Poisson / 全量）、并发上限（Semaphore）」；`--request-rate` 与 `--max-concurrency` 共同决定实际负载，混淆二者会得到无意义的吞吐数。
- patch.py 用双写 `datasets.get_samples` 与 `serve.get_samples`、注册 `ASYNC_REQUEST_FUNCS`、替换 `serve.benchmark` 三招扩展上游，并用继承自 `RequestFuncOutput` 的 `MixRequestFuncOutput` 承载多模态指标。
- `bench_attention_backends.py` 用合成 Q/K/V 在固定形状下对比 SDPA/FlashInfer/FA4 的单 kernel 中位耗时，定位「自动路由里该关掉的慢后端」；`quantization_quality.py` 用 LPIPS 量化感知质量损失。
- `audio_continuity.py` 用朴素的「实时播放器模拟」捕捉 RTF 合格但块到达抖动导致的 audible underrun，补齐了 TTS 流式体验的最后一环。
- profiling.md 提供 torch/cuda 两套剖析后端与 `/start_profile`/`/stop_profile` 在线控制，并用一张表划清了 pipeline profiler / AR profiler / torch profiler / orchestrator monitor / Prometheus 各自的适用边界——选错工具会看不到瓶颈。

## 7. 下一步学习建议

- **回到加速主线**：本讲的「后端对比」与「量化评测」是 U7（[u7-l1](u7-l1-attention-backends.md)、[u7-l3](u7-l3-cache-acceleration.md)、[u7-l5](u7-l5-diffusion-batching.md)）的测量支撑。学完本讲后，建议重读 u7-l1 的「平台默认四级降级」，结合 `bench_attention_backends.py` 的输出去理解「为什么需要降级」。
- **多阶段剖析**：若你要测的是 Qwen3-Omni 这类多阶段模型，重点读 profiling.md 的 orchestrator monitor 段，并配合 [u3-l2](u3-l2-orchestrator.md) 理解「编排线程饱和」这个多阶段独有的瓶颈。
- **贡献基准**：若你要给 PR 附性能数据，参考 `benchmarks/diffusion/performance_dashboard/` 下的两份现成报告（qwen_image / wan_2_2），它们是「压测 + 评测 + 表格化」的标准范式。
- **继续 U8**：下一讲无（u8-l3 是 U8 末讲）。U8 的 [u8-l1](u8-l1-quantization.md) 量化与 [u8-l2](u8-l2-platforms.md) 平台抽象与本讲的量化评测、注意力后端评测紧密相关，建议交叉阅读。
