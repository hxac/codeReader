# 多后端评测运行器与指标

## 1. 本讲目标

本讲是评测模块（u3-l4 的续篇）的核心实现部分。上一讲我们看清了 `dflash/benchmark.py` 的「骨架」——数据集配置表、缓存下载与 `main` 的 CLI 分发；本讲则要打开骨架末端的**三条后端运行器**与**指标计算**，搞清楚 DFlash 到底怎么把「跑一遍数据集」变成「加速比 / 吞吐 / 接受长度」这些数字。

学完后你应当能够：

1. 看懂 Transformers 后端如何用 `torchrun` 多卡分布式评测：`_dist_*` 工具函数读环境变量、按 rank **跨步分片**数据、最后 `_dist_gather` 把结果汇总到 0 号卡。
2. 看懂 server 后端（vLLM / SGLang）如何用 `ThreadPoolExecutor` 做**并发 HTTP 评测**：warmup、并发提交、按后端聚合吞吐与接受长度。
3. 理解 MLX 后端如何复用同一套指标容器把流式生成结果**归一化**。
4. 手算并解释**加速比、平均接受长度、接受长度直方图**这三个指标的计算公式，并明白「库型后端」与「服务型后端」汇报的指标为何不同。

## 2. 前置知识

阅读本讲前，请确保你已经掌握：

- **投机解码的接受长度**（见 u2-l1 / u2-l4）：草稿每轮起草一块、target 验证后取最长公共前缀，命中数记为 `acceptance_length`，每轮实际产出 `acceptance_length + 1` 个 token（那个 `+1` 是 target 的兜底 token，永不丢失）。本讲里的「接受长度」一律指**含兜底 token 的值**，取值范围为 \([1, B]\)（\(B\) = `block_size`）。
- **`block_size=1` 即 baseline**（见 u2-l1）：当 `block_size=1` 时 DFlash 退化为纯 target 自回归，没有投机。这就是评测里「加速比」的分母来源。
- **数据集缓存与 CLI 入口**（见 u3-l4）：`main` 按 `--backend` 把请求路由到 `_run_transformers` / `_run_mlx` / `_run_server`；数据集已缓存为 `cache/<name>.jsonl`，每行是 `{"turns": [...]}`。

几个本讲会用到的工程概念：

- **TPOT（time per output token，每 token 生成耗时）**：生成阶段总耗时除以输出 token 数。TPOT 越小越快；吞吐 throughput（tok/s）= \(1 / \text{TPOT}\)。
- **`torchrun` 与 NCCL**：PyTorch 官方的多进程启动器，会为每个进程注入 `RANK` / `WORLD_SIZE` / `LOCAL_RANK` 等环境变量；NCCL 是 GPU 间通信后端。这两个名字在本讲会反复出现。
- **`ThreadPoolExecutor`**：Python 标准库的线程池，用来并发发送 HTTP 请求——这是 server 后端「压测」的关键。

## 3. 本讲源码地图

本讲几乎全部内容集中在一个文件：

| 文件 | 作用 |
| --- | --- |
| [dflash/benchmark.py](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py) | 评测框架，本讲聚焦其中的三条运行器与指标函数 |

本讲涉及的关键函数清单（按出场顺序）：

- `_make_decode_metrics` / `_print_decode_summary`：**统一的指标容器**与**汇总打印**（加速比 + 直方图）。
- `_dist_*` 一组工具函数：读 `torchrun` 注入的环境变量。
- `_run_transformers`：Transformers 多卡分布式运行器。
- `_send_vllm` / `_send_sglang`：两种服务端的 HTTP 请求封装。
- `_run_server`：服务端并发压测运行器。
- `_run_mlx`：MLX 单进程运行器。

> 全文永久链接以 `94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756` 为 base。下面引用的行号均对应该 HEAD。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：4.1 Transformers 分布式运行器、4.2 Server 并发 HTTP 运行器、4.3 MLX 本地运行器、4.4 指标汇总。

### 4.1 Transformers 分布式运行器与 `_dist_*` 工具函数

#### 4.1.1 概念说明

Transformers 后端把模型**直接装进当前 Python 进程**里跑（没有 HTTP 服务）。要在 8 张卡上评测，最自然的办法是「起 8 个进程、每个进程绑一张卡、各自跑一部分数据」。DFlash 用 PyTorch 官方的 `torchrun` 来起这 8 个进程。

