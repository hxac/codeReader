# 可观测性：Tracing、指标与 Profiling

## 1. 本讲目标

RL 后训练是一个「采样 → 训练 → 权重同步」反复循环的分布式系统，一次出问题可能是 SGLang 排队、可能是 prefill 慢、也可能是某个 rollout 组被动态过滤掉。如果没有可观测性工具，这类问题几乎无从下手。

本讲的目标是让你掌握 slime 的「三件套」可观测性工具链：

1. **Tracing（追踪）**：`slime/utils/trace_utils.py` 提供的 `TraceHandle` / `trace_span` 体系，把每个 `Sample` 的生成、奖励计算等阶段记录成带时间戳的 span。
2. **指标（Metrics）**：`slime/ray/rollout.py` 里的 `compute_metrics_from_samples`（配合 `slime/utils/metric_utils.py`），从一批样本聚合成 `rollout/`、`perf/`、`eval/` 前缀的标量，落到 wandb / TensorBoard。
3. **Profiling（剖析）**：`tools/analyze_profile.py`，把 SGLang decode worker 的 PyTorch profiler trace（`.trace.json.gz`）解读成 GPU 利用率、kernel 占比、CUDA Graph 停顿等诊断结论。

学完后你应当能回答：trace 事件是怎么写到 `Sample` 上的？指标从哪取数？profile 文件怎么生成、又怎么读？三者如何配合定位一次吞吐异常。

## 2. 前置知识

阅读本讲前，你需要先建立以下认知（对应前置讲义）：

- **三大对象与数据流**（u2-l3）：`rollout_manager`（`RolloutManager` 远程演员）负责采样并产出一批 `Sample`，`actor_model`/`critic_model` 负责训练。本讲的指标几乎都计算自 `RolloutManager.generate` 返回的那批样本。
- **Sample 数据结构**（u3-l1）：`Sample` 是贯穿闭环的核心载体，带 `tokens`、`loss_mask`、`reward`、`status`、`response_length` 等字段。本讲会用到 `effective_response_length`、`non_generation_time`，并揭示 `Sample` 上还动态挂着一个 `trace` 字段。
- **默认 rollout 流程**（u3-l2）：`generate_and_rm` / `generate_and_rm_group` 是单样本/整组生成函数，它们正是被 trace 装饰器包裹的地方。

几个本讲要用到的术语：

- **span（跨度）**：一段有起止时间的执行片段，比如一次「调 SGLang 生成」。来自分布式追踪（tracing）的标准概念。
- **event（事件）**：一个时间点的瞬时记录（无持续时间）。
- **carrier（载体）**：slime 里把一条 trace 的全部事件存在一个普通 `dict` 里，叫 carrier，它被动态挂到 `Sample.trace` 上。
- **contextvars（上下文变量）**：Python 标准库，让每个异步任务有自己独立的「当前 span 栈」，是 slime 追踪父子 span 的底层机制。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `slime/utils/trace_utils.py` | 追踪核心：`TraceHandle`、`trace_span`、`trace_function`、SGLang 元数据采集 |
| `slime/rollout/sglang_rollout.py` | 在生成流程里埋点（`sglang_generate` span、`generate_and_rm` 装饰器） |
| `slime/rollout/filter_hub/base_types.py` | `MetricGatherer`：收集动态过滤丢弃原因等流程指标 |
| `slime/utils/metric_utils.py` | 指标计算原子函数：`compute_statistics`、`has_repetition`、`compute_pass_rate` |
| `slime/ray/rollout.py` | `compute_metrics_from_samples` / `compute_perf_metrics_from_samples`：聚合样本成指标，并把 trace 事件反向消费为性能指标 |
| `slime/utils/logging_utils.py` | `log()`：把指标字典写到 wandb / TensorBoard |
| `tools/analyze_profile.py` | 解读 SGLang decode 的 PyTorch profiler trace |
| `tools/profile_rollout.py` | 触发 SGLang 多引擎同时打 profile（产出 `.trace.json.gz`） |
| `tools/trace_timeline_viewer.py` | 把保存的 rollout dump 渲染成 trace 时间线 HTML |
| `docs/en/developer_guide/trace.md`、`profiling.md` | trace viewer 与 profiling 的用户文档 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先讲追踪载体（`TraceHandle/Span`），再讲它如何采集 SGLang 元数据，然后讲指标如何从样本聚合并反向消费 trace，最后讲 profile 剖析工具。

### 4.1 追踪载体：TraceHandle 与 trace_span

#### 4.1.1 概念说明

slime 的追踪系统回答一个问题：**「这一条样本在 rollout 阶段都经历了哪些步骤、各花了多久？」**

它的设计哲学是「轻量、与样本绑定、不依赖外部追踪后端（如 Jaeger）」：

- 每个被追踪的 `Sample` 上动态挂一个 `trace` 字段（一个普通 `dict`，叫 carrier），所有 span/event 都 append 到这个 dict 的 `events` 列表里。
- 一条 trace 用一个 `trace_id` 标识；每个 span 有自己的 `span_id` 和 `parent_span_id`，构成树。
- 跨进程/跨异步任务传递时，用 `export_trace` / `import_trace` 把 `(trace_id, span_id, ...)` 像行李牌一样带着走。

