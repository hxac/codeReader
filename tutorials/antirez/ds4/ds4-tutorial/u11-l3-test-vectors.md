# 测试向量与回归

## 1. 本讲目标

本讲讲清 ds4 如何用「测试向量（test vectors）」做推理正确性回归。学完后你应该能够：

- 区分**官方向量（official.vec）**与**本地 golden 向量（local-golden.vec）**各自的来源、信息量与能/不能发现什么问题。
- 读懂 `ds4_test` 这个 C runner 的测试分发机制，知道 `--logprob-vectors`、`--local-golden-vectors`、`--server`、`--metal-ssd-streaming-cache-pressure` 各跑什么。
- 理解为什么比对官方向量必须把 `DS4_METAL_PREFILL_CHUNK` 钉死在 2048、并关掉加速器快速路径——即「严格比对」的工程含义。

本讲承接 u4-l3（生成与采样）引入的 `ds4_session_top_logprobs` / `argmax` / `copy_logits` 这组只读 logprob 旁路 API，把它们放到回归测试的真实场景里。

## 2. 前置知识

- **logit 与 logprob**：模型在每一步对整个词表输出一组原始分数 `logit`；用 log-sum-exp 归一化后得到每个 token 的对数概率 `logprob`。`logprob=0` 表示概率为 1（完全确定），越负越不可能。ds4 的 `ds4_token_score` 同时持有这两个值（[ds4.h:49-53](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L49-L53)）。
- **贪婪解码（greedy / argmax）**：每步选 logit 最大的 token，温度为 0、无随机性，因此同一模型同一 prompt 的输出逐位可复现。官方向量与本地 golden 向量都基于贪婪解码。
- **top-k / top-logprobs 切片**：把词表按 logprob 降序取前 k 名。官方 DeepSeek API 暴露的是 `top_logprobs=20` 这一切片，**不是完整 logits**——这是本讲最关键的一句前提。
- **prefill chunk**（u6-l1）：长 prompt 被切成固定大小的块逐块填入 KV 缓存，chunk 边界同时是 KV checkpoint 推进点与 logit 写出点，因此 chunk 大小会改变 KV 状态与 logit 落地路径。
- **回归测试**：不是「跑分越高越好」，而是「改了代码后，同一组输入的输出是否与基线逐位一致」。本讲的向量就是这套基线。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tests/ds4_test.c` | C 测试 runner，约 2300 行，`#include` 了真实 `ds4_server.c`，承载所有 `--flag` 测试入口与向量比对逻辑。 |
| `tests/test-vectors/README.md` | 向量目录的说明书，讲清两类向量来源与各 `--flag` 用法。 |
| `tests/test-vectors/manifest.json` | 向量清单：5 个 case（3 short + 2 long），每个指向一个 prompt 文件与一个官方 JSON。 |
| `tests/test-vectors/fetch_official_vectors.py` | 用 API key 调官方 API 抓取响应，再压缩成 `official.vec`。 |
| `tests/test-vectors/official.vec` | 官方向量紧凑夹具，C 直接解析。 |
| `tests/test-vectors/local-golden.vec` | 本地 golden 向量，存真实 logit。 |
| `tests/test-vectors/official/*.official.json` | 官方 API 原始响应，留作审计依据。 |
| `README.md` | `Test Vectors` 节与 `Debugging Notes` 节给出运行命令与 `--dump-logprobs` 用法。 |
| `ds4.h` | 声明 `ds4_session_top_logprobs` / `argmax` / `copy_logits` 等只读旁路 API。 |

## 4. 核心概念与源码讲解

### 4.1 测试向量：官方向量与本地 golden 向量

#### 4.1.1 概念说明

「测试向量」就是一段固定 prompt 加上「模型在该 prompt 上贪婪解码时，每一步期望选中的 token、以及它的 top-k 概率分布」。把它当作回归基准：改了内核/量化/分词/注意力后，重跑同一 prompt，比对输出是否仍与基线一致。不一致就说明改动引入了数值漂移。

ds4 有两套向量，互补而非替代：

