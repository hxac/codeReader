# 单页处理：重试、温度阶梯与旋转纠错

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `try_single_page` 一次请求的完整生命周期，以及 `is_valid` 的两个判定条件（token 超限与 `finish_reason`）。
2. 区分三种失败信号：返回 `None`、`is_valid=False`、`is_rotation_valid=False`，并说明它们分别触发什么处理。
3. 描述 `process_page` 的两条重试路径：旋转错误走**串行反馈重试**（每次用模型反馈的旋转角预转图片），非旋转错误走**串行重试、队列空时并行发射剩余重试**。
4. 解释 `TEMPERATURE_BY_ATTEMPT` 温度阶梯的设计意图，以及 `try_single_page_with_backoff` 的指数退避曲线。
5. 理解所有重试耗尽后的最终兜底：`make_fallback_result` 用 pdftotext 提取文本，保证流水线永不因单页卡死。

## 2. 前置知识

### 2.1 一次「请求-响应」里可能出什么错

本讲讨论的失败都发生在**单页粒度**上。回顾 u2-l1：每个 worker 拿到一个 PDF 后，会为每一页创建一个 `process_page` 协程任务。每一页都要经历「渲染图片 → 组装请求 → HTTP 调用推理服务 → 解析响应」。这条链路上任何一环都可能出问题，大致分三层：

| 层级 | 失败表现 | 谁负责处理 |
|---|---|---|
| 连接层 | `ConnectionError`、`OSError`、`asyncio.TimeoutError` | `try_single_page_with_backoff`（指数退避） |
| 请求层 | HTTP 非 200、响应解析异常 | `try_single_page` 返回 `None` |
| 内容层 | 输出被截断 / token 超限 / 页面是旋转的 | `process_page`（本讲主角） |

**温度（temperature）** 是大语言模型采样的超参数：越低采样越「确定」，越高越「随机」。低温度下模型偶尔会陷入**退化重复**（degenerate repetition）——一句话翻来覆去地写，直到撞上 `max_tokens` 上限被强行截断。提高温度是打破重复循环的常用手段，这正是温度阶梯存在的原因。

**`finish_reason`** 是 OpenAI 兼容 API 响应中的字段，`"stop"` 表示模型自然收尾，`"length"` 表示因达到 `max_tokens` 被截断（这是 vLLM 等服务的通用约定）。被截断的输出几乎必然缺尾巴，不能算一次合格的转写。

**旋转纠错** 回顾 u2-l4：模型在 front matter 里输出 `is_rotation_valid` 与 `rotation_correction`（限 0/90/180/270）。当模型判断页面是侧放/倒放的，流水线会把渲染出的 PNG 物理旋转后再重新提问——模型看到的永远是「正」的图。

### 2.2 你需要记住的两个数字

- `MAX_TOKENS = 8000`：单次请求允许模型生成的最大 token 数（[pipeline.py:L107](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L107)）。
- `MODEL_MAX_CONTEXT = 16384`：判定响应是否超上下文的阈值（[pipeline.py:L162](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L162)）。
- `--max_page_retries` 默认 8：一页最多尝试的总次数（[pipeline.py:L1221](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1221)）。温度表恰好也有 8 个元素，一一对应。

## 3. 本讲源码地图

本讲几乎全部内容集中在 `olmocr/pipeline.py` 的第 84~375 行，这是流水线中最「厚」的一段单页逻辑：

| 位置 | 函数/常量 | 作用 |
|---|---|---|
| [pipeline.py:L84-L85](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L84-L85) | `TEMPERATURE_BY_ATTEMPT` | 温度阶梯常量 |
| [pipeline.py:L94-L103](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L94-L103) | `PageResult` | 单页结果的数据类 |
| [pipeline.py:L106-L146](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L106-L146) | `build_page_query` | 渲染 + 组装请求体（含图片物理旋转） |
| [pipeline.py:L149-L230](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L149-L230) | `try_single_page` | 单页单次尝试 |
| [pipeline.py:L233-L250](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L233-L250) | `make_fallback_result` | pdftotext 兜底结果 |
| [pipeline.py:L253-L278](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L253-L278) | `try_single_page_with_backoff` | 连接错误指数退避包装 |
| [pipeline.py:L281-L375](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L281-L375) | `process_page` | 重试编排（本讲核心） |
| [pipeline.py:L876-L880](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L876-L880) | `vllm_queued_requests` 更新处 | 内部 vLLM 队列深度的来源（详见 u3-l3） |
| [pipeline.py:L548-L562](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L548-L562) | `process_pdf`（调用方） | 每页一个任务 + fallback 比例淘汰 |

## 4. 核心概念与源码讲解