`torchrun` 启动时会给每个进程注入一组环境变量：

- `RANK`：进程的全局编号（0 到 `WORLD_SIZE-1`）。
- `WORLD_SIZE`：进程总数。
- `LOCAL_RANK`：本机内的编号（决定绑哪张卡）。

`_dist_*` 就是一组**读这些环境变量的薄封装**。这样写有两个好处：单进程跑（不用 `torchrun`）时它们也能优雅降级，代码不分支；测试时可以手动设环境变量来模拟多卡。

数据分片采用**跨步（strided）划分**而非连续切块：rank `r` 处理下标为 `r, r+W, r+2W, …` 的样本（\(W\) = `WORLD_SIZE`）。这样各 rank 拿到的样本在长度/难度上更均匀，避免某张卡分到一串长样本成为短板。

#### 4.1.2 核心流程

`_run_transformers` 的执行流程：

```
_check_transformers_model(model)        # 白名单校验（仅 Qwen3 / LLaMA-3.1-8B）
设置随机种子（random / np / torch / cuda）
_dist_init(torch_dist)                  # 初始化 NCCL 进程组（单进程则跳过）
torch.cuda.set_device(LOCAL_RANK)       # 绑定本进程的 GPU
加载 target (AutoModelForCausalLM) 与 draft (DFlashDraftModel)
读取 dataset，按 rank 跨步分片: indices = range(rank, len, size)
for 每个样本:
    for bs in [1, block_size]:          # 同一个样本跑两遍：baseline(bs=1) 与 DFlash
        response[bs] = dflash_generate(..., block_size=bs, return_stats=True)
    responses.append(response)
if 多卡: _dist_gather 把所有 rank 的 responses 汇总到 rank 0
_print_decode_summary(responses, block_size)   # 只有 rank 0 打印
```

注意第 6 步的「A/B 对照」：每个样本既跑 `bs=1`（baseline）又跑 `bs=block_size`（DFlash），结果分别存进 `response[1]` 和 `response[block_size]`。**正因为同一批样本同时跑了 baseline 和 DFlash，加速比才能在「同一次运行内」算出来**——这一点和 server 后端有本质区别（见 4.2）。

#### 4.1.3 源码精读

`_dist_init` 判断是否在 `torchrun` 环境下，是则用 NCCL 初始化进程组：

[dflash/benchmark.py:139-143](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L139-L143) —— 读 `RANK` 是否存在；存在才 `init_process_group(backend="nccl", init_method="env://")`，否则告警跳过（单进程降级）。

其余 `_dist_*` 都是一行的环境变量读取：[benchmark.py:146-159](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L146-L159)（`_dist_size`→`WORLD_SIZE`、`_dist_rank`→`RANK`、`_dist_local_rank`→`LOCAL_RANK`、`_dist_is_main`→`rank==0`）。

`_dist_gather` 把各进程的结果对象汇总到 0 号进程：

[dflash/benchmark.py:162-170](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L162-L170) —— 主进程收到长度为 `WORLD_SIZE` 的列表，非主进程返回 `None`；未初始化分布式时直接返回 `[obj]`（单元素），让调用方代码无需分支。

数据跨步分片就一行：

```python
indices = range(_dist_rank(), len(dataset), _dist_size())
```

[dflash/benchmark.py:234-235](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L234-L235) —— rank 0 跑 `0,8,16,…`、rank 3 跑 `3,11,19,…`（以 8 卡为例）；`tqdm` 只在主进程显示进度条。

每个样本的 A/B 对照循环：

[dflash/benchmark.py:243-254](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L243-L254) —— `for bs in [1, block_size]`，两次都带 `return_stats=True`，把含 `time_per_output_token` / `acceptance_lengths` 的 `SimpleNamespace` 存进 `response[bs]`。

最后的多卡汇总：

[dflash/benchmark.py:262-268](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L262-L268) —— `_dist_size() > 1` 时 gather 到 rank 0，用 `chain(*responses)` 把「列表的列表」拍平成一份，非主进程直接 `return`，只有 rank 0 调用 `_print_decode_summary`。

#### 4.1.4 代码实践

