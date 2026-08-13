# 校准数据提取 extract_ptq_data

## 1. 本讲目标

本讲深入 AMCT 的 LLM 训练后量化（PTQ）四阶段里的第二阶段——`extract_ptq_data`。学完本讲你应当能够：

- 说清 `extract_ptq_data` 在整条 PTQ 链路里负责什么、为什么 PTQ 不能直接拿原始 token 就开训；
- 解释 `_run_blockwise` 的三步骨架：选 `hook_name` → 加载校准样本 → 做 embedding→block 逐层前向；
- 回答核心问题：为什么 `attn-linear`/`attn-cache` 目标要 hook `input_layernorm`，而 `mlp`/`moe` 要 hook `post_attention_layernorm`；
- 看懂 `Catcher` 与 `register_forward_hooks` 两种「截获中间激活」的手段，以及捕获的张量如何落盘、如何被 `ptq` 阶段读回复用；
- 理解 `sharded_block` 标志如何让 extract/eval 走多卡分片前向、而 PTQ 必须退回 CPU 暂存。

## 2. 前置知识

本讲承接 [u3-l2（Workflow 编排骨架）](u3-l2-workflow-skeleton.md)，默认你已经知道：

- AMCT 的 LLM PTQ 分四条命令：`eval` → `extract_ptq_data` → `ptq` → `deploy`，本讲只讲第二条；
- 四条命令共用「`run()` → `setup()` → 按 granularity 分发 → 收尾卸日志 sink」的编排骨架；
- `--quant_target` 是模块分类名，取值 `mlp`/`moe`/`attn-linear`/`attn-cache`，且 `extract_ptq_data` 与 `ptq` 两阶段必须填同一个值。

再补三个本讲用得上的基础概念：

- **校准数据（calibration set）**：一批代表性的输入文本。PTQ 不重训练原始权重，但需要看一眼「真实激活长什么样」才能算出合理的量化参数。本讲讲的就是如何产生这些激活。
- **中间激活（intermediate activation）**：模型前向过程中、某个子模块输入处的张量。LLM 太大装不下，AMCT 的做法是**一次只加载一层 decoder block**，把这一层关键子模块的输入激活录下来存盘，PTQ 时再逐层读回。
- **decoder block 的两段结构**：一个标准 transformer decoder 层先做注意力（attention），再做前馈（FFN/MLP）。两者各有一个 **layernorm** 在前面做归一化，归一化的输出正好就是注意力子模块 / FFN 子模块的输入。本讲选哪个 layernorm 来 hook，就由这一点决定。

一个 decoder block 的简化结构（HuggingFace 常见命名）：

```
block 输入(hidden_states)
   │
   ├─ input_layernorm ────────────► self_attn(要量化的 attn-linear/attn-cache 在这里)
   │                                  │
   │   ◄──── 残差相加 ────────────────┘
   │
   ├─ post_attention_layernorm ──► mlp / moe(要量化的 mlp/moe 在这里)
   │                                  │
   ◄──── 残差相加 ────────────────────┘ ──► block 输出(给下一层)
```

记住这张图，第 4.2 节的 hook 选择就自然成立。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `amct_pytorch/workflows/llm_extract_ptq_data.py` | 本讲主角 `LlmExtractPtqDataWorkflow`，三步骨架与 `hook_name` 选择都在这里 |
| `amct_pytorch/common/datasets/preproc.py` | `get_pileval`：从 pile-val 数据集切出 `nsamples × seq_len` 校准样本 |
| `amct_pytorch/common/datasets/ptq_io.py` | 中间激活 / 位置参数的存盘与读回（`save_ptq_inps`/`save_ptq_kwargs`/`load_ptq_inps`） |
| `amct_pytorch/common/models/llm/common/base.py` | `BaseModel` 的 `do_embedding_forward` / `do_block_forward`，逐层前向流水线的真正实现 |
| `amct_pytorch/common/models/llm/common/capture.py` | `Catcher`（embedding 阶段截获 layer-0 输入与位置参数）与 `register_forward_hooks`（block 阶段截获 norm 输出） |
| `amct_pytorch/common/models/llm/deepseek/deepseek_v4/deepseekv4.py` | 一个改写了 `attn_norm_name`/`ffn_norm_name` 并引入 `sharded_block` 的适配器样例 |
| `examples/extract_ptq_data.sh` | 真实 CLI 调用样例 |
| `tests/unit_test/workflows/test_llm_extract_ptq_data.py` | 验证 `hook_name` 选择与 `sharded_block` 开关的单元测试 |

## 4. 核心概念与源码讲解

### 4.1 extract_ptq_data 的定位与三段式编排骨架

#### 4.1.1 概念说明

PTQ 的目标是「在不重训练原始权重的前提下，为每个 Linear 子模块算出量化参数（scale/offset 等）」。算参数不能凭空算，必须有「真实数据跑出来的激活」作为依据。`ptq` 阶段会用一个 `BlockwiseSolver` 把量化参数「学习」出来——它需要不断把真实激活灌进单个子模块、比较量化前后的输出误差。