### 4.1 try_single_page：一次请求的完整生命周期

#### 4.1.1 概念说明

`try_single_page` 是「问模型一次」的最小单元：渲染页面、组装 OpenAI 兼容的 chat 请求、发出、解析响应、打包成 `PageResult`。它的函数注释写得很清楚：**不做重试，重试是调用者的事**——职责单一，这让上层可以自由设计重试策略。

关键设计：它有**三种不同的返回形态**，分别携带不同的失败信息：

1. 返回 `None`：请求本身失败（HTTP 非 200、JSON 解析异常、front matter 解析异常等）。
2. 返回 `is_valid=False` 的 `PageResult`：拿到了响应，但内容不合格（截断或超限）。
3. 返回 `is_rotation_valid=False` 的 `PageResult`：内容和格式都合格，只是页面是旋转的。

#### 4.1.2 核心流程

```text
try_single_page(args, pdf路径, 页码, attempt, rotation)
  ├─ 1. 查温度表取本次 temperature（attempt 越大温度越高）
  ├─ 2. build_page_query：渲染 PNG，若 rotation≠0 用 PIL 物理旋转图片
  ├─ 3. （可选）挂上 guided_regex 引导解码
  ├─ 4. 在全局信号量内调 apost 发 HTTP POST
  ├─ 5. 非 200 → 返回 None
  ├─ 6. 记录 token 用量指标
  ├─ 7. is_valid 判定：
  │      total_tokens > 16384 → False
  │      finish_reason ≠ "stop" → False
  ├─ 8. FrontMatterParser 解析出 PageResponse
  ├─ 9. 返回 PageResult（is_fallback=False, is_valid=…）
  └─ 异常分流：
         CancelledError → 原样上抛（协程取消不能吞）
         ConnectionError/OSError/TimeoutError → 上抛给退避包装
         其他异常 → 记日志，返回 None
```

#### 4.1.3 源码精读

**温度阶梯的取值**（[pipeline.py:L84-L85](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L84-L85)）：模块级常量，8 个元素对应最多 8 次尝试，注释点明设计意图——「higher temperature helps overcome repetition issues」（高温克服重复问题）。

```python
TEMPERATURE_BY_ATTEMPT = [0.1, 0.1, 0.2, 0.3, 0.5, 0.8, 0.9, 1.0]
```

**按尝试序号查表**（[pipeline.py:L164-L165](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L164-L165)）：`min` 钳位保证 attempt 超出表长时取最后一个元素，永不越界：

```python
temp_idx = min(attempt, len(TEMPERATURE_BY_ATTEMPT) - 1)
temperature = TEMPERATURE_BY_ATTEMPT[temp_idx]
```

**请求体里温度是占位符**：`build_page_query` 先写死 `"temperature": 0.0`（[pipeline.py:L145](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L145)），随后在 [pipeline.py:L170-L177](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L170-L177) 被真实温度覆盖。同一函数还把 `rotation` 传给 `build_page_query`——图片的物理旋转发生在发送之前（旋转实现见 [pipeline.py:L114-L131](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L114-L131)，用 PIL `transpose` 旋转后重新编码 base64）：

```python
query = await build_page_query(..., image_rotation=rotation, ...)
query["temperature"] = temperature
```

**并发闸门与手动 HTTP**（[pipeline.py:L184-L185](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L184-L185)）：所有页共享 `max_concurrent_requests_limit` 信号量（u1-l5 讲过，默认 1600），请求本身用项目手写的 `apost` 发出（u2-l7 专门精读）：

```python
async with max_concurrent_requests_limit:
    status_code, response_body = await apost(COMPLETION_URL, json_data=query, api_key=api_key)
```

**is_valid 的两个判定**（[pipeline.py:L200-L206](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L200-L206)）：先默认合法，再被两个条件逐一击倒——总 token 超过 16384 说明这页把上下文撑爆了；`finish_reason` 不是 `"stop"`（典型值 `"length"`）说明输出被 `max_tokens=8000` 截断。注意**即使 is_valid=False 也照样往下走解析并返回结果**，把它留作「最后的备胎」是 `process_page` 结尾逻辑的伏笔：

```python
is_valid = True
if base_response_data["usage"]["total_tokens"] > MODEL_MAX_CONTEXT:
    is_valid = False
if base_response_data["choices"][0]["finish_reason"] != "stop":
    is_valid = False
```

**解析与打包**（[pipeline.py:L208-L222](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L208-L222)）：用 u2-l5 精读过的 `FrontMatterParser` 把 markdown 变成 `PageResponse`，连同 token 用量一起装进 `PageResult`（数据类定义见 [pipeline.py:L94-L103](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L94-L103)）：