**实践目标**：在不用真卡的情况下，验证跨步分片与 gather 的语义。

**操作步骤**：

1. 阅读上面的分片行，在纸上推演：`--nproc_per_node=8`、`len(dataset)=128` 时，rank 3 会处理哪些样本下标？总共处理多少个？（源码阅读型实践，无需运行）
2. 用一段最小 Python（不依赖 torch）模拟 `_dist_gather` 的单进程降级路径：

```python
# 示例代码：演示 _dist_gather 未初始化分布式时的返回
def fake_gather(is_init, obj, is_main, size):
    if not is_init:
        return [obj]          # 单进程：返回单元素列表
    if is_main:
        return [None] * size  # 主进程：收到所有 rank 的对象
    return None               # 非主进程：返回 None

print(fake_gather(False, [1,2,3], True, 1))   # 期望 [[1, 2, 3]]
```

3. 进阶（需 GPU + `torchrun`）：在 `_dist_init` 内部、`init_process_group` 之后加一行 `logger.info(f"rank={_dist_rank()} local={_dist_local_rank()} size={_dist_size()}")`，用 `torchrun --nproc_per_node=2 -m dflash.benchmark --backend transformers ...` 跑一次，观察每张卡打印的编号。

**需要观察的现象 / 预期结果**：

- rank 3 处理下标 `3, 11, 19, …, 123`，共 16 个样本。
- 示例代码输出 `[[1, 2, 3]]`（单进程降级返回单元素列表）。
- 第 3 步真实运行的编号「待本地验证」（取决于你的硬件）。

#### 4.1.5 小练习与答案

**练习 1**：为什么用 `range(rank, len, size)` 跨步分片，而不是把数据切成 `rank*chunk : (rank+1)*chunk` 的连续块？
**答**：样本长度差异大时，连续切块容易让某张卡分到一串长样本成为短板；跨步分片让每个 rank 拿到的样本在整体分布上更均匀，负载更平衡。

**练习 2**：在 4 卡评测中，rank 1 调用 `_dist_gather` 的返回值是什么？rank 0 呢？
**答**：rank 1（非主进程）返回 `None`；rank 0（主进程）返回长度为 4 的列表，依次是 4 个进程各自收集的 `responses`。

---

### 4.2 Server 并发 HTTP 运行器：`_run_server` / `_send_vllm` / `_send_sglang`

#### 4.2.1 概念说明

vLLM 和 SGLang 都是**服务型后端**：它们自己起一个 HTTP 服务、独占 GPU 和推理引擎，DFlash 的加速能力被烧进服务端（通过 `--speculative-config` / `--speculative-algorithm` 配置，见 u1-l2）。于是评测脚本的角色从「跑模型」变成了「**当客户端发请求压测**」。

这带来两个与 Transformers 后端完全不同的设计：

1. **评测的是吞吐（throughput），不是单条加速比。** 服务端天然支持批处理（continuous batching），并发请求越多、引擎越能凑批，单条 TPOT 的意义让位于「整体 tok/s」。所以 `_run_server` **不调用 `_print_decode_summary`**，也不在同一进程里跑 baseline——它只压测当前的（开启了 DFlash 的）服务配置。
2. **两种服务的 API 风格不同**，要分两个发送函数：
   - **vLLM** 走 OpenAI 兼容的 `/v1/chat/completions`，把原始用户消息交给服务端，**服务端负责套 chat template**。
   - **SGLang** 走 `/generate`，接收的是**已经套好模板的纯文本**，客户端（评测脚本）要自己先 `apply_chat_template`。

投机相关指标也只在 SGLang 的响应 `meta_info` 里暴露（`spec_accept_length`、`spec_verify_ct`）；vLLM 的 OpenAI 响应只给 `usage.completion_tokens`，拿不到逐请求的接受长度。

#### 4.2.2 核心流程

```
准备 prompts（num_prompts + concurrency 条，循环复用数据集）
  - vLLM: 存原始用户文本
  - SGLang: 先 apply_chat_template 再存
SGLang: GET /flush_cache 清空服务端 KV 缓存
warmup: 用 ThreadPoolExecutor 发 concurrency 条请求（不计入计时），然后从 prompts 里剔除
start = perf_counter()
ThreadPoolExecutor(concurrency): 并发提交剩余 prompts
as_completed 收集结果，累加 total_tokens（及 SGLang 的 spec_accept_length / spec_verify_ct）
latency = perf_counter() - start
throughput = total_tokens / latency
打印 Backend / Dataset / Concurrency / Latency / Output tokens / Throughput / Accept length / Spec verify ct
```

