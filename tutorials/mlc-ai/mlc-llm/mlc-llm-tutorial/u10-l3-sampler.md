# 采样器：CPU 与 GPU

## 1. 本讲目标

模型前向计算吐出的是每个词的 **logits**（未归一化的分数），而真正「决定下一个 token 是什么」的是**采样器（Sampler）**。采样器把 logits 转概率、做 top-p / temperature 控制、再抛骰子挑出一个 token；在推测解码里，它还要扮演「裁判」，逐个裁决小模型起草的 draft token 是否被大模型接受。

本讲聚焦 `cpp/serve/sampler/` 目录，学完后你应当掌握：

- 采样器的**统一抽象接口**：四个核心方法分别承担「重归一化 / 采样 / 校验 draft」。
- **CPU 采样器**与 **GPU 采样器**两套完整实现的工作原理与差异。
- `Sampler::SupportGPUSampler` 如何根据 device 类型决定走哪条路径，以及两种实现在**延迟与显存**上的取舍。
- `BatchVerifyDraftTokensWithProbAfterTopP` 在推测解码「校验」阶段的用途与拒绝采样原理。

本讲承接 [u9-l4 模型运行时与 FunctionTable](u9-l4-model-runtime-functiontable.md)：那里讲过 model lib 通过**函数名字符串契约**把采样相关 kernel 暴露给 C++ 引擎，本讲就打开这些函数被调用的那一侧——采样器。

## 2. 前置知识

### 2.1 从 logits 到 token 的两步

模型最后一层输出 logits 向量 \(\mathbf{z}\in\mathbb{R}^{V}\)（\(V\) 为词表大小）。采样流程通常是：

1. **softmax 转概率**：\(p_i = \dfrac{e^{z_i/T}}{\sum_j e^{z_j/T}}\)，其中 \(T\) 是 temperature（温度）。
2. **截断与采样**：常用 top-p（nucleus）截断——只保留累计概率达到 `top_p` 的高概率词，再把剩余概率重新归一化，最后按概率随机抽一个。

> 名词解释：
> - **temperature（温度）**：\(T\to 0\) 时分布趋于 one-hot（贪心 argmax），\(T\to\infty\) 时趋于均匀。MLC 中 `temperature≈0` 直接走 argmax。
> - **top-p / nucleus**：保留概率累计到 `p` 的最小词集合，丢弃长尾。
> - **multinomial（多项采样）**：给定概率向量，按概率随机抽一个，等价于「转轮盘」。

### 2.2 推测解码的拒绝采样原理（一图理解）

推测解码让小模型先「起草」一串 draft token，大模型只需对这些 draft 做一次批量校验。校验用**拒绝采样（rejection sampling）**保持与大模型独立采样**完全相同的分布**。设某位置大模型给 draft 词的概率为 \(p\)、小模型给的概率为 \(q\)：

- 若 \(p \geq q\)：**直接接受**该 draft token。
- 若 \(p < q\)：以概率 \(p/q\) 接受；否则拒绝。
- 一旦拒绝：从残差分布 \(\max(p - q,\,0)\) 归一化后**重新采样**一个 token，并终止本序列的继续校验。

接受概率即 \(\min(1,\, p/q)\)。这套规则保证最终输出分布严格等于大模型单独采样，详见 [u10-l4 推测解码动作链](u10-l4-speculative-decoding.md)。

### 2.3 TVM 对象系统速记

MLC 的 C++ 采样器沿用 TVM 的对象模型：`SamplerObj` 是带引用计数的**实现基类**，`Sampler` 是它的 **对象引用（ObjectRef）** 句柄。对外只暴露 `Sampler`，内部多态分发到 `CPUSampler` 或 `GPUSampler`。这一点与 [u9-l4](u9-l4-model-runtime-functiontable.md) 里 `ModelObj`/`Model` 是同一套模式。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [cpp/serve/sampler/sampler.h](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/sampler.h) | 采样器抽象接口 `SamplerObj`（4 个纯虚方法）与工厂 `Sampler`（含 `SupportGPUSampler` 判定）。 |
| [cpp/serve/sampler/cpu_sampler.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/cpu_sampler.cc) | CPU 采样实现：手写 top-p 算法、重归一化、draft 校验，多线程并行。 |
| [cpp/serve/sampler/gpu_sampler.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/gpu_sampler.cc) | GPU 采样实现：通过 FunctionTable 调用编译期附加的采样 kernel，双流掩盖拷贝延迟。 |
| [cpp/serve/model.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.cc) | `CreateSampler`：按 device 类型在 CPU/GPU 采样器间二选一。 |
| [cpp/serve/function_table.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc) | 当支持 GPU 采样时，按名从 model lib 加载 6 个采样 kernel 句柄。 |
| [cpp/serve/data.h](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/data.h) | `SampleResult` / `TokenProbPair` 数据结构。 |

## 4. 核心概念与源码讲解