```python
parser = FrontMatterParser(front_matter_class=PageResponse)
front_matter, text = parser._extract_front_matter_and_text(model_response_markdown)
page_response = parser._parse_front_matter(front_matter, text)
return PageResult(pdf_orig_path, page_num, page_response, ..., is_fallback=False, is_valid=is_valid)
```

**异常三分流**（[pipeline.py:L223-L230](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L223-L230)）：`CancelledError` 必须原样上抛（吞掉它会破坏 asyncio 取消机制）；连接类异常也上抛，交给退避包装重试；其余一切异常（如 JSON 字段缺失、front matter 非法）只记 warning 并返回 `None`——这类问题重试同样的请求大概率还是失败，交给上层的温度变化去碰运气：

```python
except asyncio.CancelledError:
    raise
except (ConnectionError, OSError, asyncio.TimeoutError):
    # Re-raise connection errors so caller can apply exponential backoff
    raise
except Exception as e:
    logger.warning(f"try_single_page failed ... {type(e).__name__}: {e}")
    return None
```

#### 4.1.4 代码实践

**实践目标**：亲眼看到温度阶梯的形状，并验证 attempt → temperature 的映射关系。

**操作步骤**（以下为示例代码，在装好 `olmocr` 的轻量环境中运行即可，`boto3`/`httpx`/`Pillow` 等都是主依赖，无需 GPU）：

```python
# temp_ladder.py —— 示例代码
from olmocr.pipeline import TEMPERATURE_BY_ATTEMPT

print(f"阶梯长度: {len(TEMPERATURE_BY_ATTEMPT)}")
for attempt, t in enumerate(TEMPERATURE_BY_ATTEMPT):
    bar = "#" * int(t * 40)
    print(f"attempt {attempt}: temperature={t:<4} {bar}")

# 验证 min 钳位：attempt 超出表长时取最后一个
for attempt in [7, 8, 20]:
    idx = min(attempt, len(TEMPERATURE_BY_ATTEMPT) - 1)
    print(f"attempt={attempt:>2} -> temp={TEMPERATURE_BY_ATTEMPT[idx]}")
```

**需要观察的现象**：前两次尝试温度都是 0.1（几乎确定性采样），从第 3 次开始爬坡，最后一步到 1.0；钳位逻辑让任何超大 attempt 都落在 1.0 上。

**预期结果**：输出 8 行阶梯状上升的温度条形图；`attempt=8/20` 均映射到 1.0。曲线设计意图：低温度先拿「最可能正确」的答案；反复失败暗示模型陷入重复循环，逐步加温把它「摇」出来。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `try_single_page` 把连接类异常上抛、却把其他异常吞成 `None`？

**答案**：连接失败意味着请求**根本没送达**（服务重启、网络抖动），重试同样的请求完全合理，且需要等待（退避）而不是立刻重试；而解析类异常说明请求送达了但响应内容有问题，立刻原样重试多半重蹈覆辙，返回 `None` 让上层换温度、换旋转角再试更划算。

**练习 2**：如果模型在 7900 个 token 处自然写完了，`finish_reason` 是什么？`is_valid` 是 True 还是 False？

**答案**：`finish_reason="stop"`（未触及 8000 上限），只要 `total_tokens ≤ 16384`，`is_valid=True`。`max_tokens` 只是上限，不是必须写满的配额。

**练习 3**：`build_page_query` 里写死 `"temperature": 0.0` 又马上被覆盖，为什么不留个必填参数？

**答案**：`build_page_query` 只负责「造请求体」，温度是**重试策略**的一部分，属于调用方 `try_single_page` 的职权。先用占位值保证字段齐全、再覆盖，让两个函数职责分明——这也是它同时被 GRPO 等其他场景复用时不必关心温度的原因。

### 4.2 process_page：两条重试路径的编排

#### 4.2.1 概念说明

`process_page` 是本讲的主角：给定一页，保证**总能返回一个 `PageResult`**——要么是模型的成功答案，要么是兜底结果。它把 4.1 的三种失败信号编排成两条性质完全不同的重试路径：

- **旋转错误路径（串行反馈重试）**：模型说「图是歪的」。重试时必须把图片按模型上次给的 `rotation_correction` 物理转正再问——**每次重试都依赖上一次的反馈**，天然只能串行。
- **非旋转错误路径（串行 + 队列空时并行）**：截断、解析失败等。重试之间彼此独立，不必等前一个结果；但默认仍串行，只有当内部 vLLM 的队列**空了**（GPU 闲着）才把剩余重试一次性并行发射——省下来的算力不花白不花，同时不影响整体吞吐。