问题来了：大模型一次装不进显存，`ptq` 阶段更不可能端到端跑整个模型。于是 AMCT 把「录制真实激活」这一步单独拆出来，这就是 `extract_ptq_data`：它把整个模型**逐层前向一遍**，把每个待量化子模块输入处的激活录下来，存到磁盘（`--data_dir`）。`ptq` 阶段再把这些激活一份份读回，逐层逐子模块训练量化参数。

一句话定位：`extract_ptq_data` 是 `ptq` 的「数据准备工」，只跑前向、只录激活、只存盘，不做任何量化。它和 `ptq` 之间靠 `--data_dir` 目录传递数据。

#### 4.1.2 核心流程

`LlmExtractPtqDataWorkflow` 完全沿用 [u3-l2](u3-l2-workflow-skeleton.md) 讲过的三段式编排：

```
run()
  └─ setup()                      # 固定四步：建日志目录 → 惰性注册 → 建 pipeline → 挂 sink
  └─ 按 granularity 分发           # 这里只支持 "block"，否则报错
       └─ _run_blockwise()        # 选 hook_name → get_pileval → embedding 前向 → 逐层 block 前向
  └─ logger.remove(sink_id)       # 收尾卸除临时文件 sink
```

`setup()` 里有一行容易看漏、但很关键：如果适配器提供了 `sharded_block` 属性，就把它置为 `True`。这点在 4.1.3 里精读。

#### 4.1.3 源码精读

整个 workflow 类只有 4 个方法，骨架非常薄：