这样设计的好处是：trace 数据天然跟着 `Sample` 走，保存 rollout dump（`--save-debug-rollout-data`）时一并落盘，事后可离线用 `tools/trace_timeline_viewer.py` 回放，无需在线后端。

#### 4.1.2 核心流程

trace 的写入是一条「上下文式」流水：

1. `bind_trace(sample)`：给样本初始化 carrier，返回一个轻量句柄 `TraceHandle`（持有 carrier 引用与 id）。
2. 进入 `trace_span(target, name)`：为每个 target 生成新 `span_id`，往 carrier append 一条 `span_start` 事件，并把 `(trace_id, span_id)` 压入「当前 span 栈」（用 `contextvars.ContextVar` 实现，每个异步任务独立）。
3. 函数体执行；期间可用 `span.set(...)` / `span.update(...)` 给 span 追加结尾属性。
4. 离开 `trace_span`（无论正常还是异常）：append 一条 `span_end` 事件，并弹出栈。
5. 嵌套的 `trace_span` 会自动以栈顶 span 为 `parent_span_id`，形成父子层级。

关键不变量是「当前父 span」由 `_get_current_parent_span_id` 在栈里反序查找同 `trace_id` 的最近一条得到——这正是 `contextvars` 保证异步隔离的用处。

#### 4.1.3 源码精读

`TraceHandle` 是一个极简 dataclass，只存 id 与 carrier 引用，本身不含事件数据：