两个关键设计：**warmup** 先发 `concurrency` 条请求让引擎编译好 kernel、填好缓存，避免冷启动污染计时；**多造 `concurrency` 条 prompt**（`num_prompts = args.num_prompts + args.concurrency`）正是为了 warmup 用掉这些之后，还能剩 `args.num_prompts` 条参与正式计时。

#### 4.2.3 源码精读

`_send_vllm` 向 vLLM 的 OpenAI 接口发请求：

[dflash/benchmark.py:299-326](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L299-L326) —— body 里带 `model` / `messages` / `max_tokens` / 采样参数，以及 `chat_template_kwargs.enable_thinking`；发到 `/v1/chat/completions`。

`_send_sglang` 走 SGLang 的 `/generate`：

[dflash/benchmark.py:271-296](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L271-L296) —— body 是 `{"text": ..., "sampling_params": {...}}`，注意这里传的是**文本**而非 messages，对应 4.2.1 说的「SGLang 客户端要自己套模板」。

prompt 构造与多造 warmup 额度：

[dflash/benchmark.py:389-401](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L389-L401) —— `num_prompts = args.num_prompts + args.concurrency`；按 `i % len(dataset)` 循环复用数据集（样本不够时回绕）；vLLM 存原文、SGLang 调 `_apply_chat_template` 后存。

warmup 段（先于计时）：

[dflash/benchmark.py:432-437](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L432-L437) —— `bs = max(concurrency, 1)`，用线程池发 `bs` 条请求热身，然后 `prompts = prompts[bs:]` 把热身请求剔除，保证正式计时仍是 `num_prompts` 条。

计时与并发收集：

```python
with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
    futures = {pool.submit(send_one, p): i for i, p in enumerate(prompts)}
    for fut in tqdm(as_completed(futures), total=len(prompts), ...):
        out = fut.result()
        ...  # vLLM: 取 usage.completion_tokens；SGLang: 还取 spec_verify_ct / spec_accept_length
```

[dflash/benchmark.py:440-463](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L440-L463) —— `as_completed` 谁先回谁先收，逐条累加 token；`throughput = total_tokens / max(latency, 1e-6)`（`max(..., 1e-6)` 防止除零）。

结果打印（注意：**不走** `_print_decode_summary`，是自己的格式）：

[dflash/benchmark.py:465-477](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L465-L477) —— 只有 `spec_accept_lengths` 非空（SGLang）才打印 `Accept length`，只有 `spec_verify_ct_sum > 0` 才打印 `Spec verify ct`；vLLM 这两行都不会出现。

#### 4.2.4 代码实践

**实践目标**：看清两种发送函数的请求体差异（无需真服务，纯源码对照）。

**操作步骤**：

1. 打开 `_send_vllm` 与 `_send_sglang`，在笔记里列一张对比表：请求路径、请求体字段、是否带 `model`、是否带 `messages`、`enable_thinking` 在哪里传。
2. 思考：为什么 `_run_server` 里 SGLang 分支需要 `AutoTokenizer.from_pretrained(args.model)` 而 vLLM 分支不需要？（提示：谁负责套 chat template）
3. 进阶（需 GPU + 服务）：见本讲第 5 节「综合实践」。

**需要观察的现象 / 预期结果**：

