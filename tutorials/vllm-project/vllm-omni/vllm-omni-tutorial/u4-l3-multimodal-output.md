# 多模态输出处理：MultimodalOutputProcessor 与张量累积

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚 vLLM-Omni 的 **AR 阶段**是如何把每一步产生的多模态张量（音频、图像、latent）从「逐步小块」最终拼成「一整块」交给下游 stage 的。
2. 读懂 `MultimodalOutputProcessor.process_outputs` 按 `output_type` 路由、并把多模态张量交给 `OmniRequestState` 累积的流程。
3. 理解「**列表累积（deferred concatenation）→ 最终 `torch.cat` 拼接**」这条两段式管线的设计动机与具体代码点。
4. 知道 `OmniModelRunnerOutput.pooler_output` 如何把每请求的隐藏态与多模态输出一并承载，并被下游 scheduler 消费。

本讲是 AR 模块的第三篇，承接 [u4-l2（AR 调度器）](u4-l2-ar-schedulers.md)：调度器决定「这一步给请求分配多少 token」，而本讲回答「这一步产出的多模态数据怎么收集、什么时候拼好、交给谁」。

## 2. 前置知识

在进入源码前，先建立三个直觉。本讲对初学者不熟悉的概念都会展开。

### 2.1 为什么多模态输出需要「累积」

vLLM 原生只服务**文本**：每生成一个 token，decoder 产出一个 token id，逐个拼成句子。这种「一 token 一吐」的模型，输出天然是「离散小片段」，直接拼字符串即可。

但全模态模型不是这样。以 Qwen3-Omni 的 **Code2wav / Talker** 阶段为例，它每解码一步产出的不是文字，而是一小段**音频 latent（隐向量）**——可以理解成「一小段声音的压缩表示」。一段完整的语音要几十甚至上百步才能凑齐。问题来了：

- 不能每步都把这一小段送出去单独播放（会断断续续、无法拼接）。
- 也不能等所有步跑完一次性返回（那就失去了流式能力）。

所以需要一个**中间容器**：每来一小段就 append 进去，等某个时机（比如本步结束、或累计到一定量）再把所有小段**拼成一条完整的张量**交给下游。这就是本讲的核心——**张量累积（tensor accumulation）**。

> 类比：像用快递箱收货。每来一件商品（一小段张量）就丢进箱子（list），等发货时再把箱子里的东西整整齐齐码好（`torch.cat`）。如果每来一件就重新码一次整个货架（每次都 `torch.cat` 全部历史），效率极差——这正是代码注释里反复强调要避免的「O(n²) 反复 cat」。

### 2.2 两个关键术语

- **EngineCoreOutput（引擎核输出）**：vLLM v1 里「一次推理迭代」对**每个请求**产出的原始结果，包含 `new_token_ids`、`finish_reason` 等。vLLM-Omni 扩展出 `OmniEngineCoreOutput`，多挂了 `multimodal_output` 字段。
- **RequestState（请求状态）**：OutputProcessor 内部为每个请求维护的「账本」，跨多次迭代累积这个请求的产出。vLLM-Omni 扩展出 `OmniRequestState`，多挂了多模态累积容器 `mm_accumulated`。

### 2.3 「数据面 vs 控制面」的分工

回忆 [u4-l2](u4-l2-ar-schedulers.md) 的请求流：

```
InputProcessor → Scheduler → Worker → ModelRunner → OutputProcessor
```

本讲聚焦最右端的 **OutputProcessor**。它的职责是：把 ModelRunner 产出的「一批 EngineCoreOutput」加工成面向用户/下游 stage 的 `RequestOutput`。对文本走 vLLM 原生路径（detokenize 还原成字符串），对多模态走 vLLM-Omni 自己的累积与拼接路径。两路并存、互不干扰，是本讲反复出现的结构。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| `vllm_omni/outputs/output_processor.py` | **主角**。定义 `MultimodalOutputProcessor`（路由+累积入口）与 `OmniRequestState`（每请求累积账本）。 |
| `vllm_omni/outputs/mm_outputs.py` | 定义 `MultimodalPayload`（累积容器，实现「list 累积 → cat 拼接」）与 `MultimodalCompletionOutput`（带多模态的补全输出）。 |
| `vllm_omni/outputs/output_modality.py` | 定义 `OutputModality` 标志位与 `TensorAccumulationStrategy`（按模态选「怎么拼」）。 |
| `vllm_omni/outputs/multimodal_accumulation.py` | DELTA 流式模式下的快照替换、抽取（drain）与「非末块音频」判定等辅助逻辑。 |
| `vllm_omni/outputs/__init__.py` | 定义 `OmniModelRunnerOutput`（含 `pooler_output` / `multimodal_outputs`）。 |
| `vllm_omni/worker/gpu_ar_model_runner.py` | AR runner 的 `sample_tokens`，是 `pooler_output` 的**生产侧**。 |
| `vllm_omni/core/sched/omni_ar_scheduler.py` | scheduler 的**消费侧**，从 `pooler_output` 取出每请求载荷。 |
| `vllm_omni/engine/__init__.py` | 定义 wire 线缆类型 `OmniEngineCoreOutput`（带 `multimodal_output` 字段）。 |