### 4.1 采样器接口：一个抽象基类 + 四个动作

#### 4.1.1 概念说明

采样器对外是一个**抽象基类** `SamplerObj`，定义了「给定一批概率分布，产出 token」的全部能力。它故意被设计成与设备无关——调用方（引擎里的各个 Action）只持有 `Sampler` 句柄，不关心底下是 CPU 还是 GPU。

`SamplerObj` 暴露 **4 个纯虚方法**，恰好覆盖 LLM 推理的两类需求：

| 方法 | 用途 | 典型调用方 |
| --- | --- | --- |
| `BatchRenormalizeProbsByTopP` | 对一批概率分布按各自 top-p 做「截断 + 重归一化」 | decode / verify 前的预处理 |
| `BatchSampleTokensWithProbBeforeTopP` | 输入概率**尚未**做 top-p，采样器内部应用 top-p 再采样 | 单步普通采样 |
| `BatchSampleTokensWithProbAfterTopP` | 输入概率**已经**做过 top-p，直接采样 | decode 主路径（先重归一化再采） |
| `BatchVerifyDraftTokensWithProbAfterTopP` | 推测解码里**裁决 draft token** 接受/拒绝 | `BatchVerify` Action |

注意 `BeforeTopP` / `AfterTopP` 这对命名：它表达的是「输入的概率分布是否已经被 top-p 处理过」。引擎的 decode 路径会先调 `BatchRenormalizeProbsByTopP` 把 top-p 应用好，再调 `...AfterTopP` 完成采样——这样 top-p 重归一化只做一次，但同一批概率可以被多次采样。

#### 4.1.2 核心流程

一个关键设计是 `sample_indices`（采样下标）。方法的契约写明：

\[
\text{result}[i] = \text{sample\_from}\bigl(\text{prob}[\text{sample\_indices}[i],\ :],\ \text{cfg}[i]\bigr)
\]

即第 \(i\) 个采样结果，是从第 `sample_indices[i]` 行概率分布里抽出来的。这是一层**间接索引**，允许多个采样结果共享同一行概率分布（例如对同一 logits 多次采样），也让调用方不必把概率张量重新排布成「一行一个采样」。

每个方法的输入输出形状约定（`probs_on_device` 形状为 `(n, V)`，\(n\) 为概率分布条数）：

```
输入: probs_on_device (n, V)  +  sample_indices[k]  +  generation_cfg[k]  +  rngs[k]
        │
        ├─ (可选) BatchRenormalizeProbsByTopP  → 重归一化后的 probs (n, V)
        │
        └─ BatchSampleTokensWithProb{Before|After}TopP  → std::vector<SampleResult> 长度 k
              每个 SampleResult = { sampled_token_id: (id, prob),
                                    top_prob_tokens: [(id,prob), ...] 最多 5 个 }
```

#### 4.1.3 源码精读

抽象基类与四个纯虚方法签名在头文件中定义：