- vLLM：`/v1/chat/completions`，body 含 `model`/`messages`，`enable_thinking` 在 `chat_template_kwargs` 里；服务端套模板。
- SGLang：`/generate`，body 含 `text`/`sampling_params`，无 `model`、无 `messages`；客户端必须先套模板，所以需要 tokenizer。
- 第 3 步的真实数字「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_run_server` 要先 warmup 再计时？
**答**：首批请求会触发 kernel 编译、缓存填充等一次性冷启动开销，若计入会系统性低估吞吐。先发 `concurrency` 条请求热身并剔除，让正式计时的测量更接近稳态。

**练习 2**：用 vLLM 后端跑完评测后，输出里为什么**没有** `Accept length` 这一行？
**答**：vLLM 的 OpenAI 兼容响应里只有 `usage.completion_tokens`，不暴露逐请求的投机接受长度；代码只在 SGLang 的 `meta_info.spec_accept_length` 里收集该值，因此 `spec_accept_lengths` 为空、跳过打印。

---

### 4.3 MLX 本地运行器：`_run_mlx`

#### 4.3.1 概念说明

MLX 后端面向 Apple 芯片、单进程、无分布式，最接近「在笔记本上本地试一下」。它的角色和 Transformers 类似（库型后端，直接 `import` 调用），但 baseline 不再是「DFlash 把 `bs` 设成 1」，而是直接调 **`mlx_lm` 原生的 `stream_generate`**——即未做任何改动的官方流式生成，作为最干净的对照基线。

和 Transformers 后端一样，MLX 后端对每个样本**同时跑 baseline 与 DFlash**，结果归一化进同一个指标容器，最终也调用 `_print_decode_summary` 输出加速比与直方图。

#### 4.3.2 核心流程

```
load target (mlx_lm.load) + draft (load_draft)
warmup：用 "Hi" 跑 baseline 与 DFlash 各一次（编译 Metal kernel）
for 每个样本:
    baseline: 流式收 tokens，记录 len(tokens) 与 generation_tps
        response[1] = _make_decode_metrics(len, tps, [1])     # baseline 无投机，占位 [1]
    DFlash:   流式收 tokens，逐轮收集 accepted
        response[block_size] = _make_decode_metrics(len, tps, accs)