#### 4.2.2 核心流程

```text
process_page(页)
  ├─ 首次尝试 attempt=0, rotation=0
  ├─ 成功（is_valid 且 is_rotation_valid）→ 返回
  ├─ 若 is_rotation_valid=False ────── 旋转路径
  │    cumulative_rotation = rotation_correction % 360
  │    for attempt in 1..MAX_RETRIES-1:            # 串行
  │        带 cumulative_rotation 重试
  │        成功 → 返回
  │        又报旋转 → cumulative_rotation += 新correction (mod 360)
  │    循环耗尽：最后一个 is_valid 的结果 → 返回
  │    否则 → failed_pages+1，返回 make_fallback_result
  └─ 否则（None 或 is_valid=False）── 非旋转路径
       for i, attempt in 1..MAX_RETRIES-1:          # 先串行
           重试（rotation 不变）
           成功 → 返回
           若 vllm 队列空 且 还有剩余尝试:
               并行发射全部剩余尝试
               谁先成功用谁，其余 cancel → 返回
       循环耗尽：最后一个 is_valid 的结果 → 返回
       否则 → failed_pages+1，返回 make_fallback_result
```

累计旋转角是对 360 取模的整数加法：

\[ \theta_{k+1} = (\theta_k + c_k) \bmod 360, \quad c_k \in \{0, 90, 180, 270\} \]

其中 \( c_k \) 是第 \( k \) 次响应里模型给出的 `rotation_correction`。

#### 4.2.3 源码精读

**重试次数的确定**（[pipeline.py:L289-L291](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L289-L291)）：`MAX_RETRIES` 来自命令行（默认 8），`retry_attempts = range(1, 8) = [1..7]`——首次尝试编号是 0，所以**总共至多 8 次尝试 = 1 次首试 + 7 次重试**，与温度表的 8 个元素一一对应：

```python
MAX_RETRIES = args.max_page_retries
retry_attempts = list(range(1, MAX_RETRIES))
cumulative_rotation = 0
```

**首试与成功判定**（[pipeline.py:L296-L305](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L296-L305)）：成功要同时满足 `is_valid`（没截断）与 `is_rotation_valid`（页面是正的）；同时**无论成败**，只要模型报了旋转角就先记下来，供旋转路径使用。指标名 `finished_on_attempt_0` 记录「一次过」的页数：

```python
result = await try_single_page_with_backoff(args, ..., attempt=0, rotation=cumulative_rotation)
if result is not None and not result.response.is_rotation_valid:
    cumulative_rotation = result.response.rotation_correction % 360
if result is not None and result.is_valid and result.response.is_rotation_valid:
    metrics.add_metrics(**{"completed_pages": 1, "finished_on_attempt_0": 1})
    ...
    return result
```

**旋转路径：串行反馈重试**（[pipeline.py:L307-L332](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L307-L332)）：每次重试把**累计**旋转角传进去（注意 L320 是 `(cumulative_rotation + result.response.rotation_correction) % 360`，模型可能连续报两次 90°）；循环耗尽后若最后一次至少 `is_valid`，仍把它作为答案返回——「歪着读出来的文本」好过「没有文本」；再不行才计 `failed_pages` 并走兜底：

```python
for attempt in retry_attempts:
    result = await try_single_page_with_backoff(args, ..., attempt, cumulative_rotation)
    if result is not None and result.is_valid and result.response.is_rotation_valid:
        ... return result
    if result is not None:  # Another rotation correction needed
        cumulative_rotation = (cumulative_rotation + result.response.rotation_correction) % 360
```

**非旋转路径：队列空时的并行发射**（[pipeline.py:L334-L363](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L334-L363)）：默认仍逐次串行；每次失败后检查全局变量 `vllm_queued_requests`（由内部 vLLM 看护协程解析服务器日志更新，见 [pipeline.py:L876-L880](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L876-L880)）。队列为 0 说明 GPU 无事可做——多半是任务收尾、只剩这些「钉子户」页面——此时把剩余尝试全部 `create_task` 并行发出，用 `asyncio.as_completed` 收第一个成功者并 `cancel` 其余：

```python
remaining_attempts = retry_attempts[i + 1 :]
if remaining_attempts and vllm_queued_requests == 0:
    logger.info(f"Queue empty, firing {len(remaining_attempts)} parallel retries ...")
    tasks = [asyncio.create_task(try_single_page_with_backoff(args, ..., a, ...))
             for a in remaining_attempts]
    for coro in asyncio.as_completed(tasks):
        result = await coro
        if result is not None and result.is_valid and result.response.is_rotation_valid:
            for t in tasks:
                t.cancel()
            metrics.add_metrics(**{"completed_pages": 1, "finished_on_parallel_retry": 1})
            ... return result
    break  # Parallel attempts exhausted
```