[LlmExtractPtqDataWorkflow.run/setup — amct_pytorch/workflows/llm_extract_ptq_data.py:47-65](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_extract_ptq_data.py#L47-L65)：`setup` 固定走「日志目录 → `_register_components()` → `_build_pipeline()` → `setup_run_logging`」，与其它三个 workflow 同构；`run` 在分发处只认 `granularity == "block"`，其它取值直接抛 `ValueError`。

[sharded_block 开关 — amct_pytorch/workflows/llm_extract_ptq_data.py:51-52](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_extract_ptq_data.py#L51-L52)：`if hasattr(self.pipeline, "sharded_block"): self.pipeline.sharded_block = True`。这两行的作用见 4.1.4 的实践。

`sharded_block` 到底改了什么行为，看适配器样例最清楚：

[DeepseekV4.block 分发 — amct_pytorch/common/models/llm/deepseek/deepseek_v4/deepseekv4.py:125-134](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/deepseek/deepseek_v4/deepseekv4.py#L125-L134)：注释写得很直白——`sharded_block=True` 走 `_block_sharded`（多卡分片加载，给 eval 用）；`sharded_block=False` 走 `super().block`（CPU 暂存，给 PTQ 用）。原因是「分片路径会用 `AlignDevicesHook` 把每个 expert 钉死在固定 NPU 上，这与 PTQ 的 `.to(device)`/`.cpu()` 来回搬运相冲突」。`extract_ptq_data` 只跑前向不学习，所以可以安全地打开分片、用多卡加速；`ptq` 阶段要逐 expert 搬运学习，必须关掉分片。

注意 `LlmPtqWorkflow`（[amct_pytorch/workflows/llm_ptq.py:69](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L69)）的 `setup` 完全不碰 `sharded_block`，它保持适配器 `__init__` 里设的默认值 `False`——这正是 extract 与 ptq 在同一模型上行为分叉的关键开关。

#### 4.1.4 代码实践

**实践目标**：用单元测试亲眼确认「extract 的 setup 会把 `sharded_block` 置 True」，并对比 ptq 不会。

**操作步骤**：

1. 打开 `tests/unit_test/workflows/test_llm_extract_ptq_data.py`，找到 `test_extract_setup_enables_sharded_block`。
2. 阅读它如何用一个带 `sharded_block = False` 的 `FakePipeline` 注入 workflow，再断言 `wf.pipeline.sharded_block is True`。
3. 在本地只跑这一条用例（纯逻辑测试，不需要真 NPU）：

   ```bash
   pytest tests/unit_test/workflows/test_llm_extract_ptq_data.py::test_extract_setup_enables_sharded_block -v
   ```

**需要观察的现象**：用例通过，说明 `setup()` 确实把标志改写了。

**预期结果**：通过；若你把第 51-52 行注释掉再跑，用例会失败（断言 `False is True` 不成立）。

> 待本地验证：仓库环境若无 `pytest`/依赖未装，命令会报 ModuleNotFoundError，这不影响理解逻辑。

#### 4.1.5 小练习与答案

**练习 1**：`extract_ptq_data` 为什么不能像 `eval` 那样支持 `granularity=model`（整模型前向）？

**参考答案**：`extract` 的产出是「逐层的子模块输入激活」，必须逐层 hook 才能录到每个 `block` 的 norm 输出并落盘；整模型前向无法在中间层挂 hook 取数据，所以源码里对非 `block` 取值直接抛 `ValueError`。

**练习 2**：如果把 `setup()` 第 51-52 行删掉，对一个带 `sharded_block` 属性的 MoE 模型跑 `extract_ptq_data` 会怎样？

**参考答案**：标志保持适配器默认的 `False`，`block()` 会走 CPU 暂存路径，功能上仍能录到激活，但放弃了多卡分片加速、单卡显存压力大、速度变慢；不会出错，只是慢。

---

### 4.2 hook_name 选择逻辑：attn 与 mlp/moe 为何选不同的 norm

#### 4.2.1 概念说明

这是本讲的核心设计，也是规格里要求重点解释的问题。

`extract_ptq_data` 每次只能填一个 `--quant_target`（见 `__init__` 的强校验）。这个值决定了「这一轮要录哪个子模块的输入激活」：

- `attn-linear` / `attn-cache`：要量化的是注意力子模块（`self_attn` 或 `linear_attn`）里的 Linear/Cache。它的输入 = `input_layernorm` 的输出。
- `mlp` / `moe`：要量化的是前馈子模块（`mlp` 或其中的 experts）。它的输入 = `post_attention_layernorm` 的输出。

回到第 2 节那张 block 结构图：注意力子模块紧跟在 `input_layernorm` 后面，FFN 子模块紧跟在 `post_attention_layernorm` 后面。**要录「子模块的输入」，就 hook「紧挨在它前面的那个 norm」**——norm 的输出就是子模块的输入。这就是选 hook 的全部直觉。

为什么必须 hook norm、而不是 hook 子模块本身？因为 AMCT 用的是 PyTorch 的 `register_forward_hook`，它在模块**前向结束后**触发、拿到的是模块输出。我们想要的是子模块**输入**，所以退一格，hook 它前一个模块（norm），取 norm 的输出——恰好就是我们要的输入张量。

#### 4.2.2 核心流程

```
quant_target ∈ {attn, attn-linear, attn-cache}  ──►  hook_name = attn_norm_name (默认 "input_layernorm")
quant_target ∈ {mlp, moe}                        ──►  hook_name = ffn_norm_name  (默认 "post_attention_layernorm")
```

两个 norm 名都不是硬编码，而是从适配器上读属性，默认值就是 HuggingFace 的主流命名：

```python
attn_hook_name = getattr(self.pipeline, "attn_norm_name", "input_layernorm")
ffn_hook_name  = getattr(self.pipeline, "ffn_norm_name",  "post_attention_layernorm")
```

这样 DeepSeek-V4 这类把 norm 命名为 `attn_norm`/`ffn_norm` 的模型，只要在适配器里覆写两个类属性即可，workflow 代码无需改动（见 4.2.3）。

#### 4.2.3 源码精读

[hook_name 选择 — amct_pytorch/workflows/llm_extract_ptq_data.py:71-81](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_extract_ptq_data.py#L71-L81)：先取两个候选名，再用一个三元式按 `quant_target` 分流。注意判等用的是 `in ("attn", "attn-linear", "attn-cache")` 元组，`else` 分支自然覆盖 `mlp`/`moe`。

[BaseModel 默认 norm 名 — amct_pytorch/common/models/llm/common/base.py:51-54](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L51-L54)：类属性 `attn_norm_name = "input_layernorm"`、`ffn_norm_name = "post_attention_layernorm"`，注释明确「HuggingFace 约定，命名不同的适配器覆写之」。

[DeepseekV4 覆写 norm 名 — amct_pytorch/common/models/llm/deepseek/deepseek_v4/deepseekv4.py:64-65](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/deepseek/deepseek_v4/deepseekv4.py#L64-L65)：`attn_norm_name = "attn_norm"`、`ffn_norm_name = "ffn_norm"`。由于 workflow 用 `getattr` 读属性，这里覆写后无需改任何分发逻辑。

[落盘时再归一化为 attn — amct_pytorch/common/models/llm/common/base.py:179-187](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L179-L187)：`save_block_hook_inputs` 在写文件名时，把 `attn-linear`/`attn-cache` 统一归并成 `attn`（`save_target = "attn" if "attn-linear" in ... or "attn-cache" in ... else self.quant_target[0]`）。这是为了和 `ptq` 阶段的 `iter_ptq_units` 对齐——那边 attn-linear/attn-cache 都会生成 `kind="attn"` 的单元（见 [base.py:294-300](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L294-L300)），读写两端必须用同一个文件名 key 才能对上。

#### 4.2.4 代码实践

**实践目标**：亲手验证「不同 quant_target 选不同 hook」这一行为，并回答本讲的核心问题。

**操作步骤**：

1. 打开 `tests/unit_test/workflows/test_llm_extract_ptq_data.py`，阅读参数化用例 `test_run_blockwise_picks_hook_name_by_quant_target`——它用一个假 pipeline 拦截 `do_embedding_forward(samples, hook_name)` 收到的 `hook_name`，并枚举了 5 个 target 与期望 hook 的对应。
2. 跑该参数化用例：

   ```bash
   pytest "tests/unit_test/workflows/test_llm_extract_ptq_data.py::test_run_blockwise_picks_hook_name_by_quant_target" -v
   ```

3. 对照本节内容，写出核心问题的答案（见下方「预期结果」）。

**需要观察的现象**：5 组参数全部通过；日志里能看到 `attn*` 三类都选了 `input_layernorm`，`mlp`/`moe` 选了 `post_attention_layernorm`。

**预期结果**（核心问题答案）：

- **为什么 attn 目标用 `input_layernorm`？** 因为注意力子模块（`self_attn`/`linear_attn`）的输入就是 `input_layernorm` 的输出。要录「attn 子模块的输入激活」，就 hook 它前一个模块 `input_layernorm`、取其输出。
- **为什么 mlp/moe 用 `post_attention_layernorm`？** 因为 FFN 子模块（`mlp`/`moe` experts）紧跟在 `post_attention_layernorm` 之后，它的输出正是 FFN 子模块的输入。

> 待本地验证：若环境缺依赖无法跑 pytest，直接读用例的 `@pytest.mark.parametrize` 表格即可确认对应关系。

#### 4.2.5 小练习与答案

**练习 1**：`register_forward_hook` 拿到的是模块的输入还是输出？为什么我们 hook 的是 norm 而不是子模块本身？

**参考答案**：`register_forward_hook` 在模块前向结束后触发，拿到的是**输出**。我们要的是子模块**输入**，所以 hook 前一个模块（norm），取它的输出 = 子模块的输入。

**练习 2**：若一个新模型的 FFN 前的 norm 不叫 `post_attention_layernorm` 而叫 `pre_feedforward_layernorm`（Gemma 风格），需要改 workflow 代码吗？

**参考答案**：不需要。只需在该模型的适配器类里加一行 `ffn_norm_name = "pre_feedforward_layernorm"`，workflow 的 `getattr(self.pipeline, "ffn_norm_name", ...)` 会自动取到新值。

---

### 4.3 校准样本加载：get_pileval

#### 4.3.1 概念说明

有了 hook_name，还需要「喂给模型的输入文本」。AMCT 默认用 **pile-val** 数据集（`mit-han-lab/pile-backup` 的 validation 分区）作为校准源——这是量化社区常用的代表性语料，分布足够「普通」，适合当统计激活的基准。

校准样本不是一条条单独喂的，而是要拼成固定大小的张量：`nsamples` 条、每条 `seq_len` 个 token。这两个值由 CLI 参数 `--nsamples` 与 `--seq_len` 控制（[examples/extract_ptq_data.sh:27](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/extract_ptq_data.sh#L27) 默认 `--nsamples 128`）。样本越多、序列越长，录到的激活分布越接近真实，但前向越慢、落盘越大。

#### 4.3.2 核心流程

`get_pileval(tokenizer, n_samples, seq_len)` 的产出形状是：

\[
\text{samples}: \text{list 长度 } n\_samples,\ \text{每个元素形状 } [1,\ seq\_len]
\]

内部两步：

1. 从 HuggingFace Hub 拉 pile-val 的 validation 分区，按固定种子 `shuffle(seed=42)` 保证可复现。
2. `pileval_awq` 把文本逐条 tokenize，**把多条样本在 token 维度上首尾拼接成一条长序列**，再按 `seq_len` 等长切片，取前 `n_samples` 片。这种「拼接再切片」的做法能避免单条文本过短造成的浪费，保证每片都是满 `seq_len`。

#### 4.3.3 源码精读

[get_pileval 入口 — amct_pytorch/common/datasets/preproc.py:54-59](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/preproc.py#L54-L59)：`load_dataset('mit-han-lab/pile-val-backup', split='validation')` 拉数据，再交给 `pileval_awq` 切片。

[pileval_awq 拼接切片 — amct_pytorch/common/datasets/preproc.py:23-51](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/preproc.py#L23-L51)：关键三段——
- L28-40：逐条文本 tokenize，**跳过** `len > seq_len` 的长文本（避免一条占满一切片），累加 token 数到 `target_tokens = n_samples * seq_len` 即停；
- L43：`torch.cat(samples, dim=1)` 把所有短样本在序列维拼成一条长 tensor；
- L44-50：`n_split = total // seq_len` 计算能切出几片，不足 `n_samples` 直接抛错；最后列表推导切出前 `n_samples` 片，每片形状 `[1, seq_len]`。

注意 L32-33 的 `if len(line_encoded) > seq_len: continue`——这里**丢掉超长文本**而不是截断，是为了让每片都由完整短句拼接而成、分布更自然。L45-49 的校验保证：若语料不够拼出 `n_samples` 片，宁可报错也不返回残缺数据。

#### 4.3.4 代码实践

**实践目标**：在不加载大模型的前提下，单独观察 `pileval_awq` 产出的样本形状。

**操作步骤**（源码阅读型 + 可选运行）：

1. 阅读上面的 L23-51，自己推算：若 `n_samples=4`、`seq_len=8`，`target_tokens` 是多少？至少需要拼出多长的序列？（答：32）
2. （可选）若本地能联网且有 `datasets`/`transformers`，写一段最小调用（**示例代码**，非项目原有）：

   ```python
   from transformers import AutoTokenizer
   from amct_pytorch.common.datasets.preproc import get_pileval
   tok = AutoTokenizer.from_pretrained("/path/to/model", trust_remote_code=True)
   samples = get_pileval(tok, n_samples=4, seq_len=512)
   print(len(samples), samples[0].shape)  # 预期: 4, torch.Size([1, 512])
   ```

**需要观察的现象**：返回 list 长度恰为 `n_samples`，每个元素形状都是 `[1, seq_len]`。

**预期结果**：`4` 与 `torch.Size([1, 512])`。若 `n_samples*seq_len` 超过语料 token 总数，会命中 L46-49 的 `ValueError`。

> 待本地验证：`get_pileval` 需联网下载 pile-val 数据集与 tokenizer，离线环境会失败。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `pileval_awq` 选择「拼接再切片」而不是「每条文本单独作为一个样本」？

**参考答案**：单条文本长度参差不齐，要么需要 pad（浪费、引入 pad token 污染激活统计）、要么截断（丢数据）。拼接成一条长序列再等长切片，能保证每片都恰好 `seq_len`、无 padding，且每片都含完整语义边界。

**练习 2**：把 `--nsamples` 从 128 调到 256，`extract_ptq_data` 的产物体积会怎么变？

**参考答案**：每层落盘的激活张量沿 sample 维翻倍，`block_*_in.pkl` 文件体积大约翻倍；录到的激活分布更充分，后续 PTQ 精度通常更好，但前向耗时与磁盘占用也翻倍。

---

### 4.4 逐层前向：do_embedding_forward + do_block_forward

#### 4.4.1 概念说明

样本和 hook 都就位后，进入真正的前向。这里有一个大模型量化的经典技巧叫 **blockwise 逐层前向**：既然整模型装不下，就只把第 0 层装进显存、跑一遍拿到它每个样本的输入激活；再卸掉第 0 层、改装第 1 层、把刚才的激活喂进去跑出第 1 层的输出……如此接力。每一层只需要在显存里待一小会儿。

这一过程在 AMCT 里拆成两个方法：

- `do_embedding_forward`：只跑 embedding + 第 0 层，目的是**两件事**——录下第 0 层的输入激活（给 block 0 当输入），并截获整段前向需要的「位置参数」（`position_ids`/`position_embeddings`/`attention_mask`），供后续每一层 block 前向复用。
- `do_block_forward`：逐层执行，每层挂 hook 录下 norm 输出（= 待量化子模块输入）并落盘；同时返回该层整体输出，作为下一层的输入。

#### 4.4.2 核心流程

```
samples (nsamples × [1, seq_len])
   │
   │ do_embedding_forward(samples, hook_name)
   │   ├─ load embedding 权重 + 第 0 层权重
   │   ├─ 用 Catcher 包住第 0 层，逐 sample 前向（Catcher 抛 ValueError 提前终止一层内部计算，
   │   │   只为截获第 0 层【输入】与位置参数）
   │   └─ 返回 outs (= 第 0 层各 sample 的输入激活)，位置参数存到 self.position_ids/...
   ▼
inter_io (= 上一层的输出 / 下一层的输入)
   │
   │ for layer_idx in range(num_layers):
   │     do_block_forward(layer_idx, inter_io, hook_name)
   │       ├─ 加载该层权重 (block(layer_idx))，移到 device
   │       ├─ register_forward_hooks 挂到 hook_name 模块上，捕获其输出到 act_stat
   │       ├─ 逐 sample 前向: block(sample, position_ids=..., attention_mask=..., ...)
   │       ├─ save_block_hook_inputs: act_stat[f"{hook_name}_out"] 落盘
   │       └─ 返回 outs (= 该层输出) → 成为下一轮 inter_io
   ▼
所有层的子模块输入激活都已落盘
```

两个「截获」手段的区别要分清：

| 手段 | 阶段 | 截获什么 | 机制 |
| --- | --- | --- | --- |
| `Catcher` | embedding（仅 layer 0） | layer 0 的**输入** + 位置参数 | 包一层 `nn.Module`，`forward` 里把输入 append 到列表后立即 `raise ValueError`，靠异常终止单次前向 |
| `register_forward_hooks` | block（每层） | norm 模块的**输出**（= 子模块输入） | PyTorch 原生 `register_forward_hook`，前向后触发 |

为什么 embedding 阶段要用「抛异常」这种奇技？因为第 0 层的输入就是 embedding 的输出，我们只想要这个张量、不想真把第 0 层算完（算完还要消耗算力且后续不再用）。`Catcher` 在 `forward` 入口截获输入后立刻抛 `ValueError`，外层 `try/except` 吞掉它，于是「只录不算」。而 block 阶段必须把整层算完（要拿输出给下一层），所以用正常的 hook。

#### 4.4.3 源码精读

[workflow 调用两段前向 — amct_pytorch/workflows/llm_extract_ptq_data.py:87-93](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_extract_ptq_data.py#L87-L93)：`inter_io = do_embedding_forward(samples, hook_name=hook_name)` 拿到 layer-0 输入；随后 `for layer_idx in tqdm(...): inter_io = do_block_forward(layer_idx, inter_io, hook_name=hook_name)` 逐层推进，注意每层的返回值直接喂给下一层——这就是接力。

[Catcher 截获 layer-0 输入 — amct_pytorch/common/models/llm/common/capture.py:23-50](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/capture.py#L23-L50)：`forward(inp, **kwargs)` 把 `inp.to("cpu")` append 到 `self.dataset`（即外面的 `outs`），并顺手把 kwargs 里的 `attention_mask`/`position_ids`/`position_embeddings` 存下来，最后 `raise ValueError`（L50）终止本次前向。

[do_embedding_forward — amct_pytorch/common/models/llm/common/base.py:189-215](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L189-L215)：L193-195 装好第 0 层并用 `Catcher` 包住；L196-203 的 `try: self.model(inputs) except ValueError: pass` 就是「让 Catcher 抛、外层吞」；L204-206 把 Catcher 存下的位置参数搬到 `self` 上；L207-213 在 `hook_name is not None` 时把这些参数落盘（`save_ptq_kwargs`，供 attn 目标后续 block 前向时还原位置信息）；L214 把 `Catcher` 剥掉还原成原始 module。

[register_forward_hooks — amct_pytorch/common/models/llm/common/capture.py:66-73](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/capture.py#L66-L73)：遍历 `block.named_modules()`，凡 `target_name in name`（**子串匹配**）就给该模块挂一个 hook；hook 把输出 append 到 `act_stat[f"{name}_out"]`（[_stat_input_hook — capture.py:62-63](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/capture.py#L62-L63)）。子串匹配意味着 hook 名 `input_layernorm` 会同时命中 `self_attn.input_layernorm` 之类，正合需要。

[do_block_forward — amct_pytorch/common/models/llm/common/base.py:234-288](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L234-L288)：L242-259 装载/分派 block、挂 hook；L261-264 取位置参数、按 sample 逐条前向 `block(sample, **call_kwargs)`（[get_block_forward_kwargs — base.py:166-177](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L166-L177) 负责把 `self.position_ids` 等搬到 device）；L265-273 把每条输出收进 `outs`；L275-278 移除 hook 并 `save_block_hook_inputs` 落盘；L279-287 清理 block、`gc.collect()`+`torch.npu.empty_cache()` 释放显存——这正是「一层只用一瞬间」的关键，下一层加载前显存必须腾空。

#### 4.4.4 代码实践

**实践目标**：把 `Catcher` 与 `register_forward_hooks` 两种截获手段在本地用极小玩具复现，直观感受「抛异常截输入」与「hook 截输出」的差别。

**操作步骤**（**示例代码**，非项目原有，可独立运行）：

```python
import torch
import torch.nn as nn
from amct_pytorch.common.models.llm.common.capture import Catcher, register_forward_hooks

# 一个最小 block: norm(input_layernorm) -> 线性(mlp)
class ToyBlock(nn.Module):
    def __init__(self):
        super().__init__()
        self.input_layernorm = nn.LayerNorm(4)
        self.mlp = nn.Linear(4, 4)
    def forward(self, x):
        return self.mlp(self.input_layernorm(x))

block = ToyBlock()
outs = []
catcher = Catcher(block, outs)          # 包住 block
try:
    catcher(torch.randn(1, 4))
except ValueError:
    pass
print("Catcher 截到的输入:", outs[0].shape)   # torch.Size([1, 4])
print("mlp 真的没被调用:", block.mlp.weight)   # 权重未变，因为 mlp 前向没跑完

# 对比: 用 hook 截 norm 的输出
act_stat, hooks = [], []
register_forward_hooks(block, "input_layernorm", hooks, act_stat)
with torch.no_grad():
    block(torch.randn(1, 4))
print("hook 截到的 norm 输出:", act_stat["input_layernorm_out"][0].shape)  # torch.Size([1, 4])
```

**需要观察的现象**：`Catcher` 那段，`outs[0]` 形状正确且 `mlp` 未参与计算；`register_forward_hooks` 那段，`act_stat` 里出现了 `input_layernorm_out` 键。

**预期结果**：两处都打印 `torch.Size([1, 4])`，且能看出 Catcher 路径下 `mlp` 没被实际执行。

> 待本地验证：需可 import `amct_pytorch`（仅依赖本模块的纯 PyTorch 代码，无需 NPU/大模型）。

#### 4.4.5 小练习与答案

**练习 1**：`do_block_forward` 末尾的 `gc.collect()` 和 `torch.npu.empty_cache()` 能不能省？为什么？

**参考答案**：不能省。blockwise 前向的核心约束是「显存里同时只有一层」。每层跑完若不显式释放，Python 引用与 NPU 缓存会拖延回收，下一层 `block(layer_idx+1)` 加载时可能 OOM。这两行强制腾空显存。

**练习 2**：`do_embedding_forward` 里为什么要 `layers[0] = layers[0].module`（L214）把 Catcher 还原？

**参考答案**：Catcher 是临时包装，只为截获 layer-0 输入与位置参数。截完后必须剥掉还原成原始 module，否则后续若复用该层会再次触发「抛异常」逻辑、或因 Catcher 转发语义导致行为异常。

---

### 4.5 中间 IO 落盘与跨阶段复用

#### 4.5.1 概念说明

`extract_ptq_data` 的全部价值都体现在「落盘的 `.pkl` 文件」上——这些文件是 `ptq` 阶段的输入。本节讲清楚：存了什么、文件名叫什么、`ptq` 阶段怎么按名读回。

落盘分两类：

1. **子模块输入激活**（每层一个文件）：`block_{layer_idx}_{target}_in.pkl`，即 `do_block_forward` 里 hook 到的 norm 输出。这是 PTQ 训练量化参数时的「真实输入」。
2. **位置参数**（全局一份）：`position_ids.pkl`/`position_embeddings.pkl`/`attention_mask.pkl`，由 `do_embedding_forward` 录到、`save_ptq_kwargs` 落盘。block 前向需要这些来正确还原 attention 与位置编码，且只有 attn 目标真正用到（见 `load_ptq_inps` 的分支）。

#### 4.5.2 核心流程

写（extract 阶段）：

```
do_embedding_forward  --save_ptq_kwargs-->  data_dir/{position_ids,position_embeddings,attention_mask}.pkl
do_block_forward      --save_ptq_inps----->  data_dir/block_{layer_idx}_{target}_in.pkl
```

读（ptq 阶段）：

```
load_ptq_inps(data_dir, target, layer_idx)
   ├─ (仅 target == "attn") 读回 position_ids/position_embeddings/attention_mask → kwargs
   └─ 读 block_{layer_idx}_{target}_in.pkl → cached_inps
   返回 (cached_inps, kwargs)
```

**命名契约**（两端必须对齐的关键）：写端的 `target` 由 `save_block_hook_inputs` 决定（attn-linear/attn-cache 归并为 `attn`，其余取 `quant_target[0]`）；读端的 `target` 来自 `ptq` 阶段 `PtqUnit.kind`（`iter_ptq_units` 里也是 `attn`/`mlp`/`moe`）。两端用同一组 key，所以 extract 与 ptq 的 `--quant_target` 必须一致——这正是 [u1-l4](u1-l4-first-quant-cli.md) 强调的约束在文件系统层面的体现。

#### 4.5.3 源码精读

[save_ptq_inps — amct_pytorch/common/datasets/ptq_io.py:35-39](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_io.py#L35-L39)：取出 `act_stat[f"{hook_name}_out"]`，`torch.cat` 把各 sample 的输出沿 batch 维拼好，存为 `block_{layer_idx}_{quant_target}_in.pkl`。注意这里 key 用的是 `f"{hook_name}_out"`，与 [_append_capture_tensor — capture.py:53-59](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/capture.py#L53-L59) 写入时的 `f"{name}_{tensor_type}"`（`tensor_type="out"`）完全对应。

[save_ptq_kwargs — amct_pytorch/common/datasets/ptq_io.py:23-32](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_io.py#L23-L32)：三个位置参数各自独立存盘，且都做了 `is not None` 判空（某些模型可能没有 `position_embeddings`）。由 `do_embedding_forward` 在 L207-213 调用。

[load_ptq_inps — amct_pytorch/common/datasets/ptq_io.py:42-63](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_io.py#L42-L63)：L44-56 只有 `target == "attn"` 时才读位置参数（因为只有 attn 子模块前向需要 attention_mask/position_ids）；L57-59 用 `try/except FileNotFoundError` 兜底，文件不存在时记 warning 并返回 `(None, kwargs)`——这配合 ptq 的断点续跑/缺数据降级。

[load_unit_inputs 入口 — amct_pytorch/common/models/llm/common/base.py:80-82](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L80-L82)：ptq 阶段通过 `BaseModel.load_unit_inputs` 调 `load_ptq_inps(data_dir, unit.kind, unit.layer_idx)`——`unit.kind` 正是 `attn`/`mlp`/`moe`，与写端 `save_target` 对齐。

#### 4.5.4 代码实践

**实践目标**：亲手确认 extract 写出的文件名能被 ptq 读回，理解命名契约。

**操作步骤**（源码阅读 + 模拟落盘）：

1. 在 `ptq_io.py` 里读 `save_ptq_inps`，确认文件名模板 `f"block_{layer_idx}_{quant_target}_in.pkl"`。
2. 在 `base.py` 里读 `save_block_hook_inputs`（L179-187），列出不同 `quant_target` 对应的 `save_target`：

   | `--quant_target` | `save_target`（文件名里的 target） |
   | --- | --- |
   | `mlp` | `mlp` |
   | `moe` | `moe` |
   | `attn-linear` | `attn` |
   | `attn-cache` | `attn` |

3. （可选）用 `torch.save` 模拟写一个文件再读回（**示例代码**）：

   ```python
   import torch, os, tempfile
   from amct_pytorch.common.datasets.ptq_io import save_ptq_inps, load_ptq_inps
   d = tempfile.mkdtemp()
   act_stat = {"post_attention_layernorm_out": [torch.randn(1, 8, 4)]}
   save_ptq_inps(act_stat, "post_attention_layernorm", "mlp", 0, d)
   print("写出文件:", os.listdir(d))               # ['block_0_mlp_in.pkl']
   inps, kwargs = load_ptq_inps(d, "mlp", 0)
   print("读回形状:", inps.shape, "kwargs:", kwargs)  # torch.Size([1, 8, 4]) {}
   ```

**需要观察的现象**：文件名是 `block_0_mlp_in.pkl`，读回的张量形状与写入一致；`kwargs` 为空（因为 target 不是 `attn`）。

**预期结果**：写出 `block_0_mlp_in.pkl`；读回形状 `torch.Size([1, 8, 4])`，`kwargs == {}`。

> 待本地验证：仅需 `torch` 与可 import 的 `amct_pytorch.common.datasets.ptq_io`（纯 CPU）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `load_ptq_inps` 只在 `target == "attn"` 时读位置参数？

**参考答案**：mlp/moe 子模块前向不依赖 attention_mask 与位置编码，只需要 hidden states；而 attn 子模块（含 attention 计算）必须还原 attention_mask/position_ids/position_embeddings 才能算对，所以位置参数只对 attn 目标有意义，只在 attn 分支读回。

**练习 2**：`load_ptq_inps` 对文件不存在返回 `None` 而不是抛错，这种设计的用意是什么？

**参考答案**：配合 PTQ 的「断点续跑」与「缺数据降级」——某层激活没录到或被删时，ptq 不直接崩溃，而是返回 `None` 让上层决定是跳过该层、走直转兜底，还是报错。这是一种防御式读取。

---

## 5. 综合实践

把本讲知识串起来，完成一次「纸面端到端追踪」任务（无需 NPU，纯源码阅读）：

**任务**：假设要对一个 Qwen3 模型做 `--quant_target mlp` 的 PTQ。请按下面的顺序，从源码里找到每一步对应的函数与文件，画出完整的数据流图，并标注每一步产出的张量/文件。

1. CLI：`python -m amct_pytorch.extract_ptq_data --quant_target mlp ...`（[examples/extract_ptq_data.sh:19-27](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/examples/extract_ptq_data.sh#L19-L27)）→ 转发到 [cli/llm/extract_ptq_data.py:22-25](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/extract_ptq_data.py#L22-L25) 的 `main`。
2. 构造 `LlmExtractPtqDataWorkflow`，`run()` → `setup()`（建 pipeline、置 `sharded_block=True`）。
3. `_run_blockwise`：`quant_target=mlp` → `hook_name = post_attention_layernorm`。
4. `get_pileval` 切出 `128 × [1, seq_len]` 样本。
5. `do_embedding_forward` 用 `Catcher` 录到 layer-0 输入 + 位置参数，`save_ptq_kwargs` 落盘 3 个 `.pkl`。
6. `for layer_idx in range(num_layers)`：`do_block_forward` 挂 hook 截 `post_attention_layernorm` 输出 → `save_ptq_inps` 落盘 `block_{idx}_mlp_in.pkl`；返回值作为下一层输入。
7. 这些 `.pkl` 将被 [u4-l2（PTQ 主流程）](u4-l2-ptq-main-flow.md) 的 `LlmPtqDataProvider` 经 `load_ptq_inps` 读回，喂给 `BlockwiseSolver` 训练量化参数。

**交付物**：一张数据流图（含张量形状与文件名），并写一句话回答：如果把 `--quant_target` 改成 `moe`，步骤 3/6 的 hook 名和落盘文件名分别会怎么变？

**参考答案**：步骤 3 `hook_name` 仍是 `post_attention_layernorm`（moe 也是 FFN 子模块，前接同一个 norm）；步骤 6 文件名由 `block_{idx}_mlp_in.pkl` 变为 `block_{idx}_moe_in.pkl`（`save_target = self.quant_target[0] = "moe"`）。

## 6. 本讲小结

- `extract_ptq_data` 是 PTQ 的「数据准备工」：只跑前向、把每个待量化子模块的输入激活录下来存到 `--data_dir`，供 `ptq` 阶段读回训练量化参数；它本身不做任何量化。
- `_run_blockwise` 三步骨架：按 `quant_target` 选 `hook_name` → `get_pileval` 取校准样本 → `do_embedding_forward` + 逐层 `do_block_forward` 接力。
- hook 选择的核心直觉：要录「子模块输入」，就 hook「紧挨它前面的 norm」——attn 目标 hook `input_layernorm`，mlp/moe 目标 hook `post_attention_layernorm`；norm 名从适配器属性读取，默认即 HuggingFace 主流命名。
- 两种截获手段要分清：`Catcher`（embedding 阶段，靠抛 `ValueError` 截 layer-0 输入与位置参数）、`register_forward_hooks`（block 阶段，截 norm 输出）。
- 落盘命名契约 `block_{layer_idx}_{target}_in.pkl` 两端对齐（attn-linear/attn-cache 归并为 `attn`），这正是 extract 与 ptq 的 `--quant_target` 必须一致的文件系统层根源。
- `sharded_block` 标志：extract/eval 置 True 走多卡分片前向（只前向、快），ptq 保持 False 走 CPU 暂存（要逐 expert 搬运学习）。

## 7. 下一步学习建议

- 下一篇 [u4-l2 PTQ 训练后量化主流程](u4-l2-ptq-main-flow.md) 会讲 `LlmPtqWorkflow._run_blockwise` 如何把本讲录下的 `.pkl` 读回、切成 `PtqUnit`、逐 unit 求解——建议顺着 `load_ptq_inps` 的调用点继续往下读。
- 若想深入「截获手段」的底层，可读 [amct_pytorch/common/models/llm/common/capture.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/capture.py) 全文，并对照 PyTorch 文档理解 `register_forward_hook` 的触发时机。
- 想了解校准数据如何影响精度，可回到 [u2-l1 模型压缩与量化的基本原理](u2-l1-compression-basics.md) 复习 scale/offset 与 outlier 的关系，再思考「为什么 nsamples 越大精度越好」。