_print_decode_summary(responses, block_size)
```

#### 4.3.3 源码精读

warmup 两行（baseline + DFlash 各一次）：

[dflash/benchmark.py:346-348](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L346-L348) —— 用 `"Hi"` 这个短 prompt 各跑 3 个 token，目的是让 Metal kernel 完成首次编译。

baseline 分支（用 `mlx_lm` 原生流式）：

[dflash/benchmark.py:360-364](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L360-L364) —— `tokens_bl` 收 token、`tps_bl` 取每个响应里的 `generation_tps`（流式过程中持续刷新，取末值）；baseline 没有投机，`acceptance_lengths` 传 `[1]` 作占位（直方图只读 `r[block_size]`，这个占位不影响结果）。

DFlash 分支（用 `dflash.model_mlx.stream_generate`）：

[dflash/benchmark.py:366-371](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L366-L371) —— `tokens_df.extend(r.tokens)`、`accs.append(r.accepted)`、`tps_df = r.generation_tps`；`r.accepted` 在 MLX 实现里被赋为 `accepted + 1`（含兜底 token，见 [model_mlx.py:534](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L534)），与 Transformers 的 `acceptance_length + 1`（[model.py:140](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L140)）口径一致，所以两条库型后端能共用同一个直方图函数。

#### 4.3.4 代码实践

**实践目标**：确认「两条库型后端的 `acceptance_lengths` 口径一致」这件事。

**操作步骤**：

1. 对照 [model.py:140](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L140)（Transformers 记 `acceptance_length + 1`）与 [model_mlx.py:534](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L534)（MLX 记 `accepted + 1`），在笔记里写下：两者的取值范围都是 \([1, B]\)，因而 4.4 的直方图桶 `range(block_size + 1)` 对两者都成立。
2. 想清楚：为什么 MLX 的 baseline 占位用 `[1]` 而不是 `[]`？

**需要观察的现象 / 预期结果**：两种实现都「含兜底 token」，范围 \([1, B]\) 一致；占位 `[1]` 是为了构造一个合法的 `acceptance_lengths` 列表（避免空列表在后续 `np.mean` 时报错），且其值不会被直方图读取。

#### 4.3.5 小练习与答案

**练习 1**：MLX 后端的 baseline 用的是 `block_size=1` 的 DFlash 吗？
**答**：不是。MLX baseline 直接用 `mlx_lm` 原生的 `stream_generate`（未经 DFlash 改造的官方流式生成），比「DFlash 设 `bs=1`」更干净，作为对照基线更可信。

**练习 2**：为什么 MLX 后端需要 warmup，而 `_run_server` 也有 warmup，两者的目的相同吗？
**答**：目的本质相同——都是排除首次调用的编译/填充开销。MLX warmup 是为了编译 Metal kernel；server warmup 是为了编译 CUDA kernel 并填缓存。区别在 MLX 是单条预热，server 是按 `concurrency` 并发预热。

---

### 4.4 指标汇总：`_make_decode_metrics` 与 `_print_decode_summary`

#### 4.4.1 概念说明

三条运行器的产出**形态各异**：Transformers 的 `dflash_generate` 返回一个 `SimpleNamespace`，MLX 的流式响应是 `GenerationResponse` 字段，server 则是 HTTP 返回的 dict。要统一汇报，需要一个**归一化容器**——这就是 `_make_decode_metrics` 的职责：把「输出 token 数 / 生成 tps / 接受长度列表」打包成一个带 `time_per_output_token` 字段的 `SimpleNamespace`。

汇总打印 `_print_decode_summary` 只服务**库型后端**（Transformers / MLX），因为只有它们在单次运行里同时跑了 baseline 与 DFlash，能算出加速比。它输出三组数字：

1. **吞吐与加速比**：由 baseline 与 DFlash 各自的平均 TPOT 算出。
2. **平均接受长度**：所有 decode 轮次的接受长度（含兜底）的均值。
3. **接受长度直方图**：每个接受值 \(b\) 出现的频率。

#### 4.4.2 核心流程与数学

**归一化容器** `_make_decode_metrics`：

\[
\text{TPOT} = \frac{1}{\text{generation\_tps}} = \frac{\text{decode 耗时}}{\text{输出 token 数}}
\]

直接把流式生成里现成的 `generation_tps`（tok/s）取倒数得到 TPOT（秒/token），省去再计时一次。

**加速比** `_print_decode_summary`：

\[
\text{Speedup} = \frac{\overline{\text{TPOT}}_{\text{baseline}}}{\overline{\text{TPOT}}_{\text{DFlash}}}
\quad,\quad
\text{Throughput}_x = \frac{1}{\overline{\text{TPOT}}_x}
\]

DFlash 把多步生成压进更少的前向，TPOT 更小，所以 Speedup > 1。

**平均接受长度**：

\[
\bar a = \frac{1}{|\mathcal{R}|}\sum_{i \in \mathcal{R}} \overline{a}_i
\]

其中 \(\overline{a}_i\) 是第 \(i\) 个样本各轮接受长度的均值，\(\mathcal{R}\) 是所有样本。注意代码先对每个样本求均值、再对样本求均值（两次平均）。

**接受长度直方图**（关键公式）：

\[
H(b) = \frac{\#\{\,a_i = b\,\}}{N}, \quad b \in \{0, 1, \ldots, B\}
\]

其中 \(N\) 是所有样本所有轮次的总数，\(a_i \in [1, B]\)。由于接受长度最小是 1（含兜底 token），\(H(0)\) 恒为 0%；桶的右端 \(B\) = `block_size`。直方图能直观看出「草稿命中率分布」——峰值越靠右，草稿越准、加速越好。

#### 4.4.3 源码精读

归一化容器：

[dflash/benchmark.py:112-117](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L112-L117) —— `time_per_output_token = 1.0 / generation_tps`（防 `generation_tps==0` 时返回 `inf`），外加 `num_output_tokens` 与 `acceptance_lengths`。

汇总打印：

[dflash/benchmark.py:120-132](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L120-L132) —— `baseline_tpot` 取 `r[1].time_per_output_token`，`dflash_tpot` 取 `r[block_size].time_per_output_token`，加速比 = 两者之比；直方图用 `chain.from_iterable` 把所有样本的接受长度拍平成一条大列表，再对 `range(block_size + 1)` 逐桶 `.count(b) / len` 求频率。

逐行对应：

```python
baseline_tpot = np.mean([r[1].time_per_output_token for r in responses])          # baseline 平均 TPOT
dflash_tpot   = np.mean([r[block_size].time_per_output_token for r in responses]) # DFlash 平均 TPOT
print(f"Decoding speedup: {baseline_tpot / dflash_tpot:.2f}")                      # 加速比
...
acceptance_lengths = list(chain.from_iterable(r[block_size].acceptance_lengths for r in responses))
histogram = [acceptance_lengths.count(b) / len(acceptance_lengths) for b in range(block_size + 1)]
```

#### 4.4.4 代码实践

**实践目标**：用一个伪造的 `responses` 直接验证指标公式（无需任何模型）。

**操作步骤**：

```python
# 示例代码：用伪造数据验证 _print_decode_summary 的计算（不依赖 dflash 包）
from types import SimpleNamespace as NS
from itertools import chain
import numpy as np