一个值得注意的细节：`vllm_queued_requests` 只在**内部 vLLM 模式**下被赋值（初始值 `None`，见 [pipeline.py:L82](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L82)）。用外部 `--server` 时它永远是 `None`，`None == 0` 为 False，并行重试**永远不会触发**——对按 token 计费的外部 API 来说，并行轰炸重试只会烧钱，这个守卫恰好是正确的。

**尾部：备胎与兜底**（[pipeline.py:L365-L375](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L365-L375)）：两条路径共用同一段收尾——最后一次尝试若至少 `is_valid`（比如旋转始终没纠对、但文本完整），返回它；否则 `failed_pages` 计数并调用 `make_fallback_result`：

```python
if result is not None and result.is_valid:
    metrics.add_metrics(**{"completed_pages": 1, f"finished_on_attempt_{MAX_RETRIES}": 1})
    ... return result
logger.error(f"Failed {pdf_orig_path}-{page_num} after {MAX_RETRIES} attempts")
metrics.add_metrics(failed_pages=1)
...
return make_fallback_result(pdf_orig_path, pdf_local_path, page_num)
```

**调用方视角**（[pipeline.py:L548-L562](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L548-L562)）：`process_pdf` 为每页创建一个 `process_page` 任务（TaskGroup 同生共死），收齐后断言所有 `is_valid`——兜底结果的 `is_valid=True` 保证了这个断言恒过；随后统计 fallback 页占比，超过 `--max_page_error_rate` 就**整篇丢弃**（u2-l8 展开）。也就是说：单页永远「成功」，质量淘汰推迟到整篇粒度。

**运行结束后如何验证重试策略有效**（[pipeline.py:L1498-L1510](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1498-L1510)）：main() 收场时打印「Pages finished by attempt number」——按尝试序号统计完成页数与累计百分比，`finished_on_attempt_0` 占比越高说明模型与提示词越可靠。

#### 4.2.4 代码实践

**实践目标**：把两条重试路径整理成对照表（本讲规格中指定的实践任务），检验你是否真正读懂了编排逻辑。

**操作步骤**：

1. 通读 [pipeline.py:L281-L375](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L281-L375)，对照下表逐格核对，把「依据行号」一栏替换为你自己的话。
2. 参考答案表如下：

| 维度 | 旋转错误路径 | 非旋转错误路径 |
|---|---|---|
| 触发条件 | 首试结果 `is_rotation_valid=False`（L308） | 首试返回 `None` 或 `is_valid=False`（落到 L334） |
| 重试方式 | **纯串行**（每次依赖上次反馈，L311-L312） | 默认串行；`vllm_queued_requests == 0` 时把剩余尝试**全部并行**发射（L345-L350） |
| 每次重试变化什么 | 图片被物理旋转 `cumulative_rotation`（累加取模，L320）后再问 | 图片不变，只升温度（attempt→温度表） |
| 温度随 attempt 变化 | 两路径相同：0.1, 0.1, 0.2, 0.3, 0.5, 0.8, 0.9, 1.0（L164-L165 查表） | 同左 |
| 成功判据 | `is_valid` **且** `is_rotation_valid`（L314） | `is_valid` **且** `is_rotation_valid`（L338） |
| 循环耗尽后的备胎 | 最后一个 `is_valid` 的结果（L323-L326） | 最后一个 `is_valid` 的结果（L366-L369） |
| 最终兜底 | `make_fallback_result`（pdftotext，L332） | `make_fallback_result`（pdftotext，L375） |
| 特有指标 | `finished_on_attempt_{N}` | 额外有 `finished_on_parallel_retry`（L358） |

**需要观察的现象**：两条路径的成功判据、温度策略、兜底行为完全一致，差异只在「重试之间是否有依赖」——有依赖（旋转）必须串行，无依赖（截断/解析）可以在 GPU 空闲时并行。

**预期结果**：你能不看源码复述出这张表，并能回答「为什么旋转路径不做并行优化」——因为第 k+1 次的入参（旋转角）依赖第 k 次的输出，并行无意义。

#### 4.2.5 小练习与答案

**练习 1**：一个页面模型连续两次报 `rotation_correction=90`，第三次重试时图片会被转多少度？

**答案**：270°。累计公式为 \((0+90+90) \bmod 360 = 180\) 是**第二次**重试的入参；若第二次又报 90，则第三次入参是 \((90+90+90) \bmod 360 = 270\)。代码里每次都把新反馈累加到 `cumulative_rotation` 上（L320），而不是直接用最新一次的值。