> 永久链接统一使用 HEAD `900a7f0813d0482811b0e4dfd3cf7deabbe2429f`。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1** `MultimodalOutputProcessor`：按 `output_type` 路由 + 多模态/文本分流。
- **4.2** `OmniRequestState`：跨步累积张量、最终 `torch.cat` 拼接。
- **4.3** `pooler_output`：每请求隐藏态与多模态输出的统一承载。

### 4.1 MultimodalOutputProcessor：路由与累积入口

#### 4.1.1 概念说明

`MultimodalOutputProcessor` 继承自 vLLM 的 `VLLMOutputProcessor`。它的核心定位用一个词概括：**薄包装（thin wrapper）**——文本/池化的处理全部委托给父类（super），自己只额外干一件事：**捕获每一步的多模态张量，喂给请求状态去累积**。

类文档把数据流说得很清楚（4 步）：

1. 每个 `EngineCoreOutput` 若带 `multimodal_output`，就交给 `OmniRequestState.add_multimodal_tensor()` 累积。
2. 文本反tokenize 交给父类。
3. 结束时 `_consolidate_multimodal_tensors()` 按 strategy 拼接。
4. `_new_completion_output()` 返回 `MultimodalCompletionOutput`。

#### 4.1.2 核心流程

主入口是 `process_outputs(engine_core_outputs)`，它把一批输出分成两路：

```
对每个 EngineCoreOutput (eco):
  ├─ 若该 eco 带 multimodal_output：
  │     计算 mm_type（来自 eco.output_type 或 stage 默认）
  │     req_state.add_multimodal_tensor(mm_output, mm_type)   # 累积（所有路径都做）
  │
  ├─ 判断路由：
  │     若该请求无 detokenizer 且无 pooling_output（即「纯多模态 generation 阶段」）
  │         → 放进 mm_only_outputs 列表，本地处理（绕开父类）
  │     否则
  │         → 放进 upstream_outputs 列表，交给父类处理（文本/池化）
  │
  ├─ 本地处理 mm_only_outputs（_process_mm_only_outputs）
  └─ super().process_outputs(upstream_outputs) 处理文本，再把两路结果合并返回
```

为什么要分流？因为上游 vLLM 的 `process_outputs` 里有一句 `assert detokenizer is not None`。像 Talker / Code2wav 这类**没有 tokenizer 的 generation 阶段**，如果走父类会直接断言失败。所以 omni 把这些「纯多模态」输出挑出来本地处理，避开父类的硬性假设。

#### 4.1.3 源码精读

**路由主循环**——`process_outputs` 的分流与累积：