- **官方向量（`official.vec`）**：从 DeepSeek 官方 API 抓取。请求参数是 `deepseek-v4-flash`、greedy、thinking disabled、`top_logprobs=20`（见 [tests/test-vectors/README.md:1-6](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/test-vectors/README.md#L1-L6)）。它对齐的是「官方实现」，权威性最高。但官方 API **不暴露完整 logits**，只给 top-20 切片；更致命的是，在 greedy 模式下 API 把被选中 token 的 logprob 标成 `0.0`，其余 top-20 候选一律标成 `-9999.0`（哨兵值，意为「不告诉你真实值」）。所以官方向量本质只能验证「贪婪 token 序列是否一致」，几乎不携带分布信息。
- **本地 golden 向量（`local-golden.vec`）**：从一次「已知正常的 DS4 本地 Metal 运行」抓取，用 `--dump-logprobs` / `ds4_session_copy_logits` 拿到 top-64 的**真实 float logit 值**（见 [tests/test-vectors/local-golden.vec:1-9](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/test-vectors/local-golden.vec#L1-L9)）。它对齐的是「自己上一次正常状态」，非权威，但信息量大，能发现「贪婪 token 没变、但 logits 分布已损坏」的后端漂移。

一句话：**官方向量保证「我们对齐了官方」，本地 golden 向量保证「我们没悄悄坏掉分布」**。

#### 4.1.2 核心流程

**官方向量的生成与消费**：

1. `fetch_official_vectors.py` 用 `DEEPSEEK_API_KEY` 调官方 API，把每个 case 的原始响应存成 `official/<id>.official.json`（含 20 个 top_logprobs，selected 为 0.0、其余为 -9999.0）。
2. 同一脚本把 JSON 压缩成 `official.vec`：每条 `top` 行先丢弃 `lp <= -1000` 的哨兵项，所以 greedy 下每步只剩 1 个 token（被选中的，logprob 0.0）。
3. `ds4_test --logprob-vectors` 读 `official.vec`：对每个 case prefill prompt → 逐步调 `ds4_session_top_logprobs(20)` 与 `ds4_session_argmax` → 比对选中 token 的字节 → 比对留下的 top token 的 logprob 差是否 ≤ 4.0。

**本地 golden 向量的消费**：

1. 一次正常 Metal 运行，dump 出某 frontier（上下文前沿）处的 top-64 token id 与真实 logit。
2. `ds4_test --local-golden-vectors` 读 `local-golden.vec`：prefill 到 frontier → `ds4_session_copy_logits` 取全词表 logits → 本地算 top-k → 比对 top1 精确相等、top5/20/64 重叠数、top20 最大绝对 logit 差。

官方向量的 logprob 阈值数学：选中 token 官方 logprob 恒为 0，判定式为

\[
\left|\,\text{local\_lp}-0\,\right| \le 4.0 \quad\Longleftrightarrow\quad \text{local\_lp} \ge -4.0
\]

即本地算出的选中 token 概率不得低于 \(e^{-4}\approx 0.018\)。这只是一个「贪婪 token 在本地至少不能太离谱」的弱下限，**不是**分布比对。

#### 4.1.3 源码精读

官方 API 响应里 logprob 被掩码的证据——`short_reasoning_plain` 这一步，被选中的 `"16"` 字节为 `[49,54]`（hex `3136`），logprob `0.0`；其余 19 个候选（`"204"`、EOS、`"To"`、`"15"`…）全是 `-9999.0`：

- [tests/test-vectors/official/short_reasoning_plain.official.json:52-62](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/test-vectors/official/short_reasoning_plain.official.json#L52-L62) — 选中 token `logprob: 0.0`。
- [tests/test-vectors/official/short_reasoning_plain.official.json:64-73](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/test-vectors/official/short_reasoning_plain.official.json#L64-L73) — 第二名 `"204"` 的 `logprob: -9999.0`。

fetcher 把这些 -9999 过滤掉，所以 `official.vec` 每步 `ntop=1`：

- [tests/test-vectors/fetch_official_vectors.py:205-214](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/test-vectors/fetch_official_vectors.py#L205-L214) — `if lp <= -1000: continue` 丢弃哨兵，再写出 `step <i> <selected-hex> <top-count>` 与 `top <token-hex> <official-logprob>`。
- [tests/test-vectors/official.vec:1-7](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/test-vectors/official.vec#L1-L7) — 可见 `step 0 416461 1` / `top 416461 0`，每步只留 1 个 token、logprob 0。

对比本地 golden 向量，它存的是真实 logit：

- [tests/test-vectors/local-golden.vec:1-9](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/test-vectors/local-golden.vec#L1-L9) — `top 0 4371 36.5096703`、`top 1 523 18.6111526`…，64 个真实浮点 logit。

C 侧的比对逻辑——官方向量逐步取 top-20 与 argmax，比对字节与 4.0 阈值：

- [tests/ds4_test.c:846-908](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/ds4_test.c#L846-L908) — `test_logprob_vector_case`：第 868 行 `ds4_session_top_logprobs(session, scores, 20)`，第 869 行 `ds4_session_argmax`，第 870 行按字节比对选中 token，第 893 行 `fabsf(local_lp - step->top[t].logprob) > 4.0f` 判失败。

本地 golden 向量的比对逻辑——取全词表 logits，算 top-k，比对重叠与最大绝对差：

- [tests/ds4_test.c:1117-1196](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/ds4_test.c#L1117-L1196) — `test_local_golden_case_run`：第 1160 行 `ds4_session_copy_logits(session, cand_logits, vocab)`，第 1166-1168 行算 top5/20/64 重叠，第 1170 行算 top20 最大绝对 logit 差，第 1184-1188 行给出容差（top1 精确相等、top5≥4、top20≥15、top64≥40、max_abs≤8.0）。
- 第 1179-1183 行的注释明确写道：这套容差「意在捕捉**实质性的后端漂移**（错误 tiling、漏算、错误 dispatch），而非正常内核改动的微小浮点差异」。这正是本地 golden 向量存在的理由。

只读旁路 API 声明：

- [ds4.h:255-263](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L255-L263) — `ds4_session_argmax` / `ds4_session_top_logprobs` / `ds4_session_token_logprob` / `ds4_session_copy_logits` / `ds4_session_set_logits`，都不推进 checkpoint、不动 KV。

#### 4.1.4 代码实践

**实践目标**：亲手验证「官方 top-logprobs 切片被掩码、本地 golden 向量未被掩码」，从而解释为什么本地 golden 向量更能发现后端 logits 漂移。

**操作步骤**：

1. 读 [tests/test-vectors/README.md:1-16](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/test-vectors/README.md#L1-L16)，确认两类向量来源。
2. 打开 [tests/test-vectors/official/short_reasoning_plain.official.json](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/test-vectors/official/short_reasoning_plain.official.json)，找到 `steps[0].top_logprobs` 数组，数一下有几个条目的 `logprob` 是 `0.0`、有几个是 `-9999.0`。
3. 对照 [tests/test-vectors/official.vec:29-30](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/test-vectors/official.vec#L29-L30)，确认 `short_reasoning_plain` 这步在紧凑夹具里只剩 `step 0 3136 1` / `top 3136 0`（ntop=1）。
4. 打开 [tests/test-vectors/local-golden.vec:5-9](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/test-vectors/local-golden.vec#L5-L9)，确认它存了 64 个**各不相同**的真实 logit 值。
5. 读 [tests/ds4_test.c:876-898](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/ds4_test.c#L876-L898)（官方向量比对）与 [tests/ds4_test.c:1166-1188](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/ds4_test.c#L1166-L1188)（本地 golden 比对），对比两边各比对了什么。

**需要观察的现象**：

- 官方 JSON 里每步 20 个 top_logprobs 中，只有 1 个（被选中 token）logprob 是 `0.0`，其余 19 个全是 `-9999.0`。
- `official.vec` 里每步 `ntop=1`，唯一的 `top` 行 logprob 是 `0`。
- `local-golden.vec` 里 64 个 `top` 行每个都有不同的真实 logit（如 `36.5`、`18.6`、`18.58`…）。

**预期结果**：你能用一句话回答——官方切片被 API 掩码成「只有选中 token 有 0、其余 -9999」，所以 `official.vec` 只能比对贪婪 token 序列与一个弱概率下限；本地 golden 向量带 64 个真实 logit，能比对 top-k 重叠与 logit 数值漂移，因而能发现「贪婪 token 没变但分布已坏」的回归。

> 待本地验证：若你手头有 Mac + 模型，可跑 `./ds4 --dump-logprobs /tmp/out.json --logprobs-top-k 20 --temp 0 -p "Answer with only the number: 2048 divided by 128 is"`（参考 [README.md:1244-1246](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L1244-L1246)），观察本地 dump 出的 top-20 是真实 logit 而非全 0/-9999，与官方 JSON 形成对照。

#### 4.1.5 小练习与答案

**练习 1**：`official.vec` 里某步是 `step 2 0a 1` / `top 0a 0`。如果本地 ds4 这一步 argmax 出来的 token 字节正好是 `0a`，但它的本地 logprob 是 `-6.0`，`--logprob-vectors` 会报失败吗？为什么？

**答案**：会报失败。比对逻辑在 [tests/ds4_test.c:893](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/ds4_test.c#L893) 是 `fabsf(local_lp - step->top[t].logprob) > 4.0f`，官方向量里该 token logprob 是 `0`，`|(-6.0)-0|=6.0 > 4.0`，触发 `TEST_ASSERT(false)`。这说明官方向量虽不比对完整分布，但对「贪婪 token 在本地概率过低」仍有一个 -4 的下限保护。

**练习 2**：为什么 `local-golden.vec` 用 top1 精确相等 + top5/20/64 重叠数 + max_abs≤8.0 这一组**宽松**容差，而不是要求 64 个 logit 逐位相等？

**答案**：因为本地 golden 向量的目的是捕捉「实质性后端漂移」（错误 tiling、漏算、错误 dispatch），而非微小浮点差异（见 [tests/ds4_test.c:1179-1183](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/ds4_test.c#L1179-L1183) 的注释）。不同 Metal 内核版本、不同归约顺序都会带来小于 1 的 logit 抖动，逐位相等会误伤正常改动；而真正的 bug（比如漏算一层、用错 scale）会让 top-k 排名大幅错乱或 logit 偏移几十，宽松容差正好卡在两者之间。

### 4.2 C runner：ds4_test 的测试分发与引擎复用

#### 4.2.1 概念说明

`ds4_test` 是一个纯 C 的测试 runner，把若干测试用 `--flag` 暴露出来，`--all` 跑全部、`--list` 列名、`-h` 看帮助。它不是 mock 框架——文件开头直接 `#include "../ds4_server.c"`（带 `DS4_SERVER_TEST` 宏），把真实引擎、服务器、KV store 代码都拉进同一个翻译单元，测试调的就是产品代码本身。

引擎复用是它的一个重要设计：`test_get_engine(quality)` 懒加载两个引擎实例（`test_engine_fast` 非 quality、`test_engine_quality` quality 档），跨多个测试复用，进程结束时统一 `test_close_engines`。这样昂贵的模型加载只发生一次。

注意 `--server` 这一组是**唯一不依赖 GPU** 的测试（它不在 `#ifndef DS4_NO_GPU` 块内），跑的是服务器请求解析、聊天模板渲染、缓存逻辑的单元测试，因此在 CPU 构建（`make cpu`）上也能跑。

#### 4.2.2 核心流程

1. `main` 解析 argv：`--all`（或无参，默认全跑）、`--list`、`-h`/`--help`、或具体 flag。
2. `test_find_entry` 在 `test_entries[]` 表里查 flag，还兼容别名 `--metal-mpp-equivalence` → `--metal-tensor-equivalence`。
3. `test_run_entry` 调对应函数，用进入前后的 `test_failures` 计数差判断该测试 OK 还是 ERR，并打印。
4. 测试函数内部用 `TEST_ASSERT` 宏断言，失败累加 `test_failures` 但不立即退出（除非断言带提前 return）。
5. 向量测试通过 `test_open_engine(false)` 打开非 quality 引擎，跑完 `ds4_engine_close`。

#### 4.2.3 源码精读

- [tests/ds4_test.c:1-3](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/ds4_test.c#L1-L3) — `#define DS4_SERVER_TEST` 后 `#include "../ds4_server.c"`，把产品代码拉进测试单元。
- [tests/ds4_test.c:2192-2207](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/ds4_test.c#L2192-L2207) — `test_entries[]` 表：`--logprob-vectors`、`--metal-ssd-streaming-cache-pressure`、`--local-golden-vectors` 都在 `#ifndef DS4_NO_GPU` 块内，`--server` 在块外（CPU 也可跑）。
- [tests/ds4_test.c:2262-2308](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/ds4_test.c#L2262-L2308) — `main`：argv 分发，`run_all` 默认为 `argc==1`，最后按 `test_failures` 决定返回码（0 ok / 1 失败）。
- [tests/ds4_test.c:88-114](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/ds4_test.c#L88-L114) — `test_open_engine`：按 `__APPLE__` 选 Metal 否则 CUDA，`quality` 透传，SSD 流式相关字段从环境变量读。
- [tests/ds4_test.c:116-122](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/ds4_test.c#L116-L122) — `test_get_engine`：懒加载并缓存到 `test_engine_fast`/`test_engine_quality` 两个静态槽。
- [tests/ds4_test.c:41-59](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/ds4_test.c#L41-L59) — `test_save_env` / `test_restore_env`：临时改环境变量后再原样还原（含原本未设置的情况用 `unsetenv`），是「严格比对」能不污染外部环境的基础。
- [tests/ds4_test.c:2209-2236](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/ds4_test.c#L2209-L2236) — `test_print_help`：列出所有环境变量，包括 `DS4_TEST_VECTOR_FILE`、`DS4_TEST_LOCAL_GOLDEN_FILE`（可覆盖默认向量路径）。

#### 4.2.4 代码实践

**实践目标**：不依赖 GPU 也能跑起 `ds4_test` 的服务器单元测试，建立对 runner 的直觉。

**操作步骤**：

1. 读 [README.md:1233-1237](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L1233-L1237)，确认 `make test` 实际跑的是 `./ds4-eval --self-test-extractors && ./ds4_test --all`，且 `--logprob-vectors` 与 `--server` 可单独跑。
2. 读 [Makefile:175-176](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L175-L176) 与 [Makefile:220-224](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L220-L224)，确认 `ds4_test` 由 `ds4_test.o + ds4_help.o + ds4_kvstore.o + rax.o + CORE_OBJS` 链接而成。
3. 在 CPU 构建下跑 `./ds4_test --list`（列出所有 flag）与 `./ds4_test --server`（只跑服务器单元测试）。

**需要观察的现象**：`--list` 打印的表里，`--server` 之外的所有向量/GPU 测试在 `make cpu` 构建里会因 `DS4_NO_GPU` 而从表中消失；`--server` 始终在。

**预期结果**：`./ds4_test --server` 在没有 GPU 的机器上也能跑通并打印 `server: OK`，验证「服务器解析/渲染/缓存逻辑不依赖 GPU」。

> 待本地验证：上述 `--list`/`--server` 行为需在你本机构建后确认；若仅阅读源码，可从 `test_entries[]` 的 `#ifndef DS4_NO_GPU` 包裹直接推得同样结论。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ds4_test` 要 `#include "../ds4_server.c"` 而不是链接 `ds4_server.o`？

**答案**：因为测试需要访问 `ds4_server.c` 内部的 `static` 函数与文件内结构体（如请求解析、DSML 回放、缓存的内部细节），链接 `.o` 只能拿到对外符号。用 `#include` 源文件 + `DS4_SERVER_TEST` 宏（还配合 `DS4_SERVER_TEST_NO_MAIN` 避免与 `main` 冲突）把整个翻译单元拉进来，就能直接测内部函数，同时保证测的就是产品代码本身。

**练习 2**：`test_get_engine` 用两个静态槽 `test_engine_fast` / `test_engine_quality` 缓存引擎。如果一个测试改坏了引擎内部状态，会对后续测试造成什么影响？设计上如何缓解？

**答案**：会造成测试间耦合——后续复用同一槽的测试会基于被污染的引擎状态运行，可能误报或漏报。缓解方式是：测试尽量用只读旁路 API（如 `top_logprobs`/`copy_logits`/`argmax`，见 [ds4.h:255-263](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L255-L263)）或新建独立 session（`ds4_session_create`），session 是可变时间线、引擎基本只读，从而把「可变状态」隔离在 session 层而非引擎层。

### 4.3 严格比对：钉死 prefill chunk 与关闭快速路径

#### 4.3.1 概念说明

「严格比对」回答一个问题：凭什么同一份向量在不同机器、不同运行上能复现？答案是**把所有影响 KV 状态与 logit 数值的「旋钮」全部钉死**。

第一个旋钮是 **prefill chunk 大小**。如 u6-l1 所述，chunk 边界同时是 KV checkpoint 推进点、logit 写出点与磁盘冷存对齐点；不同 chunk 会让 compressor 压缩行的最终化时机不同，KV 状态与 logit 因此可能微差。README 明确说「改 chunk 会改 KV checkpoint/logit 路径，要把它当作显式运行配置来比对」（[README.md:569-574](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L569-L574)）。所以：
- 官方向量比对钉 `DS4_METAL_PREFILL_CHUNK=2048`（README 称之为「严格官方向量 checkpoint 路径」）。
- 本地 golden 向量比对钉 `4096`（因为它本就是在 4096 chunk 下抓取的已知正常状态）。

第二个旋钮是**加速器快速路径**。Metal 上有一套「metal4」优化路径与若干 SSD 流式快速路径（cold-decode、batch-selected-addr 等），它们为速度而生，可能引入与「标准路径」不同的数值细节。严格比对要关掉它们，让比对走规范路径：`DS4_METAL_DISABLE_METAL4=1`，外加 `test_force_canonical_streaming_prefill` 关掉流式快速路径。

第三个、也是最特殊的一个是 **cache-pressure 复现测试**（`--metal-ssd-streaming-cache-pressure`），它反其道而行：故意**打开** SSD 流式 + 16GiB 小缓存 + layer-batched decode，专门复现 GitHub issue #384——在命令缓冲尚未完成时，某层已引用的缓存项被后续层重用，产生**确定性错误 logits**。它跑的是官方向量里的 `short_code_completion` 这一个 case，因为该 case 正好能暴露这个 bug。

此外，`long_memory_archive` 这一个长上下文官方向量目前被显式跳过（[tests/ds4_test.c:910-919](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/ds4_test.c#L910-L919)），原因是加入官方 Hadamard+FP4 indexer 路径后该 case 与公开 API 一致性变差，DS4 在 A/B 比对中保留了 perplexity 更低的实现，仅排除这条脆弱的 API 夹具。

#### 4.3.2 核心流程

官方向量严格比对的环境钉死（`test_official_logprob_vectors_run`）：

1. `test_save_env` 保存当前 `DS4_METAL_PREFILL_CHUNK`、`DS4_METAL_DISABLE_METAL4` 与流式快速路径变量。
2. `setenv("DS4_METAL_PREFILL_CHUNK", "2048", 1)` 钉死 chunk。
3. 除非设了 `DS4_TEST_LOGPROB_AUTO_METAL`，否则 `setenv("DS4_METAL_DISABLE_METAL4", "1", 1)` 关掉 metal4 快速路径。
4. `test_force_canonical_streaming_prefill` 关掉 SSD 流式 cold-decode/batch-selected-addr 快速路径。
5. `test_open_engine(false)` 打开非 quality 引擎，逐 case 比对。
6. `test_restore_env` 把所有变量原样还原（含原本未设置的 `unsetenv`）。

cache-pressure 测试则把第 2-4 步换成「打开」流式与小缓存，再调同一个 `test_official_logprob_vectors_run("short_code_completion")` 只跑那一个 case。

#### 4.3.3 源码精读

- [tests/ds4_test.c:921-969](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/ds4_test.c#L921-L969) — `test_official_logprob_vectors_run`：第 928-929 行保存环境，第 932 行钉 `2048`，第 933-937 行按 `DS4_TEST_LOGPROB_AUTO_METAL` 决定是否关 metal4，第 938 行开非 quality 引擎，第 949-962 行逐 case 跑（含 `case_filter` 只跑指定 case），第 965-967 行还原环境。
- [tests/ds4_test.c:910-919](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/ds4_test.c#L910-L919) — `test_logprob_vector_case_disabled`：跳过 `long_memory_archive`，注释解释 API 与官方图在该长上下文上不一致。
- [tests/ds4_test.c:975-1022](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/ds4_test.c#L975-L1022) — `test_metal_ssd_streaming_cache_pressure`：第 981-988 行注释 issue #384 的根因（缓存项在命令缓冲完成前被重用 → 确定性错误 logits），第 1000-1001 行设 `DS4_TEST_SSD_STREAMING=1` 与 16GiB 缓存，第 1010 行调 `test_official_logprob_vectors_run("short_code_completion")`。
- [tests/ds4_test.c:1205-1212](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/ds4_test.c#L1205-L1212) — 本地 golden 向量钉 `DS4_METAL_PREFILL_CHUNK=4096` 与 `DS4_METAL_DISABLE_METAL4=1`。
- [tests/ds4_test.c:66-86](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/ds4_test.c#L66-L86) — `test_force_canonical_streaming_prefill` / `test_restore_canonical_streaming_prefill`：仅在 `DS4_TEST_SSD_STREAMING` 打开时才设禁用变量，否则保持默认。

#### 4.3.4 代码实践

**实践目标**：理解 chunk 大小如何改变 logit 落地路径，从而理解「钉死 chunk」的必要性。

**操作步骤**：

1. 读 [README.md:569-574](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L569-L574)，确认「改 chunk 会改 KV checkpoint/logit 路径」这一论断。
2. 读 [tests/ds4_test.c:928-932](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/ds4_test.c#L928-L932) 与 [tests/ds4_test.c:1205-1212](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/ds4_test.c#L1205-L1212)，对比官方（2048）与本地 golden（4096）各自钉的 chunk。
3. 设计一个思想实验：假设有人把 `test_official_logprob_vectors_run` 里的 `2048` 改成 `0`（整批 prefill），预测哪些 case 最可能先报失败，为什么。

**需要观察的现象**：长上下文 case（`long_memory_archive`、`long_code_audit`，prompt 约 1.8 万字符，见 [tests/test-vectors/manifest.json:34-48](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/test-vectors/manifest.json#L34-L48)）对 chunk 最敏感，因为它们 prefill 跨越多块、compressor 行最终化时机随 chunk 改变；短 case（`short_italian_fact` 等）通常单块 prefill 完成，chunk 变化影响小。

**预期结果**：你能解释「chunk 是路径旋钮而非性能旋钮」（u6-l1 结论）——它改变 KV checkpoint 推进点与 logit 写出点，所以复现官方向量必须钉死 2048，否则长 case 的 logit 会因 compressor 边界不同而漂移、触发 4.0 阈值失败。

> 待本地验证：若你有 Mac + 模型，可分别用 `DS4_METAL_PREFILL_CHUNK=2048` 与 `=0` 跑 `./ds4 --dump-logprobs /tmp/out.json --logprobs-top-k 20 --temp 0 --prompt-file tests/test-vectors/prompts/long_code_audit.txt`（参考 [README.md:1244-1246](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L1244-L1246) 与 [tests/test-vectors/README.md:59-64](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/test-vectors/README.md#L59-L64)），对比两次 dump 的 top-1 token 或其 logit 是否不同。

#### 4.3.5 小练习与答案

**练习 1**：`test_official_logprob_vectors_run` 在开头 `test_save_env`、结尾 `test_restore_env`。如果某个断言在中间失败并提前 return，环境变量会被还原吗？

**答案**：会。函数在「引擎打开失败」这条提前 return 路径上（[tests/ds4_test.c:939-945](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/ds4_test.c#L939-L945)）显式调了还原；但要注意 `TEST_ASSERT` 宏本身的提前 return 行为取决于宏定义——若断言失败只是累加 `test_failures` 而不 return，则函数会继续走到结尾的正常还原路径。设计上把还原写在所有出口上，是为了不让「测试改了环境变量」污染后续测试或外部 shell 环境。

**练习 2**：`--metal-ssd-streaming-cache-pressure` 为什么只跑 `short_code_completion` 一个 case，而不是跑全部官方向量？

**答案**：因为它是 issue #384 的**聚焦复现**（见 [tests/test-vectors/README.md:37-46](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/test-vectors/README.md#L37-L46) 与 [tests/ds4_test.c:981-988](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/ds4_test.c#L981-L988)）。该 bug 的触发条件是「layer-batched decode 在小缓存压力下、命令缓冲未完成时重用缓存项」，`short_code_completion` 这个 case 正好能暴露错误 logits；跑全部 case 既慢又无额外覆盖意义，所以用 `case_filter="short_code_completion"` 只跑它。`test_official_logprob_vectors_run` 末尾的 `TEST_ASSERT(!case_filter || !case_filter[0] || ran == 1)`（[tests/ds4_test.c:963](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/ds4_test.c#L963)）还会校验「指定了 filter 就必须正好跑了 1 个 case」，防止 filter 拼错时静默跳过。

## 5. 综合实践

**任务**：跟踪一次官方向量比对从「向量文件一行」到「断言判定」的完整数据流，把三个模块的知识串起来。

**背景**：`official.vec` 里有这样一行序列（`short_reasoning_plain`，prompt 是 `Answer with only the number: 2048 divided by 128 is`）：

```
case short_reasoning_plain 4096 1 tests/test-vectors/prompts/short_reasoning_plain.txt
step 0 3136 1
top 3136 0
end
```

**操作步骤**：

1. **向量来源（4.1）**：从 [tests/test-vectors/official/short_reasoning_plain.official.json:42-62](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/test-vectors/official/short_reasoning_plain.official.json#L42-L62) 找到 `steps[0]`，确认被选 token 字节是 `[49,54]`（即 `"16"`，hex `3136`）、logprob `0.0`，其余 19 个 top_logprobs 全是 `-9999.0`。再到 [tests/test-vectors/fetch_official_vectors.py:205-214](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/test-vectors/fetch_official_vectors.py#L205-L214) 解释 `-9999` 如何被 `if lp <= -1000: continue` 过滤掉，最终在 `official.vec` 里只剩 `top 3136 0`。
2. **C runner（4.2）**：从 [tests/ds4_test.c:2262-2286](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/ds4_test.c#L2262-L2286) 的 `main` 出发，画调用链：`--logprob-vectors` → `test_find_entry` → `test_run_entry` → `test_official_logprob_vectors` → `test_official_logprob_vectors_run(NULL)` → `test_read_vector_case`/`test_fill_vector_case` 解析夹具 → `test_logprob_vector_case`。
3. **严格比对（4.3）**：在 [tests/ds4_test.c:928-937](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/ds4_test.c#L928-L937) 标出环境钉死点（chunk=2048、disable metal4、canonical streaming prefill），说明它们在打开引擎**之前**生效，因此引擎的 prefill 图按这些参数构建。
4. **断言判定（4.1）**：在 [tests/ds4_test.c:846-908](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/tests/ds4_test.c#L846-L908) 标出三个判定点：第 863 行 `ds4_session_sync` prefill、第 868-869 行取 top-20 与 argmax、第 870 行字节比对（本地 argmax 出来的 token 字节必须等于 `3136` 即 `"16"`）、第 893 行 4.0 阈值（本地该 token logprob 必须 ≥ -4.0）。

**产出**：一张数据流图（文字版即可），标注每个数据变换点，并在图上用箭头标出「chunk=2048」「disable-metal4」在哪一步注入、「-9999 过滤」在哪一步发生、「4.0 阈值」在哪一步判定。

**预期结果**：你能复述完整链路——官方 API 响应（20 个 top_logprobs，19 个被掩码为 -9999）→ fetcher 过滤 → `official.vec`（每步 1 个 token、logprob 0）→ ds4_test 钉死 chunk/快速路径后 prefill → 取本地 argmax+top-20 → 字节比对 + 4.0 阈值 → `TEST_ASSERT`。这条链路同时体现了「测试向量（数据）」「C runner（调度）」「严格比对（环境钉死）」三个模块如何协作完成一次回归判定。

## 6. 本讲小结

- ds4 有两套互补的测试向量：**官方向量**对齐官方实现但被 API 掩码（每步只剩选中 token、logprob 0），只能验贪婪 token 序列；**本地 golden 向量**存 64 个真实 logit，能发现「贪婪 token 没变但分布已坏」的后端漂移。
- 官方向量的生成链路是 `fetch_official_vectors.py` 调 API 存 JSON、再过滤 `lp <= -1000` 的 -9999 哨兵压成 `official.vec`；本地 golden 向量由一次正常 Metal 运行用 `--dump-logprobs`/`copy_logits` 抓取。
- `ds4_test` 是 `#include` 真实 `ds4_server.c` 的 C runner，用 `--flag` 分发测试、懒加载复用 fast/quality 两个引擎实例；`--server` 是唯一不依赖 GPU 的测试组。
- 「严格比对」= 钉死 `DS4_METAL_PREFILL_CHUNK`（官方 2048 / 本地 golden 4096）+ 关掉 metal4 与流式快速路径 + 用 `test_save_env`/`test_restore_env` 不污染环境；chunk 是路径旋钮，改它就改 KV checkpoint/logit 落地点。
- `--metal-ssd-streaming-cache-pressure` 反向利用严格比对：故意打开 SSD 流式 + 16GiB 小缓存 + layer-batched decode，聚焦复现 issue #384 的确定性错误 logits。
- `long_memory_archive` 官方向量目前被显式跳过，因为加入 Hadamard+FP4 indexer 后它与公开 API 一致性变差，DS4 保留了 perplexity 更低的实现。

## 7. 下一步学习建议

- 阅读 u10-l4（能力评测 ds4-eval），对比「向量回归（逐位数值一致）」与「能力回归（逐题可见、分数稳定）」两种回归哲学的差异——前者防数值漂移，后者防能力塌方。
- 进入 u11-l4（贡献与 QA 工作流），把本讲的 `make test` / `--logprob-vectors` / `--local-golden-vectors` 放进发布前检查清单，理解它们如何与 Metal、SSD 流式、分布式、CUDA 四条命脉路径的保护要求挂钩。
- 若想深入数值层面，回头读 u3-l4（量化格式）与 u5-l2（Metal 内核），思考「错误 tiling、漏算、错误 dispatch」这类本讲反复提到的漂移根源，分别对应量化点积与 GPU 内核里的哪些具体环节。