**练习 2**：为什么非旋转路径「队列空才并行」而不是从头就并行？

**答案**：从头并行会让 7 个重试瞬间涌入服务器，挤占其他页面的正常请求，高峰期反而降低总吞吐。队列空意味着 GPU 已经没有别的事做（典型场景：任务收尾只剩疑难页），此时并行重试的边际成本为零。这是一个「不伤害吞吐的前提下压缩长尾延迟」的调度技巧。

**练习 3**：`finished_on_attempt_5` 这个指标名会在什么情况下被写入？

**答案**：某页在第 6 次尝试（attempt 编号 5）才满足 `is_valid 且 is_rotation_valid` 时，两条路径的提前返回分支都会写入 `finished_on_attempt_5`（L315、L339），main() 收场时用它打印按尝试次数的分布表。

### 4.3 make_fallback_result 与指数退避：两层兜底

#### 4.3.1 概念说明

兜底有两层，职责不同：

- **连接层兜底 `try_single_page_with_backoff`**：服务暂时不可达时**等一等再试**。指数退避（exponential backoff）是分布式系统的经典模式：失败后按 \( 10 \cdot 2^n \) 秒等待，给对端恢复时间，避免雪上加霜。
- **内容层兜底 `make_fallback_result`**：模型 8 次都搞不定这一页时，退回传统工具 pdftotext 直接抽取 PDF 内嵌文本。对扫描件这通常得到空文本（图上没有文字层），但对「数字原生 PDF + 模型反复截断」这类页面，它至少能交出可用的文本。

设计哲学：**单页永不阻塞整篇、整篇永不阻塞整个任务**。fallback 页会被打上 `is_fallback=True` 标记，质量淘汰延后到 `process_pdf` 按比例裁决。

#### 4.3.2 核心流程

退避等待时间随失败次数指数增长：

\[ t_n = 10 \cdot 2^n \text{ 秒}, \quad n = 0, 1, \ldots, 9 \]

即 10s → 20s → 40s → 80s → 160s → 320s → 640s → 1280s → 2560s → 5120s（累计约 2.8 小时）。10 次全部失败则判定服务已死，`sys.exit(1)` 终止整个进程——继续跑只会产出垃圾。

#### 4.3.3 源码精读

**指数退避包装**（[pipeline.py:L253-L278](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L253-L278)）：只捕获 `try_single_page` 特意上抛的三类连接异常，其余结果（含 `None`）原样透传——它修的是「连不上」，不是「答错了」：

```python
MAX_BACKOFF_ATTEMPTS = 10
for backoff_count in range(MAX_BACKOFF_ATTEMPTS):
    try:
        return await try_single_page(args, pdf_orig_path, pdf_local_path, page_num, attempt, rotation)
    except (ConnectionError, OSError, asyncio.TimeoutError) as e:
        sleep_delay = 10 * (2**backoff_count)
        logger.warning(f"... Backoff {backoff_count + 1}/{MAX_BACKOFF_ATTEMPTS}, sleeping {sleep_delay}s")
        await asyncio.sleep(sleep_delay)
logger.error(f"Max backoff attempts reached for {pdf_orig_path}-{page_num}, terminating job")
sys.exit(1)
```

**pdftotext 兜底结果**（[pipeline.py:L233-L250](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L233-L250)）：调用 u4-l1 将精读的 `get_anchor_text`（`pdf_engine="pdftotext"`）抽取该页内嵌文本作为 `natural_text`；元数据全部填「中性值」（语言未知、无表格无图表、旋转合法）；token 计数为 0；最关键的两个标记——`is_fallback=True` 供下游统计淘汰，`is_valid=True` 让 `process_pdf` 的断言恒过：

```python
def make_fallback_result(pdf_orig_path: str, pdf_local_path: str, page_num: int) -> PageResult:
    return PageResult(
        pdf_orig_path, page_num,
        PageResponse(
            natural_text=get_anchor_text(pdf_local_path, page_num, pdf_engine="pdftotext"),
            primary_language=None, is_rotation_valid=True, rotation_correction=0,
            is_table=False, is_diagram=False,
        ),
        input_tokens=0, output_tokens=0,
        is_fallback=True, is_valid=True,
    )
```

**兜底之后发生什么**（[pipeline.py:L557-L562](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L557-L562)）：`process_pdf` 统计 fallback 页占比，超过 `--max_page_error_rate` 就丢弃整篇文档（不写 done flag，理论上下次重跑还会再试它一次）：

```python
num_fallback_pages = sum(page_result.is_fallback for page_result in page_results)
if num_fallback_pages / num_pages > args.max_page_error_rate:
    logger.error(f"Document {pdf_orig_path} has {num_fallback_pages} fallback pages ... discarding document.")
```