[vllm_omni/outputs/output_processor.py:492-539](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/output_processor.py#L492-L539)：对每个 `eco`，先用 `getattr(eco, "multimodal_output", None)` 捕获多模态张量并调用 `add_multimodal_tensor`；再按「有无 detokenizer / pooling」把它分到 `mm_only_outputs` 或 `upstream_outputs` 两路。注意第 511-516 行——**累积是无条件发生的**，无论后续走哪条路径，多模态张量都会被收进 `OmniRequestState`。

关键的两行（515-516）：

```python
mm_type = getattr(eco, "output_type", None) or default_mm_type
req_state.add_multimodal_tensor(mm_output, mm_type)
```

这里 `default_mm_type` 来自下面的工具函数——它把处理器的 `output_modality` 标志位翻译成小写字符串：

[vllm_omni/outputs/output_processor.py:36-55](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/output_processor.py#L36-L55)：`_modality_to_type_string` 按优先级 `AUDIO → IMAGE → LATENT → text` 选出字符串。这就是「按 output_type 路由」的依据。

**「按 output_type 路由」的两层来源**——处理器初始化时接收两种等价输入：

[vllm_omni/outputs/output_processor.py:365-401](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/output_processor.py#L365-L401)：`__init__` 既接收字符串 `engine_core_output_type`（向后兼容 stage_init_utils），也接收类型安全的 `output_modality` 标志。字符串会被 `OutputModality.from_string` 转成标志位。这个 stage 级的默认模态，就是当单个 `eco` 没带 `output_type` 时的兜底。

> 小结：模态标签有两个来源——**(a)** 单条输出自带的 `eco.output_type`（更细，可逐输出变化）；**(b)** 整个 stage 在初始化时声明的 `output_modality`（更粗，作为兜底默认）。两者「或」起来决定这一小段张量被当作 audio / image / latent 处理，进而决定它**怎么被拼接**（见 4.2）。

**本地处理「纯多模态」输出**：

[vllm_omni/outputs/output_processor.py:541-601](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/output_processor.py#L541-L601)：`_process_mm_only_outputs` 为没有 detokenizer 的 generation 阶段单独造 `OmniRequestOutput`，核心调用 `req_state.make_request_output(...)`（4.2 详述）。它还处理一种特殊情况：当音频流式输出标记 `tts_is_last_chunk==0`（「我还没说完」）时，即使本步 `finish_reason` 非空也不结束请求（575-579 行），保证跨段音频连续。

#### 4.1.4 代码实践

**实践目标**：亲手验证「按 output_type 路由 + 累积」这条最小链路，不动真实模型。

**操作步骤**（源码阅读 + 单测运行型实践）：

1. 打开 `tests/engine/test_output_processor.py`，找到 `test_init_empty_dict`，它验证「初始状态 `mm_accumulated` 为空」。阅读其构造 `_make_state` 与 `_DEFAULT_STATE_KWARGS` 的方式，理解 `OmniRequestState` 的最小构造。

2. 本地运行该测试（CPU 即可，marker 是 `core_model`+`cpu`）：

   ```bash
   pytest tests/engine/test_output_processor.py -k "test_init_empty_dict" -m "core_model and cpu"
   ```

3. 在 `process_outputs` 的第 516 行（`req_state.add_multimodal_tensor(mm_output, mm_type)`）后**脑内跟踪**：如果 `mm_type` 分别为 `"audio"` 与 `"image"`，后续 `_consolidate_multimodal_tensors` 会选哪个 `TensorAccumulationStrategy`？

**需要观察的现象 / 预期结果**：测试应通过（断言初始 `mm_accumulated == {}`）；阅读 `output_modality.py` 的 `get_accumulation_strategy` 可验证：`audio` → `CONCAT_LAST`，`image`/`latent` → `CONCAT_DIM0`。若本地无 GPU，此步为纯 CPU 单测，可直接跑通；若环境缺失依赖，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `process_outputs` 要把输出分成 `mm_only_outputs` 和 `upstream_outputs` 两路，而不是全交给父类？

> **参考答案**：上游 vLLM 的 `process_outputs` 在 `pooling_output is None` 时会 `assert detokenizer is not None`。而 Talker/Code2wav 这类 generation 阶段没有 tokenizer（detokenizer 为 None），走父类会断言失败。所以 omni 把「无 detokenizer 且无 pooling」的纯多模态输出挑出来本地处理，绕开这条硬假设；有 detokenizer 的文本输出才走父类。

**练习 2**：`mm_type` 的取值有哪两个来源？优先级如何？

> **参考答案**：(1) 单条输出自带的 `eco.output_type`；(2) stage 在处理器初始化时声明的默认 `output_modality`（经 `_modality_to_type_string` 转字符串）。代码用 `getattr(eco, "output_type", None) or default_mm_type`——单条自带优先，缺失时回退到 stage 默认。

---

### 4.2 OmniRequestState：张量累积与拼接

#### 4.2.1 概念说明

`OmniRequestState` 继承自 vLLM 的 `RequestState`，是 OutputProcessor 为「每个请求」维护的跨步账本。它在父类基础上新增了两个多模态字段：

- `mm_type: str | None`：这个请求输出的是什么模态（`"audio"`/`"image"`/`"latent"`/...）。
- `mm_accumulated: MultimodalPayload`：累积容器，跨多步收集多模态张量。

核心机制是**两段式管线**：

1. **累积阶段（每步）**：`add_multimodal_tensor` 把新来的张量 **append 进 list**，不做 `torch.cat`。
2. **拼接阶段（按需）**：`_consolidate_multimodal_tensors` 把所有 list **一次性 `torch.cat`** 成单张量。

为什么不在每步就 cat？因为如果每来一小段都和历史整体 cat 一次，N 步会触发 N 次 cat、每次复制越来越多数据，复杂度是 \(O(n^{2})\)。先攒成 list（每步只 `append`，\(O(1)\)），最后一次 cat（\(O(n)\)），整体降到 \(O(n)\)。源码注释把这一点写得很直白：「Uses list-based deferred concatenation to avoid O(n²) repeated torch.cat calls.」

\[ \text{朴素法总复制量} = \sum_{k=1}^{n} k = \frac{n(n+1)}{2} = O(n^{2}), \quad \text{延迟 cat 法总复制量} = n \cdot s + n \cdot s = O(n) \]

（其中 \(s\) 是单块大小；延迟法只在 append 与最终 cat 各搬一次。）

#### 4.2.2 核心流程

```
每一步（process_outputs 调用一次）：
  add_multimodal_tensor(payload, mm_type)
     ├─ payload 归一化：MultimodalPayload.from_raw(payload, modality_key)
     │     · 裸 tensor → {modality_key: tensor}
     │     · dict → 按 key 分类，producer key（"hidden"/"model_outputs"）重映射为 modality_key
     │     · 全部 _to_cpu() 搬到 CPU
     ├─ replace_snapshot_keys：把「逐块快照型」元数据（如 tts_is_last_chunk）替换为最新值
     └─ mm_accumulated = mm_accumulated.merged_with(incoming)
           → _append_entries：已有 tensor + 新 tensor → 合并成 [旧, 新] 的 list
                              已是 list + 新 tensor → append
                              非张量 → 直接替换（保最新）

触发拼接的时机（make_request_output 内）：
  若 finished 或 非 DELTA 模式（CUMULATIVE / FINAL_ONLY）：
     _consolidate_multimodal_tensors()
        ├─ modality → TensorAccumulationStrategy（get_accumulation_strategy）
        │     · audio → CONCAT_LAST（沿最后一维拼，即时间维）
        │     · image/latent → CONCAT_DIM0（沿第 0 维拼）
        ├─ consolidate_tensors：对每个 key 的 list 调 torch.cat(strategy)
        └─ consolidate_metadata：元数据保最新（取 list[-1]）
```

#### 4.2.3 源码精读

**累积入口**——`add_multimodal_tensor`：

[vllm_omni/outputs/output_processor.py:98-118](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/output_processor.py#L98-L118)：归一化 → 替换快照键 → `merged_with`。注意第 109-111 行 `modality_key = self.mm_type or "hidden"`——若该请求从未声明模态，就用 `"hidden"` 作 key（典型场景：AR runner 产出原始 hidden states）。

**list 累积的真正实现**——`_append_entries`：

[vllm_omni/outputs/mm_outputs.py:64-78](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/mm_outputs.py#L64-L78)：这就是「延迟 cat」的核心——两个 tensor 相遇时，不 cat，而是**合并成 list** `[existing, new_value]`；之后再来就 `append`。非张量值（标量、字符串）直接 `store[key] = new_value` 替换。

[vllm_omni/outputs/mm_outputs.py:145-157](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/mm_outputs.py#L145-L157)：`merged_with` 把 incoming 合并到自身。注释特意提醒调用者要用返回值：`accumulated = accumulated.merged_with(incoming)`。

**最终拼接**——`_consolidate_multimodal_tensors` + `consolidate_tensors`：

[vllm_omni/outputs/output_processor.py:120-140](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/output_processor.py#L120-L140)：先由 `mm_type` 字符串解析出 `OutputModality` 标志（解析失败回退 TEXT），再用 `get_accumulation_strategy` 选策略，最后 `consolidate_tensors` + `consolidate_metadata`。

[vllm_omni/outputs/mm_outputs.py:159-177](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/mm_outputs.py#L159-L177)：`consolidate_tensors` 先把误入 `.tensors` 的标量元数据（如 `sr`/`sample_rate`）搬到 `.metadata`（避免 0-d 张量触发 cat 报错），再对每个「list of tensor」调 `_consolidate_tensor_list` 做真正 cat。

**策略选择**——`get_accumulation_strategy`：

[vllm_omni/outputs/output_modality.py:102-124](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/output_modality.py#L102-L124)：四种策略的含义与选择规则：

| 策略 | 行为 | 用于 |
|------|------|------|
| `CONCAT_DIM0` | `torch.cat(list, dim=0)` | image / latent（沿 batch 维拼） |
| `CONCAT_LAST` | `torch.cat(list, dim=-1)` | audio（沿时间维拼，波形首尾相连） |
| `APPEND_LIST` | 仅 append 不 cat | （定义备用） |
| `REPLACE` | 取 `list[-1]` | 元数据（保最新） |

`get_accumulation_strategy` 的优先级：`AUDIO → CONCAT_LAST`，否则 `IMAGE/LATENT → CONCAT_DIM0`，默认 `CONCAT_DIM0`。

**带容错的真正 cat**——`_cat_tensors` 与 `_consolidate_tensor_list`：

[vllm_omni/outputs/mm_outputs.py:29-61](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/mm_outputs.py#L29-L61)：`_cat_tensors` 按 strategy 落到 `torch.cat(..., dim=0/-1)` 或取末元素。`_consolidate_tensor_list` 在 cat 抛 `RuntimeError` 时有兜底：对 audio 尝试沿末维重试，再不行把每块 `reshape(-1)` 拍平后 cat；对非 audio 则 warn 并保留最后一块。这说明**音频块的形状不一定完全一致**（变长语音），需要更宽容的拼接。

**触发拼接的时机**——`make_request_output`：

[vllm_omni/outputs/output_processor.py:144-233](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/output_processor.py#L144-L233)：第 195-196 行是关键判断——

```python
if finished or not is_delta:
    self._consolidate_multimodal_tensors()
```

含义：**请求结束时**（finished）或**非 DELTA 模式**（CUMULATIVE 每步都要给消费者一个完整张量）才拼接。DELTA 流式模式下中间步不拼（只攒），既省开销也支持「逐步吐增量」。

[vllm_omni/outputs/output_processor.py:235-304](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/output_processor.py#L235-L304)：`_new_completion_output` 在有累积数据时，先 `unflatten_payload` 把点号键（如 `hidden_states.layer_0`）还原成嵌套 dict，再包成 `MultimodalCompletionOutput`。对 DELTA 模式还会 `drain_delta_payload` 把「客户端面向」的增量键（audio 等）抽干，使下一步只看到新累积的数据——这是流式增量输出的实现。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：为一个跨多步产出音频 latent 的 AR 阶段，画出张量从「逐步累积为 list」到「最终 `torch.cat` 拼接」的**完整时序**，并定位每一步对应的代码行。

**操作步骤**：

1. **阅读单测** `tests/engine/test_multimodal_accumulation.py` 中的 `test_chunk_accumulation_policy_replaces_snapshots_and_drains_delta_state`（见文件第 17-49 行）。它构造了两块 `audio` 张量（`[1.0]`、`[2.0]`），先 `replace_snapshot_keys` 再 `merged_with`，最后 `drain_delta_payload`。这是「两块音频 → list 累积」的最小可读样本。

2. **画出时序图**（请你在笔记上手绘或用文本表示，下面给出模板）：

   ```
   step k=1:  eco.multimodal_output = {"audio": T1(shape=[1,100])}
              │ process_outputs (output_processor.py:516)
              ▼
              add_multimodal_tensor(T1, "audio")            (:98)
              │ from_raw → MultimodalPayload(tensors={"audio":T1})
              │ merged_with                                 (mm_outputs.py:145)
              ▼
              mm_accumulated.tensors["audio"] = T1          (mm_outputs.py:74-75 首次)

   step k=2:  eco.multimodal_output = {"audio": T2(shape=[1,100])}
              ▼ 同上
              _append_entries: existing=T1(Tensor), new=T2(Tensor)
              mm_accumulated.tensors["audio"] = [T1, T2]    (mm_outputs.py:74-75 变 list)

   step k=3..N: 每步 append，list 越来越长                (mm_outputs.py:72-73)

   finished=True（或 CUMULATIVE 每步）:
              make_request_output (:195-196) → _consolidate_multimodal_tensors (:120)
              │ modality="audio" → get_accumulation_strategy → CONCAT_LAST  (output_modality.py:120)
              ▼ consolidate_tensors (mm_outputs.py:159)
                _consolidate_tensor_list → _cat_tensors([T1,T2,...], CONCAT_LAST)
                mm_accumulated.tensors["audio"] = torch.cat(list, dim=-1)   (mm_outputs.py:35)
   ```

3. **定位代码行**：在图中每条 `(:NNN)` 处，用编辑器跳到对应行号确认逻辑。重点核对三处：
   - 「首次相遇变 list」：[mm_outputs.py:74-75](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/mm_outputs.py#L74-L75)
   - 「策略选择 audio→CONCAT_LAST」：[output_modality.py:120-121](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/output_modality.py#L120-L121)
   - 「最终 cat」：[mm_outputs.py:34-35](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/mm_outputs.py#L34-L35)

**需要观察的现象 / 预期结果**：图中应清晰呈现「张量先以 list 形式增长（O(1) append），到 finished/CUMULATIVE 才一次性 `torch.cat(dim=-1)`」。如果改 `mm_type` 为 `"image"`，第 3 处应变成 `dim=0`。

> 说明：本实践为「源码阅读 + 单测佐证」型，不依赖 GPU。若想在本地真正跑通张量拼接，可参考下面的综合实践（第 5 节）用 Python 直接构造 `MultimodalPayload`。

#### 4.2.5 小练习与答案

**练习 1**：假设某请求走了 50 步音频累积。若改用「每步都 `torch.cat` 历史」的朴素做法，相比延迟 cat，多做了大约多少倍的张量复制？

> **参考答案**：朴素法总复制量为 \(1+2+\dots+50=1275\)（块·单位），延迟法为 \(50\)（append 不复制，仅最终 cat 一次搬 50 块）。朴素法约为延迟法的 \(1275/50\approx25.5\) 倍，且随步数平方增长——这正是延迟 cat 的价值。

**练习 2**：`make_request_output` 里 `if finished or not is_delta` 这个条件，对 **DELTA 流式** 和 **CUMULATIVE** 两种模式分别意味着什么？

> **参考答案**：DELTA 模式下 `is_delta=True`，只有 `finished` 时才 consolidate——中间步只攒 list 不拼接，省开销并允许逐步吐增量；CUMULATIVE 模式下 `not is_delta=True`，**每一步**都 consolidate，保证消费者每次都拿到一条完整拼接好的张量。

**练习 3**：为什么 `_consolidate_tensor_list` 对 `audio` 单独有「沿末维重试 → 拍平重试」的兜底，对其它模态却没有？

> **参考答案**：语音是变长时序数据，不同块的时间长度（末维）可能不一致，直接 `torch.cat(dim=-1)` 偶尔会因形状不齐报错；而非 audio 模态（image/latent）的块形状通常规整一致，cat 不会失败。所以音频需要更宽容的拼接（沿末维或拍平），其它模态失败时直接 warn 并保留最后一块即可。

---

### 4.3 pooler_output：每请求隐藏态与多模态输出的承载

#### 4.3.1 概念说明

前两模块讲的是 **OutputProcessor 侧**（把 EngineCoreOutput 加工成 RequestOutput）。本模块换到 **ModelRunner 侧**——AR runner 怎么把「每请求的隐藏态 + 多模态输出」打包送出去。

回忆 [u4-l1](u4-l1-ar-module-overview.md)：`GPUARModelRunner` 采用**两阶段执行**——`execute_model()` 只跑前向把中间产物暂存，`sample_tokens()` 才采样并暴露隐藏态。这些暴露出去的载荷挂在 `OmniModelRunnerOutput.pooler_output`（每请求一项，按 `req_index` 索引）和 `multimodal_outputs`（同结构）两个字段上。

`pooler_output` 名字借自 vLLM 的「pooling（池化/embedding）」语义：vLLM 原生用它承载 embedding 向量。vLLM-Omni **复用这个槽位**，把它变成「每请求送下游 stage 的语义载荷容器」——里面装的可能是隐藏态，也可能是多模态输出 dict。

#### 4.3.2 核心流程

```
GPUARModelRunner.sample_tokens (ModelRunner 侧，生产者)
  ├─ 为每个下游请求 rid 构建 payload（hidden_states 切片 + 多模态 dict）
  ├─ pooler_output.append(flatten_payload(payload))      # 拍平成 dict[str, tensor]
  └─ 装进 OmniModelRunnerOutput(pooler_output=..., multimodal_outputs=...)
        │
        ▼ (跨进程 ZMQ/msgpack)
OmniARScheduler.update_from_output (Scheduler 侧，消费者)
  ├─ pooler_outputs = model_runner_output.pooler_output
  ├─ pooler_output = pooler_outputs[req_index]            # 按请求取回
  └─ 装进 OmniEngineCoreOutput(pooling_output=pooler_output, multimodal_output=mm_output)
        │
        ▼ (进入本讲 4.1/4.2 的 OutputProcessor)
```

注意 `pooler_output`（runner 侧的 dict 载荷）在 scheduler 侧被赋给 `OmniEngineCoreOutput.pooling_output` 字段（借用 vLLM 原生字段名），而 `multimodal_outputs` 被赋给 `multimodal_output` 字段——后者正是 4.1 里 `getattr(eco, "multimodal_output", None)` 取的那个值。两端字段名不同，靠 scheduler 这一步做适配。

#### 4.3.3 源码精读

**承载结构**——`OmniModelRunnerOutput`：

[vllm_omni/outputs/__init__.py:39-71](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/outputs/__init__.py#L39-L71)：继承 vLLM `ModelRunnerOutput`，新增 `multimodal_outputs`、`inter_stage_outputs`（stage 间载荷）、`kv_extracted_req_ids`、`omni_connector_output`。`pooler_output` 字段继承自父类（`list[Any]`，按 req_index 索引）。classmethod `with_kv_conn_output_only` 展示了空请求批量构造的用法（仅传 KV connector 输出时）。

**生产侧**——AR runner 构建 pooler_output：

[vllm_omni/worker/gpu_ar_model_runner.py:1832-1865](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/worker/gpu_ar_model_runner.py#L1832-L1865)：为 `req_ids_output_copy` 里每个 rid 循环——不在下游集合里的填空 dict `{}`（保持下标对齐），在的则调 `_build_omni_pooler_payload` 取该请求的 hidden_states 切片与多模态 dict，再 `flatten_payload` 拍平后 append。注意第 1860-1865 行的 `partition_payload_list`：在「async chunk」开启时把 pooler 分成「stage 间（inter）」与「客户端（client）」两份；否则两份指向同一对象（legacy 行为）。最终塞进 `OmniModelRunnerOutput`（1884 行起）。

> 文件开头第 1-4 行的模块 docstring 一语道破设计意图：「Exposes per-request hidden representations via ModelRunnerOutput.pooler_output and also outputs sampled tokens.」

**消费侧**——scheduler 取回并适配字段名：

[vllm_omni/core/sched/omni_ar_scheduler.py:340](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_ar_scheduler.py#L340)：`pooler_outputs = model_runner_output.pooler_output` 一次性取出整批。

[vllm_omni/core/sched/omni_ar_scheduler.py:457-486](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_ar_scheduler.py#L457-L486)：第 465 行 `pooler_output = pooler_outputs[req_index] if pooler_outputs else None` 按请求取回；第 483-486 行若该请求带 pooling_params 且有 pooler_output，则直接 `FINISHED_STOPPED`（pooling 一旦有输出就停，这是 vLLM 原生语义）。

[vllm_omni/core/sched/omni_ar_scheduler.py:552-562](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_ar_scheduler.py#L552-L562)：只要有 token / mm_output / pooler_output / kv_transfer / stopped 之一，就构造一条 `OmniEngineCoreOutput`，其中 `pooling_output=pooler_output`、`multimodal_output=mm_output`——**这就是把 runner 侧的 `pooler_output` 适配成 EngineCoreOutput 字段名的关键一行**。

**wire 线缆类型**——`OmniEngineCoreOutput`：

[vllm_omni/engine/__init__.py:127-133](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/__init__.py#L127-L133)：继承 `EngineCoreOutput`，新增 `multimodal_output`（专用多模态通道）、`is_segment_finished`（流式输入段结束标志）、`new_prompt_len_snapshot`。注释明确：`pooling_output` 继承自父类保留 vLLM 原生 embedding 语义，多模态走独立通道 `multimodal_output`，两者并存不冲突。

#### 4.3.4 代码实践

**实践目标**：验证「runner 生产 → scheduler 消费」的字段名适配，理解 `pooler_output` 与 `multimodal_output` 的关系。

**操作步骤**（源码阅读型实践）：

1. 打开 `gpu_ar_model_runner.py:1832-1857`，确认每个 rid 的 payload 被 `flatten_payload` 拍平后 append 进 `pooler_output` 列表。
2. 跳到 `omni_ar_scheduler.py:465`，确认 scheduler 用 `pooler_outputs[req_index]` 按下标取回。
3. 跳到 `omni_ar_scheduler.py:561-562`，确认这条 `pooler_output` 被赋给 `OmniEngineCoreOutput.pooling_output`，而 `mm_output` 被赋给 `multimodal_output`。
4. 回到 `output_processor.py:513`，确认 OutputProcessor 用 `getattr(eco, "multimodal_output", None)` 读取——闭环完成。

**需要观察的现象 / 预期结果**：你应该能在脑中画出一条「`pooler_output`(runner) → `pooling_output`(EngineCoreOutput) → `add_multimodal_tensor` 的 `payload` 参数(OutputProcessor)」的字段流转链，并意识到名字换了三次但数据是同一份。注意：`pooler_output`（隐藏态语义载荷）与 `multimodal_output`（多模态张量）是**两条并行的载荷**，不要混淆。

#### 4.3.5 小练习与答案

**练习 1**：`OmniModelRunnerOutput.pooler_output` 和 `multimodal_outputs` 分别承载什么？为什么要分成两个字段？

> **参考答案**：`pooler_output` 承载每请求的「语义载荷」（主要是隐藏态切片，供下游 stage 作为输入 embedding 使用），`multimodal_outputs` 承载每请求的「多模态产出 dict」（image/audio/latent 张量）。分开是因为它们语义不同：前者是「跨阶段传递的中间表示」，后者是「面向用户/下游 stage 的最终多模态内容」，且在 scheduler 侧被适配到 `EngineCoreOutput` 的不同字段（`pooling_output` vs `multimodal_output`）。

**练习 2**：从 runner 到 OutputProcessor，同一个多模态 dict 经过了哪些字段名？

> **参考答案**：runner 侧叫 `multimodal_outputs`（列表，按 req_index） → scheduler 取出后赋给 `OmniEngineCoreOutput.multimodal_output` → OutputProcessor 用 `getattr(eco, "multimodal_output", None)` 读取并交给 `add_multimodal_tensor`。三段名字不同，靠 scheduler 的赋值做适配。

---

## 5. 综合实践

**任务**：用纯 Python（不需要 GPU/模型）模拟一次「跨 3 步的音频 latent 累积与拼接」，把本讲三个模块串起来。

**示例代码**（请保存为 `accumulate_demo.py` 单独运行，**这是示例代码，不是项目原有文件**）：

```python
# 示例代码：演示 MultimodalPayload 的 list 累积 → torch.cat 拼接
import torch
from vllm_omni.outputs.mm_outputs import MultimodalPayload
from vllm_omni.outputs.output_modality import (
    OutputModality, get_accumulation_strategy, TensorAccumulationStrategy,
)

# 模拟 AR 阶段逐步产出的 3 块音频 latent（形状不一致，模拟变长语音）
chunks = [
    {"audio": torch.arange(0, 4, dtype=torch.float)},     # step1: [0,1,2,3]
    {"audio": torch.arange(4, 7, dtype=torch.float)},     # step2: [4,5,6]
    {"audio": torch.arange(7, 10, dtype=torch.float)},    # step3: [7,8,9]
]

# 1) 累积阶段：每步 merged_with，内部把 audio 收成 list
accumulated = MultimodalPayload()
for i, chunk in enumerate(chunks):
    incoming = MultimodalPayload.from_raw(chunk, "audio")
    accumulated = accumulated.merged_with(incoming)
    print(f"step{i+1} 后 audio 类型: {type(accumulated.tensors['audio']).__name__}, "
          f"值: {accumulated.tensors['audio']}")

# 2) 选策略：audio → CONCAT_LAST
strategy = get_accumulation_strategy(OutputModality.AUDIO)
print("策略:", strategy)  # TensorAccumulationStrategy.CONCAT_LAST

# 3) 拼接阶段：一次性 torch.cat
accumulated.consolidate_tensors(strategy)
print("最终拼接结果:", accumulated.tensors["audio"])
# 期望: tensor([0,1,2,3,4,5,6,7,8,9])  —— 3 块沿时间维首尾相连
```

**操作步骤**：

1. 在已安装 vLLM-Omni 的环境（按 [u1-l2](u1-l2-installation.md) 完成源码安装）中保存并运行 `python accumulate_demo.py`。
2. 观察每一步 `audio` 的类型变化：第一次是 `Tensor`，第二次起变成 `list`，`consolidate_tensors` 后变回单个 `Tensor`。
3. 把 `OutputModality.AUDIO` 改成 `OutputModality.IMAGE`，重新运行，观察策略变成 `CONCAT_DIM0`；并把 chunk 形状改成二维（如 `torch.arange(0,4).reshape(2,2)`）以看出 `dim=0` 与 `dim=-1` 的区别。

**需要观察的现象 / 预期结果**：
- 累积阶段 `audio` 从 `Tensor` 变 `list`，长度随步数增长。
- 拼接后 audio 得到一条首尾相连的 `[0..9]`。
- 改成 image 模态后策略变 `CONCAT_DIM0`，对二维张量沿第 0 维（batch）堆叠。

**若无法运行**：标注「待本地验证」。即便不运行，对照源码（`mm_outputs.py:74-75` 变 list、`mm_outputs.py:159` 拼接、`output_modality.py:120` 选策略）也能完整复现这条时序。

---

## 6. 本讲小结

- `MultimodalOutputProcessor` 是 vLLM `OutputProcessor` 的**薄包装**：文本/池化委托父类，自己只负责捕获每步多模态张量并按 `output_type` 路由进 `OmniRequestState`；为避开父类 `assert detokenizer is not None`，把无 detokenizer 的「纯多模态 generation 输出」挑出来本地处理。
- 模态标签有两层来源：单条 `eco.output_type`（细）优先，stage 初始化声明的 `output_modality`（粗）兜底，二者「或」决定张量如何被拼接。
- `OmniRequestState` 用**两段式管线**处理跨步多模态输出：每步 `add_multimodal_tensor` → `merged_with` → `_append_entries` 把张量**收成 list**（O(1) append，避免 O(n²) 反复 cat）；到 `finished` 或 CUMULATIVE 步才 `_consolidate_multimodal_tensors` 一次性 `torch.cat`。
- 拼接策略由模态决定：audio → `CONCAT_LAST`（沿时间维），image/latent → `CONCAT_DIM0`（沿 batch 维），元数据走 `REPLACE`（保最新）；audio 还配有「沿末维重试 → 拍平」的形状容错。
- `OmniModelRunnerOutput.pooler_output`（runner 生产）与 `multimodal_outputs` 是两条并行载荷，经 scheduler 适配成 `OmniEngineCoreOutput.pooling_output` / `multimodal_output`，最终被 OutputProcessor 的 `add_multimodal_tensor` 消费，闭环跨进程的多模态数据传递。
- DELTA 流式模式额外靠 `drain_delta_payload`（抽干客户端面向键）与 `is_non_final_delta_audio_chunk`（非末块不结束）保证跨段音频连续与增量输出。

## 7. 下一步学习建议

- **横向对比 Diffusion 输出**：本讲讲的是 AR 阶段的多模态输出处理；Diffusion 阶段（如 Qwen-Image）的输出去到 `OmniRequestOutput.from_diffusion`（`outputs/__init__.py:180`），不走本讲的累积管线。建议进入 [U5（Diffusion 模块）](u5-l1-diffusion-engine.md)，对照两种输出路径的差异。
- **深入流式细节**：若你对 DELTA 流式、`tts_is_last_chunk`、`duplex_epoch` 等音频流式标记感兴趣，可精读 `vllm_omni/outputs/multimodal_accumulation.py` 与 [u6-l3（流式输出与实时/全双工）](u6-l3-streaming-realtime.md)。
- **回看调度衔接**：若想再确认 `pooler_output` 如何影响调度决策（pooling 一有输出即 `FINISHED_STOPPED`），回看 [u4-l2](u4-l2-ar-schedulers.md) 的 `OmniARScheduler.update_from_output`。
- **测试佐证**：运行 `tests/engine/test_output_processor.py` 与 `tests/engine/test_multimodal_accumulation.py` 全部用例（marker `core_model`+`cpu`），用断言固化本讲描述的行为。