[cpp/serve/sampler/sampler.h:35-53](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/sampler.h#L35-L53) —— `SamplerObj` 继承 `tvm::ffi::Object`，`BatchRenormalizeProbsByTopP` 是第一个纯虚方法，注释说明返回的概率「在 GPU 采样器下驻留设备、在 CPU 采样器下驻留主机」。

[cpp/serve/sampler/sampler.h:69-96](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/sampler.h#L69-L96) —— `BeforeTopP` 与 `AfterTopP` 两个采样方法的签名，二者只差一个 `top_p_applied` 的隐含语义。

[cpp/serve/sampler/sampler.h:98-123](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/sampler.h#L98-L123) —— 推测解码校验方法 `BatchVerifyDraftTokensWithProbAfterTopP`。注意它的入参里没有 `sample_indices`，而是用 `cum_verify_lengths`（每条序列累计 draft 长度）来切分批次，并多了 `draft_output_tokens`、`token_tree_parent_ptr`、`draft_probs_on_device` 三个推测解码专属参数。返回值是 `pair`：每条序列的接受结果数组 + 每条序列「最后接受的树节点下标」。

工厂与判定在 `Sampler` 引用类上：

[cpp/serve/sampler/sampler.h:136-159](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/sampler.h#L136-L159) —— `Sampler` 提供 `CreateCPUSampler` / `CreateGPUSampler` 两个静态工厂，以及设备判定 `SupportGPUSampler`。

`SampleResult` 的结构很简单，是非 TVM 对象的普通结构体（不直接暴露给 Python）：

[cpp/serve/data.h:160-183](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/data.h#L160-L183) —— `TokenProbPair = pair<int32_t, float>`；`SampleResult` 含「采样到的 token 及其概率」与「top 概率词列表（用于 OpenAI 的 logprobs 字段，最多 5 个）」。

#### 4.1.4 代码实践

**实践目标**：理清采样器接口的「四方法 + 一判定」全貌，不运行代码，只读源码画图。

**操作步骤**：

1. 打开 [sampler.h](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/sampler.h)，在 `SamplerObj` 里数清楚四个 `virtual ... = 0` 纯虚方法，记下它们各自返回什么。
2. 打开 [batch_decode.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_decode.cc)，定位到第 168–171 行：decode 动作依次调 `BatchRenormalizeProbsByTopP` 再调 `BatchSampleTokensWithProbAfterTopP`。
3. 画一张调用关系图：`BatchDecode Action → 重归一化 → AfterTopP 采样`。

**需要观察的现象**：decode 路径用「AfterTopP」而非「BeforeTopP」，因为前面已经显式重归一化过；这说明 top-p 是一次预处理动作，与采样动作解耦。

**预期结果**：你能用一句话说清「BeforeTopP 与 AfterTopP 的区别就是输入概率是否已被 top-p 处理」，并能指出 decode 选 AfterTopP 是为了避免重复做 top-p。

**待本地验证**：无（纯源码阅读）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `BatchVerifyDraftTokensWithProbAfterTopP` 不接受 `sample_indices` 参数，而另外两个采样方法接受？

> **答案**：校验方法用 `cum_verify_lengths` 直接把概率张量按序列切成连续段，每段内的每个 draft 节点一一对应一行概率，是「自然一一映射」无需间接索引；而普通采样方法支持「多个采样结果共享同一行概率」，故需要 `sample_indices` 这层间接。

**练习 2**：`SampleResult` 为什么不做成 TVM 对象（不像 `RequestState` 那样）？

> **答案**：它只在 C++ 引擎内部流转、不需要直接跨 FFI 暴露给 Python，做成普通 `struct` 更轻量、避免对象系统开销（见 data.h 注释 "It's not a TVM object since it will not be used directly on Python side"）。

---

### 4.2 CPU 采样实现：手写 top-p 与拒绝采样

#### 4.2.1 概念说明

`CPUSampler` 的工作方式很「直白」：GPU 算完 logits 后，把概率分布**整片拷回 CPU**，然后在 CPU 上用纯 C++ 算法做 top-p 截断、采样、draft 校验。它的优势是不依赖任何编译期生成的采样 kernel——任何后端（包括不支持 GPU 采样的 CPU/OpenCL 等）都能跑；劣势是每一步都要把 \((n, V)\) 的概率张量从设备拷回主机，词表 \(V\) 通常几万，这是一笔不小的延迟。

CPU 实现的两个算法核心是：
- **`SampleTopPFromProb`**：一次「过滤 + 排序 + 转轮盘」的 top-p 采样，带大量提前退出优化。
- **`RenormalizeProbByTopP`**：原地重归一化概率分布。

#### 4.2.2 核心流程

**`SampleTopPFromProb` 的 top-p 采样**（输入一行概率 + 一个均匀随机数 \(u\in[0,1]\)）：

```
特判 top_p == 0      → 等价 argmax（贪心），边扫边提前退出
特判 top_p >= 1.0    → 纯多项采样：累加 p_i 直到累计 >= u 即返回
一般 0 < top_p < 1:
  1. 用阈值 cuttoff = top_p/1024 过滤出高概率词（鸽巢原理：至多 1024 个，通常 10~20 个）
  2. 对这少量词排序（降序）
  3. 累计求和到 top_p 得到核集合，记录 top_p_sum
  4. 在核集合上转轮盘：找首个累计概率 >= u * top_p_sum 的词
  若步骤 1 不足覆盖 top_p → 退回用 cuttoff=0 全量扫（罕见）
```

关键直觉：**先过滤再排序**，把需要对全词表 \(V\) 排序的 \(O(V\log V)\) 降到对几十个候选词排序。鸽巢原理保证阈值 `top_p/1024` 过滤后候选数 \(\leq 1024\)。

**`BatchVerifyDraftTokensWithProbAfterTopP` 的拒绝采样**（对一条序列的 draft 链逐位裁决）：

```
对每个 draft 位置 cur（大模型概率 p = prob[cur][draft_token]，小模型概率 q）:
  if p >= q:                      # 接受
      接受 draft_token，继续下一位
  else:
      r = uniform(0,1)
      if r < p / (q + eps):       # 以 p/q 概率接受
          接受 draft_token，继续下一位
      else:                        # 拒绝
          residual = max(prob[cur] - draft_prob[cur+1], 0)  # 残差分布
          归一化 residual，从中采一个新 token
          break                    # 拒绝后本序列终止校验
  若整条链全部接受 → 在最后一位再多采一个新 token（bonus token）
```

注意 CPU 版本只支持**链式（chain）draft**，即 `token_tree_parent_ptr` 必须是线性父链（代码用 `ICHECK` 强制）；树形 draft 只有 GPU 版本支持。

#### 4.2.3 源码精读

[cpp/serve/sampler/cpu_sampler.cc:35-70](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/cpu_sampler.cc#L35-L70) —— `SampleTopPFromProb` 的开头：先断言概率张量连续、float32、在 CPU 上；`top_p == 0` 分支边扫边维护 argmax，并用 `1 - sum_prob <= max_prob` 提前退出（剩余概率已不可能超过当前最大值）。

[cpp/serve/sampler/cpu_sampler.cc:84-163](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/cpu_sampler.cc#L84-L163) —— `sample_top_p_with_filter` lambda：过滤 → 排序 → 求核集合 → 转轮盘。注释点明「通常只需保留少数高概率元素」，并用 `uniform_sample < data[0].first / top_p` 这种短路判断直接返回 argmax。

[cpp/serve/sampler/cpu_sampler.cc:172-260](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/cpu_sampler.cc#L172-L260) —— `RenormalizeProbByTopP`：用预设的三档阈值 `{top_p/256, top_p/8192, 0}` 逐轮把元素分到 upper/lower 两个分区，upper 累计达到 top_p 即停；随后对 upper 排序、找出 boundary、把低于 boundary 的清零、最后对剩下的做归一化。

[cpp/serve/sampler/cpu_sampler.cc:332-373](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/cpu_sampler.cc#L332-L373) —— `CPUSampler::BatchRenormalizeProbsByTopP`：先 `CopyProbsToCPU` 把设备上的概率拷回主机，再对 `sample_indices` 去重（同一行概率只重归一化一次），最后用 `parallel_for_with_threading_backend` 多线程并行处理各行。

[cpp/serve/sampler/cpu_sampler.cc:426-503](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/cpu_sampler.cc#L426-L503) —— 拒绝采样主体：`p_value >= q_value` 直接接受；否则 `r < p_value/(q_value+eps_)` 概率接受；拒绝时用 `std::max(p_probs[j] - p_qdist[j], 0.0f)` 构造残差分布再归一化重采。循环外 `last_accepted_tree_node[i] = cur_token_idx` 记录最后接受位置。

[cpp/serve/sampler/cpu_sampler.cc:546-571](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/cpu_sampler.cc#L546-L571) —— `CopyProbsToCPU`：维护一块**可复用、倍增扩容**的 CPU 缓存 `probs_host_`，避免每次采样都重新分配大块内存；这正是 CPU 采样控制额外内存开销的关键。

#### 4.2.4 代码实践

**实践目标**：搞清 `temperature` 如何映射到 `top_p`，并验证「贪心 = top_p=0 特例」。

**操作步骤**：

1. 阅读 [cpu_sampler.cc:506-543](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/cpu_sampler.cc#L506-L543) 的 `BatchSampleTokensImpl`，定位这一行：
   ```cpp
   double top_p = top_p_applied
       ? 1.0f
       : (generation_cfg[i]->temperature < eps_ ? 0.0 : generation_cfg[i]->top_p);
   ```
2. 解读：当 `top_p_applied=true`（概率已重归一化）时 top_p 取 1.0（纯多项采样）；当 temperature 接近 0 时 top_p 取 0（走 argmax 特例）；否则用配置里的真实 top_p。
3. 在 [cpu_sampler.cc:52-70](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/cpu_sampler.cc#L52-L70) 确认 `top_p == 0` 分支确实是 argmax。

**需要观察的现象**：temperature≈0 与 top_p=0 走同一条 argmax 代码路径。

**预期结果**：你能解释「为什么 MLC 不单独写贪心解码，而是复用采样器」——把贪心当作 top-p 采样的退化情形，统一了一条代码路径。

**待本地验证**：无。

#### 4.2.5 小练习与答案

**练习 1**：`SampleTopPFromProb` 里先用 `top_p/1024` 过滤、失败后再用 `0.0` 全量扫。为什么大多数情况第一轮就够？

> **答案**：鸽巢原理——若一个词概率 \(< \text{top\_p}/1024\)，则 1024 个这样的词累计才勉强到 top_p，故高概率核集合几乎必然在阈值之上，过滤后候选数 ≤ 1024（实战通常 10~20）。只有当分布异常平坦时才需退回全量扫描。

**练习 2**：CPU 采样器为何坚持「拷回主机再做」而不是在设备上算？

> **答案**：CPU 采样器是兜底实现，要服务于**不支持 GPU 采样的后端**（如纯 CPU、OpenCL）。这些后端没有编译期附加的采样 kernel（见 4.4），只能在主机上用纯 C++ 完成；代价是 GPU 后端若误用 CPU 采样器会多一次 \((n,V)\) 的 D2H 拷贝。

---

### 4.3 GPU 采样实现：调用编译期 kernel + 双流掩盖延迟

#### 4.3.1 概念说明

`GPUSampler` 的策略与 CPU 截然相反：**尽量让采样发生在 GPU 上**，避免把整个 \((n, V)\) 概率张量拷回主机。为此它**不在 C++ 里写采样算法**，而是通过 `FunctionTable` 调用一批**在编译期由 `AttachGPUSamplingFunc` 附加进 model lib 的 GPU kernel**（见 [u8-l3 运行时函数附加 pass](u8-l3-attach-passes.md)）。

这些 kernel 以函数名字符串为契约（这正是 u9-l4 讲过的「名字符串胶水」），包括：`multinomial_from_uniform`、`argsort_probs`、`sample_with_top_p`、`sampler_take_probs`、`sampler_verify_draft_tokens`、`renormalize_by_top_p`。

GPU 采样器还有两个性能手段：
- **预分配辅助张量**：构造时一次性在 CPU 与 GPU 各开好 `uniform_samples`、`sample_indices`、`top_p` 等缓冲，避免每步采样重复分配。
- **双流（copy stream + compute stream）**：在 CUDA/ROCm 上单独开一条拷贝流，让「CPU→GPU 的小数据拷贝」与「GPU 上的采样计算」重叠，掩盖拷贝延迟。

此外，若环境支持 **FlashInfer** 的采样函数，short path 会优先调 FlashInfer 的 `parallel_sampling_from_prob`（要求 CUDA 且计算能力 ≥ 8.0）。

#### 4.3.2 核心流程

**普通采样 `BatchSampleTokensImpl → ChunkSampleTokensImpl → SampleOnGPU`** 分两条路：

```
                        ┌── need_top_p == false 且 need_prob_values == false (短路径)
                        │     直接 multinomial 采样：
SampleOnGPU ───────────┤       FlashInfer 可用 → flashinfer.parallel_sampling_from_prob
                        │       否则            → multinomial_from_uniform
                        │
                        └── 需 top_p 或需 prob 值 (长路径)
                              1. argsort_probs(probs) → (sorted_probs, sorted_indices)
                              2. 若 need_top_p: sample_with_top_p(sorted_probs, ..., top_p)
                                 否则         : 仍走 multinomial
                              3. 若 need_prob_values: sampler_take_probs(...) 取回 top 概率
```

> 「need_top_p」由 `CheckTopP` 判定：若所有请求 top_p 都等于 1.0（或 top_p 已应用），则无需 top-p，走短路径。`need_prob_values` 由是否请求 logprobs 决定。

**重归一化 `BatchRenormalizeProbsByTopP`** 走的是 `renormalize_by_top_p` kernel，并在 CPU 上预算 3 个「初始枢轴（pivot）」（`num_top_p_cutoff_pivots_=3`）拷到 GPU，供并行选择算法使用。

**draft 校验 `BatchVerifyDraftTokensWithProbAfterTopP`** 走 `sampler_verify_draft_tokens` kernel；与 CPU 版不同，**GPU 版支持树形 draft**：它在 CPU 上把 token tree 转成「first_child / next_sibling / parent_ptr」三数组拷到 GPU，kernel 内完成树形拒绝采样，最后再在 CPU 上回溯出每条序列接受的 token 列表。

#### 4.3.3 源码精读

[cpp/serve/sampler/gpu_sampler.cc:20-32](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/gpu_sampler.cc#L20-L32) —— `FlashInferSamplingAvailable`：要求 CUDA + 全局函数 `flashinfer.sampling.parallel_sampling_from_prob` 存在 + 计算能力主版本 ≥ 8（Ampere 及以上）。

[cpp/serve/sampler/gpu_sampler.cc:53-115](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/gpu_sampler.cc#L53-L115) —— `GPUSampler` 构造函数：从 `FunctionTable` 取出 6 个采样函数句柄，在 CPU/GPU 各预分配一批辅助张量；末尾对 CUDA/ROCm 额外创建 `copy_stream_`（计算流用默认流）。

[cpp/serve/sampler/gpu_sampler.cc:566-593](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/gpu_sampler.cc#L566-L593) —— `SampleOnGPU` 的**短路径**：既不需要 top-p 也不需要 prob 值时，直接 multinomial 采样，优先 FlashInfer。

[cpp/serve/sampler/gpu_sampler.cc:595-654](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/gpu_sampler.cc#L595-L654) —— `SampleOnGPU` 的**长路径**：先 `argsort_probs`，再按需 `sample_with_top_p` 或 multinomial，最后 `sampler_take_probs` 取回 top 概率值。

[cpp/serve/sampler/gpu_sampler.cc:34-47](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/gpu_sampler.cc#L34-L47) —— `CopyArray` 与 `SyncCopyStream`：双流拷贝与同步的原语，`SyncCopyStream` 在 `copy_stream==nullptr` 时直接返回（非 CUDA/ROCm 后端无独立拷贝流）。

[cpp/serve/sampler/gpu_sampler.cc:242-291](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/gpu_sampler.cc#L242-L291) —— draft 校验中「在 CPU 上构建 token tree 三数组 → 拷贝到 GPU → 调 `sampler_verify_draft_tokens`」的过程，体现了 GPU 版的树形 draft 能力。

#### 4.3.4 代码实践

**实践目标**：把「GPU 采样 = 编译期附加 kernel + FunctionTable 按名调用」这条链路在源码里走通。

**操作步骤**：

1. 打开 [function_table.cc:269-281](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L269-L281)，记下 6 个采样 kernel 的函数名字符串：`multinomial_from_uniform`、`argsort_probs`、`sample_with_top_p`、`sampler_take_probs`、`sampler_verify_draft_tokens`、`renormalize_by_top_p`。注意它们只在 `SupportGPUSampler(local_gpu_device)` 为真时才加载，且都用 `value_or(Function(nullptr))` 容错（可选函数）。
2. 打开 [gpu_sampler.cc:59-64](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/gpu_sampler.cc#L59-L64)，确认构造函数把这些句柄逐一从 `ft`（FunctionTable）取出来存为成员。
3. 回顾 [u8-l3](u8-l3-attach-passes.md) 讲过的 `AttachGPUSamplingFunc`：那正是把上述名字符串对应的 PrimFunc **附加进 IRModule** 的编译期 pass。

**需要观察的现象**：C++ 采样器与编译期 pass 之间**没有任何头文件共享**，唯一的接口就是这 6 个字符串——典型的「编译期↔运行期名字符串契约」。

**预期结果**：你能画出闭环：`AttachGPUSamplingFunc`（编译期附加）→ model lib 导出函数 → `FunctionTable` 按名加载 → `GPUSampler` 按名调用。

**待本地验证**：无。

#### 4.3.5 小练习与答案

**练习 1**：为什么 GPU 采样器要在 CUDA/ROCm 上单独开一条 `copy_stream`？

> **答案**：采样每步都需要把少量 CPU 数据（均匀随机数、sample_indices、top_p 值）拷到 GPU。若与计算共用默认流，拷贝会阻塞计算；开独立拷贝流并配合 `SyncCopyStream`，让拷贝与 GPU 采样计算**重叠执行**，掩盖拷贝延迟。

**练习 2**：短路径里 FlashInfer 与 `multinomial_from_uniform` 二选一，依据是什么？

> **答案**：`flashinfer_sampling_available_` 为真（CUDA + FlashInfer 全局函数存在 + 计算能力 ≥ 8.0）时优先用 FlashInfer 的 `parallel_sampling_from_prob`，它通常更高度优化；否则回退到 TVM 编译期附加的 `multinomial_from_uniform`。

---

### 4.4 CPU vs GPU 对比与 GPU 采样支持判定

#### 4.4.1 概念说明

到底用 CPU 还是 GPU 采样器，由一处静态方法一锤定音：`Sampler::SupportGPUSampler(device)`。它只看 device 类型是否在白名单里。判定结果在两个地方被消费：
- **model.cc 的 `CreateSampler`**：决定实例化 `GPUSampler` 还是 `CPUSampler`。
- **function_table.cc 的 `_InitFunctions`**：决定是否从 model lib 加载那 6 个采样 kernel 句柄（不支持就不加载，省内存也避免误用）。

这两个消费点必须**一致**——只有当 FunctionTable 真的加载了 kernel，GPUSampler 构造时才能取到非空句柄；而 GPUSampler 构造函数对前 4 个核心函数做了 `ICHECK(defined())` 强校验。

#### 4.4.2 核心流程

```
引擎启动 → ModelImpl::CreateSampler(device):
   ┌─ SupportGPUSampler(device) == true  → CreateGPUSampler(...)
   │     依赖: FunctionTable 已加载 6 个采样 kernel (function_table.cc:269)
   │     特点: 采样在 GPU 上完成，仅回传 token id 等少量结果
   │
   └─ SupportGPUSampler(device) == false → CreateCPUSampler(...)
         依赖: 无（纯 C++ 算法）
         特点: 把 (n, V) 概率整片拷回 CPU 再算
```

`SupportGPUSampler` 的白名单（见下方源码）：**CUDA、Vulkan、Metal** 返回 true；其余（CPU、OpenCL、ROCm 注：ROCm 走 kDLCUDA 别名所以通常算 CUDA、WebGPU）返回 false。

> 说明：WebGPU 在 u8-l3 里被 `AttachGPUSamplingFunc` 排除在 GPU 采样 kernel 之外（WebGPU 只附加 2 个函数），因此即便设备类型匹配，实际也会因为函数句柄缺失而退化为 CPU 采样行为——这正体现了「判定 + 句柄校验」的双重保险。

#### 4.4.3 源码精读

[cpp/serve/sampler/sampler.h:151-156](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/sampler.h#L151-L156) —— `SupportGPUSampler` 的全部实现：一个布尔表达式，白名单为 `kDLCUDA || kDLVulkan || kDLMetal`。

[cpp/serve/model.cc:1057-1065](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.cc#L1057-L1065) —— `CreateSampler` 的二选一分支：支持 GPU 采样时把 `max_num_sample`、`vocab_size`、`&this->ft_`、`device_` 传给 `CreateGPUSampler`；否则只传 trace_recorder 给 `CreateCPUSampler`（CPU 采样器不需要 FunctionTable）。

[cpp/serve/function_table.cc:269-281](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L269-L281) —— `_InitFunctions` 中按 `SupportGPUSampler` 守卫加载 6 个采样 kernel；全部用 `value_or(Function(nullptr))`，即「找不到就置空」。

#### 4.4.4 代码实践：判定与取舍对比（本讲主实践）

**实践目标**：说清 `SupportGPUSampler` 的判定逻辑，并对比 CPU/GPU 两种实现在**延迟与显存**上的取舍，指出 `BatchVerifyDraftTokensWithProbAfterTopP` 的用途。

**操作步骤**：

1. **判定逻辑**。读 [sampler.h:152-156](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/sampler.h#L152-L156)：判定**只看 `device.device_type`**，白名单为 CUDA/Vulkan/Metal，不看显存大小、不看算力等级。再读 [model.cc:1059-1063](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/model.cc#L1059-L1063) 确认这是唯一的分支依据。
2. **延迟取舍**。对比两者数据搬运：
   - CPU：`CopyProbsToCPU`（[cpu_sampler.cc:546](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/cpu_sampler.cc#L546)）每步把 \((n, V)\) float32 从 GPU 拷回主机（如 \(V=128256,\ n=32\) 约 16MB），D2H 拷贝与 CPU 串行排序构成显著延迟。
   - GPU：仅回传少量 token id / 概率值（`CopyArraysToCPU`，[gpu_sampler.cc:657](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/gpu_sampler.cc#L657)），采样计算与 GPU 前向同处一设备、且能融进 CUDA graph。
3. **显存取舍**。GPU 采样器构造时**预分配**一批 CPU+GPU 辅助张量（[gpu_sampler.cc:77-105](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/gpu_sampler.cc#L77-L105)），按 `max_num_sample` 规模常驻显存；CPU 采样器只需一块可复用的主机缓存 `probs_host_`（[cpu_sampler.cc:578](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/cpu_sampler.cc#L578)），不占 GPU 显存。因此显存紧张或后端不支持时，CPU 采样器是更省资源的兜底。
4. **`BatchVerifyDraftTokensWithProbAfterTopP` 的用途**。读 [batch_verify.cc:148-153](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_verify.cc#L148-L153)：推测解码的 `BatchVerify` Action 先对大模型 logits 做 top-p 重归一化，再调用本方法对每条序列的 draft 链（或树）做拒绝采样，产出「被接受的 token 列表 + 最后接受的节点下标」。它让大模型**一次前向**就能裁决小模型起草的一串 token，是推测解码加速的核心。

**需要观察的现象**：把上述对比整理成一张表后，能清晰看到「GPU 胜在延迟、CPU 胜在通用与省显存」。

**预期结果**：你能口述——
- `SupportGPUSampler` 仅按 `device_type ∈ {CUDA, Vulkan, Metal}` 判定；
- GPU 采样把计算留在设备上、只回传少量结果，延迟更低且可入 CUDA graph，代价是预占一块显存；
- CPU 采样是无 kernel 后端的兜底，代价是每步 \((n,V)\) 的 D2H 拷贝；
- `BatchVerifyDraftTokensWithProbAfterTopP` 用于推测解码校验阶段的拒绝采样裁决。

**待本地验证**：若有 GPU 环境，可分别用 `--device vulkan`（GPU 采样器）与 `--device cpu`（CPU 采样器）跑同一模型，对比 `/stats` 里的 decode token/s 差异；本环境无法运行，标记为待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：假设你要在一个新后端（比如某专用 NPU，`device_type=kDLExtDev`）上启用 GPU 采样，至少要改哪两处？

> **答案**：① 在 `SupportGPUSampler` 白名单里加上该 device_type；② 在编译期 `AttachGPUSamplingFunc`（[u8-l3](u8-l3-attach-passes.md)）为该 target 附加对应的 6 个采样 PrimFunc，否则 FunctionTable 取不到句柄、`GPUSampler` 构造会因 `ICHECK(defined())` 失败。

**练习 2**：为什么 `function_table.cc` 里 6 个采样函数用 `value_or(Function(nullptr))` 容错，而 `GPUSampler` 构造函数又对其中 4 个做 `ICHECK(defined())` 强校验？二者矛盾吗？

> **答案**：不矛盾。加载层容错是为了让**不支持 GPU 采样的后端**也能成功加载 model lib（这些函数干脆不存在）；而构造 `GPUSampler` 本身就意味着「已决定走 GPU 采样」，此时这些函数必须存在，故强校验把契约违反尽早暴露。两层校验共同实现「按需加载 + 用时强校验」。

---

## 5. 综合实践

把本讲全部知识串起来，做一次**完整的采样器调用链追踪**：

**任务**：以「一次普通 decode 步」为线索，从 Action 一路追到 GPU kernel，再回答三个问题。

**步骤**：

1. 从 [batch_decode.cc:166-171](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_decode.cc#L166-L171) 出发，记录它依次调用 `BatchRenormalizeProbsByTopP` 与 `BatchSampleTokensWithProbAfterTopP`。
2. 假设当前设备是 CUDA，进入 `GPUSampler`：
   - 重归一化落到 [gpu_sampler.cc:170-173](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/gpu_sampler.cc#L170-L173) 的 `gpu_renormalize_by_top_p_func_`（即编译期附加的 `renormalize_by_top_p`）。
   - 采样落到 [gpu_sampler.cc:566-654](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/gpu_sampler.cc#L566-L654) 的 `SampleOnGPU`。因为这是 AfterTopP（top_p 已应用），`need_top_p=false`；若用户没要 logprobs，则 `need_prob_values=false`，走**短路径** multinomial。
3. 回答：
   - **Q1**：这次采样有没有把整个 \((n,V)\) 概率拷回 CPU？为什么这是 GPU 采样器相对 CPU 的核心优势？
   - **Q2**：若用户在请求里设了 `top_p=0.9`，重归一化会把哪些词的概率清零？
   - **Q3**：把设备换成纯 CPU，重走步骤 2，哪些函数调用点会变成 `SampleTopPFromProb` / `RenormalizeProbByTopP`？

**参考答案**：
- **A1**：没有。GPU 采样器只回传 token id（+ 可选的少量概率值），\((n,V)\) 概率始终留在 GPU；这正是它延迟更低、且能融入 CUDA graph 的根本原因。CPU 采样器则必须 `CopyProbsToCPU`。
- **A2**：按概率降序累加，超过 `top_p=0.9` 之后的尾部词清零，剩余词按其和重新归一化（见 [cpu_sampler.cc:244-259](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/cpu_sampler.cc#L244-L259) 的 boundary + 归一化逻辑，GPU kernel `renormalize_by_top_p` 做同件事）。
- **A3**：`BatchRenormalizeProbsByTopP` 先 `CopyProbsToCPU` 再调 `RenormalizeProbByTopP`；`BatchSampleTokensWithProbAfterTopP` 调 `BatchSampleTokensImpl`，其中 `top_p_applied=true` 使 `top_p=1.0`，最终落到 `SampleTopPFromProb(..., top_p=1.0, ...)` 的纯多项采样分支。

## 6. 本讲小结

- 采样器统一抽象为 `SamplerObj` 的 **4 个纯虚方法**：重归一化、BeforeTopP 采样、AfterTopP 采样、draft 校验；调用方只持 `Sampler` 句柄，设备差异被多态隐藏。
- **CPU 采样器**用纯 C++ 算法（`SampleTopPFromProb` 先过滤再排序、`RenormalizeProbByTopP` 多轮分区、拒绝采样裁决 draft），靠 `CopyProbsToCPU` 与多线程并行工作；是无 GPU 采样能力后端的兜底。
- **GPU 采样器**不写算法，而是通过 `FunctionTable` 调用编译期 `AttachGPUSamplingFunc` 附加的 6 个 kernel，靠预分配缓冲与双流（copy/compute）掩盖延迟，并支持树形 draft 校验。
- **`SupportGPUSampler`** 仅按 `device_type ∈ {CUDA, Vulkan, Metal}` 判定；判定结果同时驱动 `CreateSampler` 的二选一与 `FunctionTable` 是否加载 6 个采样句柄，二者必须一致。
- **延迟/显存取舍**：GPU 胜在低延迟（计算留设备、可入 CUDA graph）、代价是预占显存；CPU 胜在通用与省显存、代价是每步 \((n,V)\) 的 D2H 拷贝。
- **`BatchVerifyDraftTokensWithProbAfterTopP`** 是推测解码校验阶段的拒绝采样裁决器，让大模型一次前向裁决小模型起草的一串 draft token。

## 7. 下一步学习建议

- 阅读本讲的姊妹篇 [u10-l4 推测解码动作链](u10-l4-speculative-decoding.md)，看 `BatchVerify` Action 如何把 draft 起草与本讲的校验方法串成一条完整的推测解码时间线。
- 回看 [u8-l3 运行时函数附加 pass](u8-l3-attach-passes.md) 的 `AttachGPUSamplingFunc`，确认本讲 GPU 采样器调用的 6 个 kernel 名字符串正是在那里被附加进 IRModule 的。
- 若对采样算法本身感兴趣，可对比 [cpu_sampler.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/sampler/cpu_sampler.cc) 的 `SampleTopPFromProb` 与 TVM Unity 上游的 `SampleTopPFromProb`（注释提到本实现是其增强版，待稳定后会上游化）。