#### 4.3.4 代码实践

**实践目标**：亲手触发一次兜底，观察 fallback 结果长什么样；并画出退避曲线。

**操作步骤**（示例代码，需要本地装有 poppler；仓库根目录下运行）：

```python
# fallback_demo.py —— 示例代码
from olmocr.pipeline import make_fallback_result

# 用仓库自带的测试 PDF（数字原生文件，pdftotext 能抽出文字层）
result = make_fallback_result("tests/gnarly_pdfs/edgar.pdf", "tests/gnarly_pdfs/edgar.pdf", 1)
print("is_fallback:", result.is_fallback)
print("is_valid   :", result.is_valid)
print("tokens     :", result.input_tokens, result.output_tokens)
print("text 预览  :", (result.response.natural_text or "")[:200])

# 退避曲线（不真正 sleep，只打印）
total = 0
for n in range(10):
    delay = 10 * 2**n
    total += delay
    print(f"backoff {n+1:>2}/10: sleep {delay:>5}s")
print(f"累计等待: {total}s ≈ {total/3600:.1f} 小时")
```

**需要观察的现象**：`is_fallback=True` 且 `is_valid=True` 同时成立；`natural_text` 是 pdftotext 抽出的原始内嵌文本（没有 markdown 结构，可能夹着换行噪声）；token 计数为 0；退避延迟每轮翻倍。

**预期结果**：edgar.pdf 第 1 页能抽出非空文本（它是数字原生 PDF）；若换成纯扫描件（如 `edgar_image.pdf`），`natural_text` 可能为空或近乎为空——这正是兜底「保底不保质」的体现（具体文本内容待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：退避循环 10 次全失败后为什么 `sys.exit(1)` 杀掉整个进程，而不是给这一页记 fallback 后继续？

**答案**：连接错误意味着**推理服务本身**挂了或不可达，不是这一页的问题。继续跑下去所有页都会走同样的 10 轮退避然后全部 fallback，产出的整批数据都是 pdftotext 垃圾。快速失败（fail fast）让人工介入重启服务更划算——work_queue 的 done flag 机制保证重启后能断点续跑。

**练习 2**：兜底结果为什么把 `is_valid` 设为 True？这是不是「作弊」？

**答案**：不是。这里的 `is_valid` 语义是「该页已有可交付的结果、流水线可以继续」，而不是「模型转写合格」。真正的质量裁决由 `is_fallback` 标记驱动，在 `process_pdf` 里按 `max_page_error_rate` 比例执行。两个布尔量分工明确：前者管控制流，后者管质量流。

**练习 3**：扫描版 PDF 走到兜底会得到什么？这暴露了什么局限？

**答案**：扫描件的页面只是图片、没有文字层，pdftotext 抽出的 `natural_text` 为空字符串（front matter 解析后为 None）。也就是说兜底对「模型最难的扫描件」恰恰无能为力——这类页面最终以空文本进入 Dolma 输出，或把整篇的 fallback 比例推过阈值而被丢弃。

## 5. 综合实践

**任务：写一个离线「重试编排模拟器」，复现 `process_page` 的两条路径。**

不连接任何服务器，用假函数模拟 `try_single_page` 的三种返回（成功 / 旋转错误 / 截断失败），把 `process_page` 的编排骨架抄下来跑通，观察两条路径的走向。以下为示例代码：