[trace_utils.py:58-65 — `TraceHandle` 只持有 trace_id、carrier 引用与 sample/group 标识](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/trace_utils.py#L58-L65)

carrier 由 `_ensure_trace_carrier` 统一初始化，保证字段齐全（`version`/`trace_id`/`events`/`sample_id`/`group_id`/`attempt`）：

[trace_utils.py:219-241 — `_ensure_trace_carrier` 用 setdefault 保证 carrier 字段完整](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/trace_utils.py#L219-L241)

「当前 span 栈」是两个模块级 `ContextVar`，分别记录 `(trace_id, span_id)` 序列和 handle 分组：

[trace_utils.py:47-54 — 两个 `contextvars.ContextVar` 维护当前 span 栈，保证异步任务隔离](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/trace_utils.py#L47-L54)

`trace_span` 是上下文管理器（`@contextmanager`），核心是「进栈 → yield → 出栈」三段式，并在异常时也保证 append `span_end`：

[trace_utils.py:350-431 — `trace_span`：压栈、yield 上下文、异常/正常均记录 span_end 并复位栈](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/trace_utils.py#L350-L431)

每条事件由 `_append_event` 统一写入，结构含 `type`/`name`/`ts`/`trace_id`/`span_id`/`parent_span_id`/`attrs`：

[trace_utils.py:593-619 — `_append_event` 把一条事件 append 进 carrier 的 events 列表](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/trace_utils.py#L593-L619)

`trace_function` 装饰器是把整函数包成一个 span 的语法糖，自动解析 trace target（支持 sync/async）：

[trace_utils.py:457-502 — `trace_function` 装饰器，对 sync/async 函数统一用 `trace_span` 包裹](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/trace_utils.py#L457-L502)

slime 在默认 rollout 流程里就是这么埋点的——`generate_and_rm`（单样本）和 `generate_and_rm_group`（整组）都被装饰：

[sglang_rollout.py:222-228 — `generate_and_rm` 用 `@trace_function(..., target="sample")` 包成每样本一个 span](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L222-L228)

[sglang_rollout.py:290-294 — `generate_and_rm_group` 用 `target="group"` 并附带 group_size 属性](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L290-L294)

#### 4.1.4 代码实践

**实践目标**：在本地（无需 GPU）理解 trace carrier 的结构，看到 span_start/span_end 是如何成对写入的。

**操作步骤**：

```python
# 示例代码：纯 CPU 可跑，演示 trace_span 如何写入 Sample.trace
from slime.utils.types import Sample
from slime.utils.trace_utils import bind_trace, trace_span, trace_event

s = Sample(index=0, group_index=0, tokens=[], response=[], loss_mask=[])
h = bind_trace(s)                       # 给 sample 挂上 carrier

with trace_span(s, "mock_generate", attrs={"max_new_tokens": 8}) as span:
    trace_event(s, "before_call")
    span.set("finish_reason", "stop")   # 给 span 追加结尾属性

import json
print(json.dumps(s.trace["events"], indent=2))
```

**需要观察的现象**：`s.trace` 是一个 dict，含 `version`/`trace_id`/`events`/`sample_id=0`/`group_id=0`；`events` 列表里应有 3 条：`span_start`（mock_generate）、`event`（before_call）、`span_end`（mock_generate，attrs 里带 `finish_reason`）。前两条与最后一条的 `span_id` 相同，体现成对。

**预期结果**：span_start 与 span_end 共享同一 `span_id`，`before_call` 的 `parent_span_id` 指向该 span_id——这就是父子层级。若你嵌套第二层 `trace_span`，它的 `parent_span_id` 会自动指向外层。

#### 4.1.5 小练习与答案

**练习 1**：为什么 slime 用 `contextvars.ContextVar` 而不是普通全局变量来存「当前 span 栈」？

**参考答案**：rollout 是高并发异步流程（`asyncio.gather` 同时处理大量样本），不同样本的 span 嵌套互不相同。普通全局变量会被所有任务共享，导致 A 样本的 span 错误地成为 B 样本 span 的父级；`ContextVar` 让每个异步任务有独立副本，父子关系只在单条样本的执行流内成立。

**练习 2**：`trace_span` 在函数体内抛异常时，span_end 还会写吗？attrs 会带什么？

**参考答案**：会。`trace_span` 的 `except` 分支在 `raise` 之前调用 `_record_span_end`，且当 `record_error=True`（默认）时会把 `error_type`/`error_message` 并入 end attrs，所以异常 span 在时间线上会显式标记错误。

### 4.2 SGLang 元数据采集与 PD 分段

#### 4.2.1 概念说明

光有时间跨度不够，还要知道「这次生成排队了多久、解码吞吐多少、命中了多少前缀缓存」。这些数据 SGLang 在响应的 `meta_info` 里返回。slime 用 `build_sglang_meta_trace_attrs` 把它们规整成 trace 属性，挂到 `sglang_generate` span 上。

特别地，当开启 PD 分离（prefill/decode disaggregation，见 u8-l2）时，SGLang 会返回一组 `pd_prefill_*` / `pd_decode_*` 时间字段。slime 不只是平铺记录，而是把它们**重组成带 `start_offset`/`end_offset` 的子 span**（synthetic children），这样 trace 时间线能分出 `[P]`/`[D]` 两条泳道，直观看到 prefill 内部的 bootstrap、forward、transfer 各占多少。

#### 4.2.2 核心流程

1. 定义两份白名单：`SGLANG_TRACE_META_KEYS`（普通请求级指标）与 PD 分段表（`SGLANG_PD_PREFILL_SEGMENTS` / `SGLANG_PD_DECODE_SEGMENTS`）。
2. `build_sglang_meta_trace_attrs(meta)` 从 `meta` 里按白名单取出非空字段，组装成 attrs；同时调 `_build_sglang_pd_trace_children` 生成子 span 列表，挂在特殊键 `_trace_children` 下。
3. rollout 代码在拿到 SGLang 响应后，用 `span.update(build_sglang_meta_trace_attrs(output["meta_info"]))` 把这些属性并入当前 span。
4. `_append_trace_children` 在 span 结束时把这些 synthetic 子 span 按 offset 展开成真正的 `span_start`/`span_end` 事件写回 carrier。

#### 4.2.3 源码精读

请求级白名单定义了最常用的 6 个 SGLang 指标键名：

[trace_utils.py:18-25 — `SGLANG_TRACE_META_KEYS` 白名单（token 数、queue_time、e2e_latency、decode 吞吐等）](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/trace_utils.py#L18-L25)

PD 分段表把 SGLang 的扁平字段名映射成可读的子 span 名（如 `pd_prefill_forward_duration` → `sglang_pd_prefill_forward`）：

[trace_utils.py:26-44 — PD prefill/decode 分段表与 summary 键，用于重组出 [P]/[D] 子 span](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/trace_utils.py#L26-L44)

`build_sglang_meta_trace_attrs` 用列表推导安全取值（缺键跳过），并把 PD 子 span 放进 `_trace_children`：

[trace_utils.py:146-164 — `build_sglang_meta_trace_attrs`：从 meta_info 规整出请求级属性与 PD 子 span](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/trace_utils.py#L146-L164)

`_build_sglang_pd_trace_children` 用游标 `cursor` 把每段的 duration 串成连续时间线，组装出嵌套的 prefill/decode 父子 span：

[trace_utils.py:167-216 — `_build_sglang_pd_trace_children`：用 offset 游标把 PD 字段重组成带时间偏移的子 span](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/trace_utils.py#L167-L216)

rollout 侧的实际埋点——一次 SGLang 调用就是一个 `sglang_generate` span，结束后把 meta 属性并进来：

[sglang_rollout.py:200-202 — `sglang_generate` span：调 SGLang 后用 `build_sglang_meta_trace_attrs` 并入 meta](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L200-L202)

#### 4.2.4 代码实践

**实践目标**：理解 `build_sglang_meta_trace_attrs` 在「有/无 PD 字段」两种输入下的输出差异。

**操作步骤**：

```python
# 示例代码：模拟 SGLang meta_info，看属性如何被规整
from slime.utils.trace_utils import build_sglang_meta_trace_attrs

# 场景 A：普通单引擎
meta_a = {"prompt_tokens": 100, "completion_tokens": 50, "queue_time": 0.02,
          "e2e_latency": 1.2, "decode_throughput": 180.0, "id": 7,
          "finish_reason": {"type": "stop"}}
print("A:", build_sglang_meta_trace_attrs(meta_a))

# 场景 B：开启 PD 分离，带 prefill 各段耗时
meta_b = {**meta_a,
          "pd_prefill_bootstrap_queue_duration": 0.01,
          "pd_prefill_forward_duration": 0.15,
          "pd_transfer_speed_gb_s": 120.0,
          "pd_transfer_total_mb": 512.0}
out = build_sglang_meta_trace_attrs(meta_b)
print("B 顶层属性:", {k: v for k, v in out.items() if k != "_trace_children"})
print("B 子 span 数:", len(out.get("_trace_children", [])))
```

**需要观察的现象**：场景 A 的属性里出现 `sglang_request_id=7`、`finish_reason="stop"`、`decode_throughput=180.0` 等；场景 B 额外有 `_trace_children`，里面含一个 `sglang_pd_prefill` 父 span，其 `children` 含 `sglang_pd_prefill_forward` 等子项，且 `start_offset`/`end_offset` 是连续累加的。

**预期结果**：白名单外的字段（如 `prompt_tokens`）被纳入，缺省字段不报错；PD 子 span 的 offset 单调递增，可在时间线上拼出 prefill 内部时序。**待本地验证**：具体字段是否出现取决于你安装的 SGLang 版本实际返回哪些 key。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `build_sglang_meta_trace_attrs` 全程用 `try/except` 包裹并在异常时只 `logger.debug`？

**参考答案**：追踪是「锦上添花」，绝不能因为 meta 结构变化（SGLang 升级改字段）而中断 rollout 主流程。所有 trace 函数都遵循「失败即静默降级」原则，保证可观测性工具自身不成为故障源。

**练习 2**：PD 子 span 用 `start_offset`/`end_offset`（相对父 span 起点的偏移）而不是绝对时间戳，为什么？

**参考答案**：这些 duration 来自 SGLang 聚合后的统计值，slime 侧拿不到其绝对时钟；用相对偏移由 `_append_trace_children` 在父 span 的 `start_ts` 基础上还原出近似时间线，足以在 viewer 里呈现 prefill 内部各段的比例关系。

### 4.3 指标聚合：compute_metrics_from_samples

#### 4.3.1 概念说明

trace 是「逐样本、细粒度」的，适合下钻排查；指标则是「逐 rollout 步、聚合」的，适合在 wandb/TensorBoard 上看趋势。slime 在 `RolloutManager.generate` 完成后调用 `_log_rollout_data`，把整批样本喂给 `compute_metrics_from_samples`（质量指标）和 `compute_perf_metrics_from_samples`（性能指标），产出带 `rollout/`、`perf/` 前缀的字典，再经 `logging_utils.log` 写出。

`compute_metrics_from_samples` 是指标总入口，它把多个子计算用 `|=` 拼成一个字典，每个子函数负责一类指标。理解它的关键是：**指标几乎全部直接读 `Sample` 的字段**，只有性能子函数会回头去挖 trace 事件（见 4.4）。

#### 4.3.2 核心流程

1. `RolloutManager.generate(rollout_id)` 记录 `start_time`，取 rollout 数据，保存 debug dump，调 `_log_rollout_data`，`rollout_time = time.time() - start_time`。
2. `_log_rollout_data` 依次调用：
   - `compute_metrics_from_samples(...)` → `rollout/` 前缀（响应长度统计、重复率、截断率、零方差组计数、前缀缓存命中率等）；
   - `compute_perf_metrics_from_samples(...)` → `perf/` 前缀（吞吐、非生成时间、SGLang 请求级耗时）；
   - 动态过滤丢弃计数（来自 `MetricGatherer`）。
3. `compute_rollout_step` 把 `rollout_id` 换算成 wandb 步号。
4. `logging_utils.log(args, log_dict, step_key="rollout/step")` 按后端（wandb / TensorBoard）写出。

#### 4.3.3 源码精读

入口 `compute_metrics_from_samples` 是多子函数的聚合器，每个 `|=` 接一类指标：

[rollout.py:1312-1324 — `compute_metrics_from_samples`：聚合响应长度、零方差、spec、前缀缓存、重复率、截断率](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L1312-L1324)

注意它读的是 `sample.effective_response_length`——只算 `loss_mask=1` 的可训练 token 数（工具/环境 token 不计）：

[types.py:249-251 — `effective_response_length`：有 loss_mask 时取其和，否则取 response_length](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/types.py#L249-L251)

`compute_statistics` 是最常用的原子函数，返回 mean/median/max/min：

[metric_utils.py:59-66 — `compute_statistics`：对一组数值算均值/中位/极值](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/metric_utils.py#L59-L66)

重复检测用了一个巧妙的压缩比启发式：把 response 末尾 1 万字符做 zlib 压缩，压缩比 >10 视为重复（重复文本压缩率极高）：

[metric_utils.py:113-117 — `has_repetition`：用压缩比启发式判断 response 是否重复退化](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/metric_utils.py#L113-L117)

前缀缓存命中率直接累加每个样本的 `prefix_cache_info`：

[rollout.py:1470-1478 — `_compute_prefix_cache_metrics`：命中 token / 总 prompt token](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L1470-L1478)

`_log_rollout_data` 把这些字典合并、加 step、调用 `logging_utils.log`：

[rollout.py:1294-1309 — `_log_rollout_data`：合并 metrics/perf 字典，算 step 并写出](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L1294-L1309)

`logging_utils.log` 根据开关分别写 wandb 与 TensorBoard：

[logging_utils.py:45-51 — `log`：按 `use_wandb`/`use_tensorboard` 分发到对应后端](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/logging_utils.py#L45-L51)

动态过滤的丢弃计数走另一条路：`MetricGatherer` 在 rollout 流程内边跑边累计，最后 `collect()` 成 `rollout/dynamic_filter/drop_*` 指标：

[base_types.py:40-53 — `MetricGatherer`：累计丢弃原因，collect 成 drop 指标](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/filter_hub/base_types.py#L40-L53)

#### 4.3.4 代码实践

**实践目标**：手工构造一批样本，调用 `compute_metrics_from_samples`，看到它输出的指标字典结构。

**操作步骤**：

```python
# 示例代码：构造 3 个样本，观察指标字典
from types import SimpleNamespace
from slime.utils.types import Sample
from slime.ray.rollout import compute_metrics_from_samples

def mk(resp_len, reward, status=Sample.Status.COMPLETED):
    s = Sample(index=0, group_index=0, tokens=[1]*resp_len, response=[1]*resp_len,
               loss_mask=[1]*resp_len, reward=reward)
    s.status = status
    return s

args = SimpleNamespace(advantage_estimator="grpo", reward_key=None,
                       log_reward_category=None,
                       sglang_speculative_algorithm=None)
samples = [mk(50, 1.0), mk(120, 0.0), mk(80, 1.0, Sample.Status.TRUNCATED)]
import json; print(json.dumps(compute_metrics_from_samples(args, samples), indent=2))
```

**需要观察的现象**：输出含 `response_len/mean`、`response_len/max`、`truncated_ratio`（约 0.33）、`repetition_frac`、`prefix_cache_hit_rate`、`zero_std/...`（GRPO 下若某组奖励全相同会计数）。

**预期结果**：`truncated_ratio=0.333...`，`response_len/mean≈83.3`。若把三个 reward 全设为 1.0 且同 `group_index`，会看到 `zero_std/count_1.0` 之类的键。**待本地验证**：`compute_metrics_from_samples` 内部对 `args` 字段的依赖较多，缺失字段可能需要按实际补齐。

#### 4.3.5 小练习与答案

**练习 1**：为什么响应长度用 `effective_response_length`（loss_mask 之和）而不是 `response_length`？

**参考答案**：在 agentic/多轮场景里，response 含大量工具观测等 `loss_mask=0` 的非训练 token。用裸 `response_length` 会让指标被环境文本膨胀，误导「模型实际生成了多少」的判断；`effective_response_length` 只计模型自己生成、参与训练的 token，更能反映真实生成长度与吞吐。

**练习 2**：`compute_pass_rate` 计算 pass@k，请写出 pass@k 的定义。

**参考答案**：对一个 prompt 的 n 次采样中若正确 c 次，pass@k 表示「任取 k 次至少有一次正确」的概率：

\[
\text{pass@}k = 1 - \frac{\binom{n-c}{k}}{\binom{n}{k}}
\]

当 \(n - c < k\) 时直接为 1。slime 的 `_estimate_pass_at_k` 用连乘 `np.prod` 数值稳定地计算它（[metric_utils.py:43-56](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/metric_utils.py#L43-L56)）。

### 4.4 性能指标与 trace 事件的反向消费

#### 4.4.1 概念说明

这是本讲最巧妙的一处设计：**trace 不只用来画时间线，还被「反向消费」成聚合性能指标**。

4.2 里我们把 SGLang 的 `e2e_latency`、`queue_time`、`decode_throughput` 以及 PD 各段耗时写进了每个样本的 `sglang_generate` span 的 end attrs。4.4 要讲的是：`compute_perf_metrics_from_samples` 调用 `_compute_sglang_request_perf_metrics`，遍历所有样本的 `sample.trace["events"]`，把名字为 `sglang_generate` 的 `span_end` 事件里的 attrs 收集起来，按字段表聚合成 `request/e2e_latency/mean`、`decode/throughput/max` 之类的指标。

换句话说：**一次埋点，两处用**——细粒度进 trace viewer 下钻，粗粒度进 wandb 看趋势。

#### 4.4.2 核心流程

1. 字段表 `_SGLANG_REQUEST_PERF_FIELDS` / `_SGLANG_PREFILL_PERF_FIELDS` / `_SGLANG_DECODE_PERF_FIELDS` 定义「指标名 → trace attr 键」的映射。
2. `_iter_sglang_generate_attrs` 遍历样本，从每个样本的 trace 事件里筛出 `sglang_generate` 的 `span_end`，yield 其 attrs。
3. `_compute_sglang_request_perf_metrics` 把这些 attrs 按字段表分桶收集，最后对每桶调 `compute_statistics`。
4. 同时 `compute_perf_metrics_from_samples` 还算吞吐：`tokens_per_gpu_per_sec = sum(响应长度) / rollout_time / rollout_num_gpus`，并尝试扣除 `non_generation_time`。

#### 4.4.3 源码精读

字段表把 trace attr 键映射成带前缀的指标名（注意 source_key 正是 4.2 写入的那些 SGLang 键）：

[rollout.py:51-72 — 三组字段表：请求级 / prefill / decode 性能字段的映射](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L51-L72)

`_iter_sglang_generate_attrs` 是 trace → metrics 的桥，只挑 `sglang_generate` 的 span_end：

[rollout.py:1400-1410 — `_iter_sglang_generate_attrs`：从每个样本的 trace 事件里筛出 sglang_generate 的 span_end attrs](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L1400-L1410)

`_compute_sglang_request_perf_metrics` 按字段表收集有效数值（过滤非有限值），再算统计量：

[rollout.py:1361-1397 — `_compute_sglang_request_perf_metrics`：把 trace attrs 聚合成请求级性能指标](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L1361-L1397)

吞吐与「最长样本」相关指标在 `compute_perf_metrics_from_samples` 里，扣除 `non_generation_time` 后得到「纯生成」吞吐：

[rollout.py:1327-1358 — `compute_perf_metrics_from_samples`：吞吐、非生成时间、并接入 sglang 请求级指标](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L1327-L1358)

#### 4.4.4 代码实践

**实践目标**：复现「trace → 性能指标」的反向消费链，理解没有 trace 时性能子指标为空。

**操作步骤**：

```python
# 示例代码：给样本挂上带 sglang_generate span_end 的 trace，再聚合
from types import SimpleNamespace
from slime.utils.types import Sample
from slime.utils.trace_utils import bind_trace, trace_span
from slime.ray.rollout import _compute_sglang_request_perf_metrics

s = Sample(index=0, group_index=0, tokens=[], response=[], loss_mask=[1]*50,
           response_length=50)
bind_trace(s)
with trace_span(s, "sglang_generate") as span:
    span.update({"e2e_latency": 1.3, "queue_time": 0.05, "decode_throughput": 200.0})

print(_compute_sglang_request_perf_metrics([s]))
# 再试一个完全没有 trace 的样本
s2 = Sample(index=0, group_index=0, tokens=[], response=[], loss_mask=[1]*50)
print(_compute_sglang_request_perf_metrics([s2]))
```

**需要观察的现象**：第一个调用返回含 `request/e2e_latency/mean=1.3`、`request/queue_time/mean=0.05`、`decode/throughput/mean=200.0` 的字典；第二个（无 trace）返回空 `{}`。

**预期结果**：验证了「性能指标完全依赖 trace 事件存在」。若生产中 `perf/request/e2e_latency/*` 缺失，说明该 rollout 的样本未被埋点（例如走了不带 `sglang_generate` span 的自定义生成函数）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_compute_sglang_request_perf_metrics` 要用 `np.isfinite` 过滤？

**参考答案**：SGLang 偶尔会返回 `inf`（如除零得到的吞吐）或 `nan`（空 batch）。若不过滤，`compute_statistics` 的 mean/max 会被污染成 `inf`/`nan`，直接毁掉整张 wandb 图表。

**练习 2**：`tokens_per_gpu_per_sec` 与 `decode/throughput` 有何区别？

**参考答案**：前者是 slime 侧的「宏观」吞吐——整批响应 token 总和除以整段 `rollout_time` 再除以 GPU 数，含排队与 prefill；后者是 SGLang 自报的「解码」阶段吞吐（token/s），仅算 decode。两者差距大通常意味着时间花在 prefill 或排队上（这正是 4.6 综合实践要定位的情形）。

### 4.5 Profiling 剖析：analyze_profile

#### 4.5.1 概念说明

trace 和指标都在「请求级」粒度——它们能告诉你「这次生成慢」，但说不清「慢在哪个 GPU kernel」。要做到 kernel 级下钻，需要 PyTorch profiler 抓的 GPU trace。slime 的 `tools/profile_rollout.py` 通过 SGLang router 的接口触发各引擎同时打点，产出 `.trace.json.gz`（Chrome Trace Event 格式，可用 `chrome://tracing` 或 Perfetto 打开）；而 `tools/analyze_profile.py` 则把这些文件**自动解读**成诊断报告：GPU 利用率、kernel 分类占比、CUDA Graph 停顿、跨 rank 负载均衡，并给出优化建议。

#### 4.5.2 核心流程

`analyze_profile.py` 的 `analyze_trace` 对单个 trace 文件做以下分析：

1. **设备/环境**：从 `deviceProperties`/`distributedInfo` 读 GPU 型号、VRAM、CUDA/NCCL 版本、world_size。
2. **kernel 分类**：`classify_kernel` 按名称把每个 GPU kernel 归入一类（Flash Attention、DeepEP、GEMM、NCCL、TopK…），统计各类的总时长与次数。
3. **GPU 利用率**：把所有 GPU 事件（kernel/memcpy/memset）的时间区间合并相交区间，busy 时间 / active span 即利用率；空闲段即「气泡」。
4. **CUDA Graph**：按 `cudaGraphLaunch` 事件分组，每 3 次启动算一个 decode step，估算每步耗时与吞吐。
5. **瓶颈诊断**：根据利用率、大 launch、DeepEP 占比、TopK 占比等阈值，列出 HIGH/MED/LOW 级问题与建议。

#### 4.5.3 源码精读

`classify_kernel` 是一个长 if/elif 链，按 kernel 名子串匹配归类（先匹配更具体的，如 DeepEP 的 dispatch/combine）：

[analyze_profile.py:130-176 — `classify_kernel`：按 kernel 名把 GPU 算子归入语义类别](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/analyze_profile.py#L130-L176)

GPU 利用率靠合并重叠区间计算（扫描线合并）：

[analyze_profile.py:235-249 — 合并 GPU 事件区间，busy/active span 得出利用率](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/analyze_profile.py#L235-L249)

每 3 次 `cudaGraphLaunch` 归为一个 decode step，估算每步 span 与 token/s：

[analyze_profile.py:304-318 — 每 3 次 cudaGraphLaunch 为一个 decode step，记录每步 span](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/analyze_profile.py#L304-L318)

瓶颈诊断按阈值生成 HIGH/MED/LOW 问题清单（GPU 利用率 <90%、大 launch、DeepEP >15% 等）：

[analyze_profile.py:492-573 — 根据阈值列出 GPU 利用率、CUDA Graph 停顿、DeepEP 等瓶颈](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/analyze_profile.py#L492-L573)

命令行入口支持单 rank、指定 rank、全 rank 对比：

[analyze_profile.py:678-684 — `main`：`--profile-dir`/`--rank`/`--all-ranks` 参数](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/analyze_profile.py#L678-L684)

注意配套的采集工具：profile 由 `tools/profile_rollout.py` 经 router 触发，文档见 `profiling.md`：

[profiling.md:33-50 — `tools/profile_rollout.py --action start --num-steps 3` 触发多引擎同时打点](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/developer_guide/profiling.md#L33-L50)

> 说明：decode step 估算吞吐约为

\[
\text{tokens/s/GPU} \approx \frac{10^{6}}{\text{avg\_step\_span\_us}}
\]

该式假设一步 decode 产出一个 token（batch 内并行），仅作量级参考。

#### 4.5.4 代码实践

**实践目标**：跑通 profile 分析工具，读懂它输出的瓶颈清单。

**操作步骤**（需有真实跑过的 profile 文件；若无，先完成 4.6 综合实践采集）：

1. 确认 `profiles/` 下有 `*.trace.json.gz`（由 `tools/profile_rollout.py` 产出）。
2. 分析第一个文件：
   ```bash
   python tools/analyze_profile.py --profile-dir profiles/your_run
   ```
3. 全 rank 对比，看负载是否均衡：
   ```bash
   python tools/analyze_profile.py --profile-dir profiles/your_run --all-ranks
   ```

**需要观察的现象**：报告分多段——Hardware & Config、Timeline（含 GPU 利用率条）、Kernel Breakdown（各类算子占比条形图）、Top Kernels、CUDA Graph（含 decode steps 表与吞吐估算）、Top GPU Idle Gaps（气泡及其成因）、Communication、Bottleneck Analysis、Optimization Recommendations。

**预期结果**：典型 MoE 模型的报告里，High 级瓶颈常是「CUDA Graph Launch Stalls」（MoE 部分跑在图外）与「DeepEP Dispatch Dominance」（专家 all-to-all 通信）；`print_cross_rank_summary` 会标出跨 rank 利用率 spread >5% 的负载不均。**待本地验证**：实际瓶颈取决于模型结构与硬件。

#### 4.5.5 小练习与答案

**练习 1**：`analyze_trace` 怎样判定一段 GPU 空闲的「成因」？

**参考答案**：它先找出空闲 gap 的时间区间，再遍历 CPU 侧操作（`cpu_op`/`user_annotation`/`cuda_runtime`），取第一个与该 gap 时间区间相交的操作名作为 `cause`（[analyze_profile.py:264-276](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/analyze_profile.py#L264-L276)）。这是一种启发式归因，帮助你猜测气泡是 CPU 启动慢、调度还是同步等待。

**练习 2**：为什么按「每 3 次 cudaGraphLaunch」划 decode step？

**参考答案**：SGLang 在 decode 时把一轮拆成三段 CUDA Graph——MoE 之前、MoE 之后、以及专家部分（DeepEP dispatch/combine + NCCL allgather 往往无法进图），故一个 decode step 对应 3 次启动。代码注释与建议里也据此给出「3-launch 模式 = pre-MoE / post-MoE / MoE-expert」的解释（[analyze_profile.py:586-594](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/analyze_profile.py#L586-L594)）。若你的配置不是该模式，吞吐估算会失真。

## 5. 综合实践：用三件套定位一次 rollout 吞吐异常

**场景**：你在 wandb 上看到某次 rollout 的 `perf/tokens_per_gpu_per_sec` 突然掉了一半，但 `rollout/response_len/mean` 没变（生成长度正常）。如何用 trace + 指标 + profile 定位？

**目标**：把四个模块串成一条排查流水，定位瓶颈落在「KV cache / prefill / 排队 / kernel」哪一层。

**操作步骤**：

1. **先看指标，缩小范围**（对应 4.3/4.4）。对比几个关键指标：
   - `perf/tokens_per_gpu_per_sec` ↓ 但 `rollout/response_len/mean` 持平 → 单位时间产出的 token 变少，问题在「速度」不在「长度」。
   - 若 `perf/request/queue_time/mean` 显著上升 → 倾向**排队**（请求在 SGLang router/engine 队列里等）。
   - 若 `rollout/prefix_cache_hit_rate` 下降 → 倾向 **prefill 变重**（前缀缓存失效，更多 token 走 prefill）。
   - 若 `perf/decode/throughput/mean` 下降而 `queue_time` 正常 → 倾向 **decode 本身变慢**（kernel 级，需 profile）。
   - 若 `rollout/truncated_ratio` 上升 → 样本顶到 `max_new_tokens`，可能是长尾样本拖慢步边界（与 u7-l4 的 fully_async/partial rollout 相关）。

2. **再用 trace 下钻到样本级**（对应 4.1/4.2）。打开 `--save-debug-rollout-data /path/to/debug/rollout_{rollout_id}.pt`，用 viewer 看时间线：
   ```bash
   python tools/trace_timeline_viewer.py /path/to/debug/rollout_0.pt
   ```
   重点看：
   - `sglang_generate` span 的 `queue_time` 与 `e2e_latency` 属性——是否大量时间在排队而非生成？
   - 若开了 PD，看 `[P]`/`[D]` 泳道：prefill 的 `pd_prefill_forward_duration` 是否异常长？`pd_transfer_*` 是否占大头（KV 迁移慢）？
   - 找最长的几条 span，看它们是被 queue 拖长还是 decode 慢。

3. **最后用 profile 定位 kernel 级根因**（对应 4.5）。让 rollout 进入等待态再压测：
   ```bash
   # 训练脚本里加 --rollout-function-path slime.rollout.sleep_rollout.sleep
   # 另起终端触发 profiling
   python tools/profile_rollout.py --router-url http://127.0.0.1:3000 --action start --num-steps 3
   python tools/analyze_profile.py --profile-dir profiles/your_run --all-ranks
   ```
   重点看报告：
   - GPU 利用率低 + Top GPU Idle Gaps 成因是调度/同步 → **排队或 CPU 启动开销**。
   - DeepEP Dispatch 占比高 → **MoE 专家通信**（可降 EP degree 或查 NVLink 带宽）。
   - CUDA Graph 大 launch 多 → MoE 跑在图外的开销（可调 batch 或 `--sglang-disable-cudagraph` 排查）。
   - 跨 rank 利用率 spread >5% → **负载不均**（router 分配或显存配比问题）。

**需要观察的现象**：三件套各有粒度——指标给「宏观趋势与方向」，trace 给「单样本时序与 SGLang 自报耗时」，profile 给「kernel 级成因」。三者交叉印证才能锁定根因，单一工具往往只能猜。

**预期结果**：你应当能给出一个有据可查的结论，例如「`queue_time` 上升 + GPU 利用率低 + Idle Gaps 成因为调度 → 瓶颈在 router 排队，建议调大并发或检查 `--router-policy`」，而非笼统的「变慢了」。**待本地验证**：实际根因取决于集群状态，上述只是排查框架。

## 6. 本讲小结

- slime 的可观测性是「三件套」：**trace（逐样本细粒度）+ metrics（逐 rollout 聚合趋势）+ profile（kernel 级下钻）**，分别回答「这条样本经历了什么」「这步整体如何」「慢在哪个算子」。
- **trace** 把 span/event 写进动态挂在 `Sample.trace` 上的 carrier（dict），用 `contextvars` 维护异步隔离的父子栈，全程 `try/except` 静默降级，绝不影响主流程；可用 `--save-debug-rollout-data` 落盘后用 `trace_timeline_viewer.py` 离线回放。
- **SGLang 元数据采集**（`build_sglang_meta_trace_attrs`）把 `meta_info` 里的请求级指标与 PD 分段耗时规整成 span 属性甚至重组出 `[P]`/`[D]` 子 span，一次埋点既进时间线又供聚合。
- **`compute_metrics_from_samples`** 是质量指标总入口，聚合响应长度、重复率、截断率、零方差组、前缀缓存命中率等；`compute_perf_metrics_from_samples` 算吞吐并扣除非生成时间；指标经 `logging_utils.log` 写 wandb/TensorBoard。
- 最巧妙的设计是**「trace 反向消费成指标」**：`_compute_sglang_request_perf_metrics` 遍历样本 trace 里的 `sglang_generate` span_end 事件，按字段表聚合成 `request/*`、`decode/*` 性能指标——一次埋点两处用。
- **`analyze_profile.py`** 解读 SGLang decode 的 PyTorch profiler trace，靠 `classify_kernel` 归类、区间合并算利用率、CUDA Graph 分组估吞吐，并按阈值给出瓶颈清单与优化建议。

## 7. 下一步学习建议

- **自定义埋点**：阅读 `docs/en/developer_guide/trace.md`，在你自己的 custom-generate / custom-rm 里用 `trace_span`/`trace_function` 给工具调用、沙箱执行等阶段埋点（联系 u6-l2、u7-l2）。
- **追踪测试**：看 `tests/utils/test_trace_utils.py`，了解 trace 在跨 attempt、嵌套 span、错误传播上的不变量断言（联系 u8-l6 测试体系）。
- **PD 拓扑下的诊断**：结合 u8-l2（PD 分离与外部引擎），理解 `pd_transfer_*` 指标与 KV 迁移带宽的关系，以及外部引擎模式下 profile 的差异。
- **rollout 数据流进阶**：若关注长尾样本对吞吐的影响，结合 u7-l4（流式/全异步/partial rollout），理解 `non_generation_time` 与 partial rollout 续传如何反映在指标里。