def make(n, tps, accs):  # 复刻 _make_decode_metrics
    return NS(num_output_tokens=n,
              time_per_output_token=1.0/tps,
              acceptance_lengths=accs)

# 假设 block_size=4，3 个样本：DFlash 每轮接受长度见下，baseline 慢(tps=50)
responses = [
    {1: make(40, 50, [1]), 4: make(40, 120, [4, 3, 2])},
    {1: make(40, 50, [1]), 4: make(40, 120, [3, 4, 1])},
    {1: make(40, 50, [1]), 4: make(40, 120, [2, 4, 4])},
]
block_size = 4

baseline_tpot = np.mean([r[1].time_per_output_token for r in responses])   # 1/50
dflash_tpot   = np.mean([r[block_size].time_per_output_token for r in responses])  # 1/120
print("speedup =", baseline_tpot / dflash_tpot)                            # 期望 2.4
accs = list(chain.from_iterable(r[block_size].acceptance_lengths for r in responses))
print("histogram =", [round(accs.count(b)/len(accs), 3) for b in range(block_size+1)])
```

**需要观察的现象 / 预期结果**：

- `speedup = 2.4`（因为 baseline tps=50、DFlash tps=120，\(120/50=2.4\)）。
- 直方图：接受长度列表 = `[4,3,2, 3,4,1, 2,4,4]`，\(b=1\) 占 \(1/9\)、\(b=2\) 占 \(2/9\)、\(b=3\) 占 \(2/9\)、\(b=4\) 占 \(4/9\)、\(b=0\) 占 0。
- 这几个数字你可以手算验证，是确定性的。

#### 4.4.5 小练习与答案

**练习 1**：加速比公式为什么是「baseline TPOT / DFlash TPOT」，而不是「DFlash tps / baseline tps」？二者等价吗？
**答**：等价。TPOT = 1/tps，所以 \(\text{TPOT}_{\text{base}}/\text{TPOT}_{\text{df}} = (1/\text{tps}_{\text{base}})/(1/\text{tps}_{\text{df}}) = \text{tps}_{\text{df}}/\text{tps}_{\text{base}}\)。代码用 TPOT 之比，是因为 `dflash_generate` 直接返回的是 TPOT（见 [model.py:167](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L167)），顺手相除即可。

**练习 2**：直方图桶为什么是 `range(block_size + 1)`，即包含 0？而 \(H(0)\) 又为何总是 0%？
**答**：因为接受长度（含兜底 token）最小是 1，最大是 `block_size`，桶需要覆盖 \([0, B]\) 才完整对齐；最小值为 1 决定了 \(H(0)\) 恒为 0%。保留 0 号桶是为了让直方图的下标与「接受长度值」一一对应，方便阅读。

---

## 5. 综合实践

**任务**：用 server 后端（vLLM 或 SGLang）跑一次 gsm8k 评测，记录吞吐与接受长度，再调高并发重跑，结合投机解码的批处理特性解释吞吐变化。这是本讲的「贯穿任务」，把 4.2 的并发压测与 4.4 的指标理解串起来。

**操作步骤**：

1. 按 u1-l2 用 vLLM（或 SGLang）起一个开启了 DFlash 的服务（带 `--speculative-config` / `--speculative-algorithm`），确认服务健康。
2. 跑一次低并发基准（README 示例）：

   ```bash
   python -m dflash.benchmark --backend vllm \
       --base-url http://127.0.0.1:8000 --model Qwen/Qwen3.5-27B \
       --dataset gsm8k --num-prompts 128 --concurrency 1 --enable-thinking
   ```

   记下输出里的 `Throughput`（tok/s）。若用 SGLang，额外记下 `Accept length`。
3. 把 `--concurrency` 调高（如 8、16）重跑，保持 `--num-prompts` 不变，记下新的 `Throughput`。
4. （可选对照）关掉服务端的 speculative 配置后用相同并发重跑，得到「无 DFlash」吞吐，与第 3 步对比，估算服务端的实际加速比。

**需要观察的现象 / 预期结果**：

- 调高 `--concurrency` 后，`Throughput` 通常**显著上升**：因为服务端是 continuous batching，并发请求越多，引擎越容易把多个请求的 token 凑进同一批前向，GPU 利用率提升。
- 这与库型后端「单条加速比」是两套度量：server 后端吞吐的提升，同时来自「批处理」与「投机解码减少验证前向次数」两个叠加效应。
- 具体数字「待本地验证」（取决于你的 GPU、模型、`block_size` 与草稿命中率）。
- 如果你发现高并发下吞吐反而下降或抖动，可能的解释：草稿命中率低导致验证开销上升、显存压力增大、或调度排队——这正是「投机解码 + 大批」需要权衡之处。

> 说明：本任务需要 GPU 与已起好的 vLLM/SGLang 服务。若你当前没有这些环境，可改为「源码阅读型」完成——对照 4.2 的源码，在笔记里画出 `_run_server` 从「构造 prompts → warmup → 计时并发提交 → as_completed 收集 → 打印」的完整时序图，并标注每一步用了 `ThreadPoolExecutor` 的哪个能力。

## 6. 本讲小结

- **三条运行器各有定位**：`_run_transformers` 多卡分布式（`torchrun` + NCCL）、`_run_server` 服务端并发压测（`ThreadPoolExecutor`）、`_run_mlx` Apple 芯片单进程本地。
- **数据跨步分片** `range(rank, len, size)` 让多卡负载更均衡；`_dist_gather` 把各 rank 结果汇总到 0 号进程，非主进程返回 `None`。
- **库型 vs 服务型的指标差异是本讲核心**：库型（Transformers/MLX）在单次运行里同时跑 baseline(`bs=1` 或原生流式)与 DFlash，用 `_print_decode_summary` 报「加速比 + 接受长度直方图」；服务型只压测当前 DFlash 配置，报「吞吐 + 平均接受长度」，不报同次加速比。
- **`_make_decode_metrics` 是归一化容器**，把异构产出统一成带 `time_per_output_token` 的 `SimpleNamespace`；TPOT = 1/tps。
- **加速比** = \(\overline{\text{TPOT}}_{\text{base}} / \overline{\text{TPOT}}_{\text{df}}\)；**直方图** \(H(b)=\#\{a_i=b\}/N\)，\(b \in [0, B]\)，\(H(0)\) 恒为 0%。
- **两种发送函数对应两种 API 风格**：vLLM 的 `/v1/chat/completions`（服务端套模板，只暴露 token 数），SGLang 的 `/generate`（客户端套模板，暴露 `spec_accept_length` / `spec_verify_ct`）。

## 7. 下一步学习建议

到这里，你已经读完了 `dflash/benchmark.py` 的全部实现，也走完了「数据集管理 → CLI → 三条运行器 → 指标」的完整评测链路。后续建议：

1. **回头做一次端到端的小评测**：哪怕只在单卡上用 Transformers 后端跑 `--max-samples 16`，亲眼看到 `Decoding speedup` 与 `Acceptance length histogram` 两行输出，把本讲的公式与真实数字对上。
2. **把加速比与接受长度联系起来**：结合 u2-l4 的接受算法，思考「为什么平均接受长度 \(\bar a\) 越大、加速比越接近理论上界 \(\bar a + 1\)，又为什么实际总低于它」（提示：草稿前向本身也要花时间，见 u2-l1 的 `time_per_output_token` 排除首次草稿 prefill 的设计）。
3. **扩展评测**：参考 u3-l4 的 `DATASETS` 配置表，给一个新数据集加一条配置（`load_args` / `load_kwargs` / `format`），用 `_run_server` 跑通，验证数据集缓存机制与吞吐统计对你的新数据也成立。
4. **若你对服务端实现感兴趣**：DFlash 本仓库不含 vLLM/SGLang 的服务端代码，本讲的 `_run_server` 只是客户端。想深入「服务端如何把块扩散投机解码烧进 continuous batching」，需要去对应项目的仓库阅读其 speculative decoding 实现。