```python
# retry_sim.py —— 示例代码：离线复现 process_page 的决策骨架
import asyncio
from olmocr.pipeline import TEMPERATURE_BY_ATTEMPT

class FakePageResponse:
    def __init__(self, is_rotation_valid=True, rotation_correction=0):
        self.is_rotation_valid = is_rotation_valid
        self.rotation_correction = rotation_correction

class FakeResult:
    def __init__(self, attempt, is_rotation_valid=True, rotation_correction=0, is_valid=True):
        self.attempt = attempt
        self.response = FakePageResponse(is_rotation_valid, rotation_correction)
        self.is_valid = is_valid

async def fake_try(attempt, rotation, script):
    """按剧本返回：script[attempt] = "ok" | "rot90" | "invalid" """
    act = script[attempt] if attempt < len(script) else "ok"
    temp = TEMPERATURE_BY_ATTEMPT[min(attempt, len(TEMPERATURE_BY_ATTEMPT) - 1)]
    print(f"  attempt={attempt} rotation={rotation:>3} temperature={temp} -> {act}")
    await asyncio.sleep(0.01)
    if act == "ok":
        return FakeResult(attempt)
    if act == "rot90":
        return FakeResult(attempt, is_rotation_valid=False, rotation_correction=90)
    return FakeResult(attempt, is_valid=False)  # 模拟截断

async def simulate(script, label):
    print(f"\n== {label} ==")
    cumulative_rotation, MAX = 0, 8
    result = await fake_try(0, cumulative_rotation, script)
    if result is not None and result.response.is_rotation_valid is False:
        cumulative_rotation = result.response.rotation_correction % 360
    if result is not None and result.is_valid and result.response.is_rotation_valid:
        print("  -> 首试成功"); return
    if result is not None and not result.response.is_rotation_valid:   # 旋转路径（串行）
        for attempt in range(1, MAX):
            result = await fake_try(attempt, cumulative_rotation, script)
            if result.is_valid and result.response.is_rotation_valid:
                print(f"  -> 旋转路径在 attempt={attempt} 成功"); return
            cumulative_rotation = (cumulative_rotation + result.response.rotation_correction) % 360
        print("  -> 旋转重试耗尽 -> make_fallback_result"); return
    for attempt in range(1, MAX):                                     # 非旋转路径（串行简化版）
        result = await fake_try(attempt, cumulative_rotation, script)
        if result.is_valid and result.response.is_rotation_valid:
            print(f"  -> 非旋转路径在 attempt={attempt} 成功"); return
    print("  -> 重试耗尽 -> make_fallback_result")

asyncio.run(simulate(["rot90", "rot90", "ok"],       "旋转两次后转正"))
asyncio.run(simulate(["invalid", "invalid", "ok"],   "截断两次后成功"))
asyncio.run(simulate(["rot90"] * 8,                  "旋转始终纠不正"))
```

**操作步骤**：

1. 保存并运行 `python retry_sim.py`。
2. 对照真实源码 [pipeline.py:L296-L375](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L296-L375)，核对你抄写的骨架与真实实现的差异（模拟器省略了退避包装、信号量与并行分支）。
3. 修改剧本（例如 `["invalid"]*7 + ["ok"]`、`["rot90","invalid","ok"]`），预测输出后再运行验证。
4. 思考：第三个剧本「旋转始终纠不正」里，每次尝试的 `rotation` 入参是多少？（答案：90 → 180 → 270 → 0 → 90 → … 循环，因为每次都累加 90 并对 360 取模。）

**预期结果**：四组剧本分别走通「首试旋转→串行纠偏→成功」「串行升温→成功」「旋转耗尽→兜底」等路径，打印出的温度序列与 4.1.4 的阶梯一致。

## 6. 本讲小结

- `try_single_page` 是「问一次」的最小单元，用三种返回形态区分失败：`None`（请求失败）、`is_valid=False`（截断 `finish_reason≠stop` 或 `total_tokens>16384`）、`is_rotation_valid=False`（页面歪了）；连接类异常特意上抛给退避包装。
- `TEMPERATURE_BY_ATTEMPT = [0.1, 0.1, 0.2, 0.3, 0.5, 0.8, 0.9, 1.0]`：先用低温度拿最稳的答案，反复失败则逐步升温打破退化重复；8 个元素与 `--max_page_retries` 默认值 8 一一对应。
- 旋转错误走**串行反馈重试**：每次把累计旋转角 \((\theta + c) \bmod 360\) 物理作用到渲染 PNG 上再问，因为下一次的入参依赖上一次的反馈，无法并行。
- 非旋转错误默认串行，但当内部 vLLM 队列空（`vllm_queued_requests == 0`，外部 API 下恒为 None 故永不触发）时，把剩余重试全部并行发射、取先到者。
- `try_single_page_with_backoff` 以 \(10 \cdot 2^n\) 秒指数退避处理连接错误，10 次失败即 `sys.exit(1)` 快速失败；`make_fallback_result` 用 pdftotext 保底，`is_fallback=True` 标记交给 `process_pdf` 按 `max_page_error_rate` 做整篇淘汰。
- 两层兜底共同实现「单页永不阻塞」：内容层保证每页必有结果，连接层保证服务故障时宁可停机不出垃圾。

## 7. 下一步学习建议

下一讲（u2-l7）将钻进本讲反复出现的 `apost`——项目为什么弃用 httpx/aiohttp、手写 `asyncio.open_connection` 级别的 HTTP/1.1 客户端，以及 content-length / chunked 两种响应体读取方式。随后 u2-l8 讲解 `PageResult` 汇聚成 Dolma 文档的输出侧；若你对 `vllm_queued_requests` 的来源好奇，u3-l3 会精读 vLLM 服务器的日志解析与生命周期管理。建议按 u2-l7 → u2-l8 的顺序推进。
