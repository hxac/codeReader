# MTP 推测解码

## 1. 本讲目标

本讲讲解 LightLLM 中 **Multi-Token Prediction（MTP，多 token 预测）推测解码** 的完整实现。推测解码（speculative decoding）是一类用「小而快的 draft 模型」先猜、再用「大而准的主模型」校验的加速技术。学完本讲，你应当能够：

1. 说清 MTP 推测解码的整体数据流：主模型一次校验多个 token、draft 模型一次预测多个 token，两者如何交替推进。
2. 理解 draft 模型 `Deepseek3MTPModel` 的特殊之处——它复用主模型的权重、KV 内存池、请求管理器，自身只多出一个投影层。
3. 掌握 `decode_mtp` 中「批量扩展 + 逐请求验证 + 回写下一轮草稿」的执行过程，以及 Triton kernel `mtp_verify` / `mtp_scatter_next_token_ids` 的语义。
4. 认识 `--mtp_mode`、`--mtp_step`、`--mtp_draft_model_dir` 三个启动参数的取值与互斥校验关系。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（对应前置讲义）：

- **prefill / decode 两阶段**（u3-l2）：prefill 一次性吃下整段 prompt，decode 每步只生成 1 个 token。MTP 是对 decode 阶段的加速。
- **ModeBackend 与 infer_loop**（u2-l4）：真正的推理在 `ModeBackend` 的 `infer_loop` 线程里自驱完成，router 只通过共享内存下发命令。本讲的 `decode_mtp` / `prefill_mtp` 就是 backend 在 MTP 模式下绑定的两个方法。
- **MoE / DeepSeek 模型**（u5-l4、u5-l5）：MTP 最早为 DeepSeek-V3 设计，draft 模型直接继承自 `Deepseek2TpPartModel`。
- **token 级 KV Cache 与 mem_manager**（u4-l1）：draft 模型与主模型共享同一个 `mem_manager`，理解显存索引分配对理解「验证后回收」很关键。

补充几个本讲用到的小概念：

- **推测解码（speculative decoding）**：让一个廉价 draft 模型先「投机」生成若干候选 token，主模型再一次性并行校验它们。命中的 token 直接采用，未命中的丢弃并从分歧点重算。本质是用「主模型一次并行算 k 个」替代「主模型串行算 k 次」，因为主模型并行算 k 个的代价远小于 k 次串行。
- **draft 模型（草稿模型）**：一个比主模型轻量的预测器。DeepSeek-V3 的 MTP 模块是一个完整的 transformer 块（带 attention），但权重独立、词表与 lm_head 与主模型共享。
- **accept length（接受长度）**：一轮推测中，主模型与 draft 模型从开头连续一致的最大 token 数，是衡量推测解码收益的核心指标。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [lightllm/server/api_cli.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L688-L720) | 定义 `--mtp_mode`/`--mtp_step`/`--mtp_draft_model_dir` 三个命令行参数的取值与默认值 |
| [lightllm/server/api_start.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L213-L219) | 启动期对三个 MTP 参数做互斥断言校验 |
| [lightllm/server/router/model_infer/mode_backend/base_backend.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L292-L358) | `ModeBackend.init_mtp_draft_model`：构造并加载 draft 模型列表 |
| [lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py#L234-L332) | MTP 模式下的 `decode_mtp`/`prefill_mtp` 主流程与两种 draft 解码策略 |
| [lightllm/server/router/model_infer/mode_backend/mtp_pre_process.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/mtp_pre_process.py#L7-L24) | `prepare_mtp_prefill_inputs`：把上一步预测 token 拼到输入末尾，构造 draft 模型输入 |
| [lightllm/server/router/model_infer/mode_backend/generic_pre_process.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/generic_pre_process.py#L98-L162) | `prepare_decode_inputs`：MTP 下按 `b_mtp_index` 把 batch 扩展 \(1+\text{mtp\_step}\) 倍 |
| [lightllm/models/deepseek_mtp/model.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek_mtp/model.py#L8-L65) | draft 模型 `Deepseek3MTPModel`：复用主模型资源、只加载自身投影与层权重 |
| [lightllm/models/deepseek_mtp/layer_infer/pre_layer_infer.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek_mtp/layer_infer/pre_layer_infer.py#L17-L30) | draft 模型前处理：拼接 [norm(emb), norm(hidden)] 后经 `eh_proj` 投影 |
| [lightllm/models/deepseek_mtp/layer_weights/pre_and_post_layer_weight.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek_mtp/layer_weights/pre_and_post_layer_weight.py#L11-L44) | draft 模型权重定义（`eh_proj`/`enorm`/`hnorm`/`final_norm`，词表与 lm_head 共享） |
| [lightllm/common/basemodel/triton_kernel/mtp_utils.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/mtp_utils.py#L46-L87) | 推测解码核心 kernel：`mtp_verify`、`mtp_scatter_next_token_ids` 等 |
| [lightllm/common/basemodel/triton_kernel/gen_mtp_prefill_params.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/gen_mtp_prefill_params.py#L26-L51) | Triton kernel：把每个序列整体左移一位、末尾插入预测 token |
| [lightllm/common/basemodel/basemodel.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L736-L739) | 主模型在 MTP 模式下额外输出最后一层 hidden state 供 draft 模型使用 |

## 4. 核心概念与源码讲解

本讲拆成三个层层递进的最小模块：

- **4.1 MTP 推测解码**：整体原理与 `decode_mtp` 的编排。
- **4.2 draft 模型**：`Deepseek3MTPModel` 如何复用主模型资源、自身结构是什么。
- **4.3 多 token 生成**：批量扩展、并行验证、回写下一轮草稿的具体机制。

### 4.1 MTP 推测解码

#### 4.1.1 概念说明

普通 decode 每一步：主模型读入上一步的 1 个 token → 前向 → 采样出 1 个新 token。瓶颈在于「主模型一次前向只为得到 1 个 token」，GPU 算力未被填满。

MTP 推测解码改写了这个节奏：

1. **draft 模型先猜**：用一个轻量 draft 模型，基于主模型上一步的 hidden state，串行（或一次）预测出 \(k\) 个候选 token（\(k=\text{mtp\_step}\)）。
2. **主模型并行校验**：把「真实 token + \(k\) 个草稿」拼成一个长 \(k+1\) 的序列一次性喂给主模型，主模型一次前向就给出每个位置之后「正确」的 token。
3. **逐位比对**：从第 1 个草稿开始，把主模型的预测与草稿逐位比较，直到出现第一个不一致的位置。前缀一致的 token 全部采纳，分歧之后的草稿丢弃。
4. **重算 + 续猜**：从分歧点的主模型预测 token 开始，draft 模型再为下一轮猜 \(k\) 个新草稿。

收益来自「并行校验」：主模型算 \(k+1\) 个 token 的延迟远低于 \(k+1\) 次串行 decode，而草稿命中率越高、单步净产出 token 越多。

LightLLM 用 `--mtp_mode` 区分两类实现：

- `*_with_att`（如 `vanilla_with_att`、`eagle_with_att`）：draft 模型带 attention，会读写 KV cache（DeepSeek-V3、GLM4-MoE-Lite、Qwen3.5 系列用此）。
- `*_no_att`（如 `vanilla_no_att`、`eagle_no_att`）：draft 模型不带 attention（Qwen3-MoE、Mistral 用此）。

而 `vanilla` 与 `eagle` 的区别在于 draft 模型实例数：`vanilla` 用 `mtp_step` 个独立 draft 模型串成链；`eagle` 只用 1 个 draft 模型在循环里复用 `mtp_step` 次。

#### 4.1.2 核心流程

MTP 模式下 backend 的方法绑定在构造期就完成切换——把原本的 `prefill_normal`/`decode_normal` 换成 `prefill_mtp`/`decode_mtp`：

```text
ChunkedPrefillBackend.__init__():
  if mtp_mode:
      self.prefill = self.prefill_mtp
      self.decode  = self.decode_mtp
      self.is_mtp_eagle = mtp_mode in ["eagle_with_att", "eagle_no_att"]
      self.num_mtp_models = 1 if is_mtp_eagle else mtp_step
      self._draft_decode_func = _draft_decode_eagle if is_mtp_eagle else _draft_decode_vanilla
```

一轮 `decode_mtp`（即主模型「打一拍」）的执行顺序如下：

```text
1. prepare_decode_inputs(decode_reqs)
     把每个请求扩展成 (1 + mtp_step) 行：b_mtp_index=0 是真实 token，
     b_mtp_index=1..k 是上一轮 draft 模型猜的草稿（待校验）。
2. main_model.forward(model_input)
     主模型对这 (1+k) 行一次性前向，得到每个位置「之后该出现」的 token 预测。
3. sample(...) → next_token_ids
     主模型采样出每个位置的正确 token。
4. _verify_mtp_v2(next_token_ids)
     调 mtp_verify kernel，把主模型预测与请求里存的草稿逐位比对，
     得到每请求的 accept_len（接受长度）与 accepted_index（逐行是否被接受）。
5. _draft_decode_func(...)   # vanilla 或 eagle
     draft 模型基于主模型 hidden state 再猜 k 个【下一轮】的草稿，
     调 mtp_scatter_next_token_ids 把新草稿写回请求结构。
6. post_handle：被接受的行产出 token、写共享内存；被拒绝的行回收 KV 显存索引。
```

注意第 4 步校验的是「上一轮猜的草稿」，第 5 步猜的是「下一轮要用的草稿」——这是理解 MTP 时间线的关键。

#### 4.1.3 源码精读

**构造期切换方法绑定**（[chunked_prefill/impl.py:L39-L51](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py#L39-L51)）：MTP 模式下，`self.prefill` / `self.decode` 被重新指向 `prefill_mtp` / `decode_mtp`；同时根据 `mtp_mode` 选定 draft 解码函数 `_draft_decode_eagle`（单模型复用）或 `_draft_decode_vanilla`（多模型链式）。

**主流程 `decode_mtp`**（[chunked_prefill/impl.py:L234-L332](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py#L234-L332)）：它的关键几句——

```python
model_input, run_reqs = prepare_decode_inputs(decode_reqs)        # 步骤 1：扩展 batch
...
model_output = self.model.forward(model_input)                    # 步骤 2：主模型并行前向
next_token_ids, _ = sample(model_output.logits, run_reqs, self.eos_id)  # 步骤 3：采样
...
mtp_accept_len, accepted_index = self._verify_mtp_v2(             # 步骤 4：并行校验
    new_next_token_ids=next_token_ids,
    b_req_idx=model_input.b_req_idx,
    b_req_mtp_start_loc=b_req_mtp_start_loc,
)
...
additional_mem_indexes_cpu = self._draft_decode_func(             # 步骤 5：draft 续猜下一轮
    main_model_input=model_input, main_model_output=model_output,
    next_token_ids=next_token_ids, mtp_accept_len=mtp_accept_len,
    b_req_mtp_start_loc=b_req_mtp_start_loc,
)
```

后续第 311–328 行做第 6 步：`accepted_index_cpu == 0` 的行（被拒绝的草稿）其 `mem_indexes` 被回收到 `mem_manager`，而被接受的行经 `_post_handle` 把 token 写回请求、推进输出。这正体现了 u4-l1 讲过的「token 级 KV 管理」——MTP 多分配的 \(k\) 个 KV 槽位，校验完立即按接受/拒绝精确回收。

**prefill 阶段也要「喂」draft 模型**（[chunked_prefill/impl.py:L185-L205](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py#L185-L205)）：`prefill_mtp` 在主模型 prefill 完成后调用 `_draft_prefill_forward`。注意这里有一句重要注释——「只是为了填充 draft model 的 KV，并不会使用生成的 token_id」。也就是说 prefill 阶段 draft 模型的产出被丢弃，目的只是让 draft 模型的 KV cache 与主模型对齐，为后续 decode 续猜做好准备。

**draft 模型加载入口**（[base_backend.py:L237-L239](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L237-L239)）：主模型 `init_model` 完成后，若 `args.mtp_mode` 为真就调用 `init_mtp_draft_model(kvargs)` 构造 draft 模型列表（详见 4.2）。

#### 4.1.4 代码实践

**实践目标**：在源码层面走通一次 `decode_mtp`，确认「校验旧草稿 → 猜新草稿」的时间线。

**操作步骤**：

1. 打开 [chunked_prefill/impl.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py#L234-L332)，定位 `decode_mtp`。
2. 在以下三处各画一个箭头标注它们读/写的对象：
   - `_verify_mtp_v2` 读取 `req_to_next_token_ids`（旧草稿的来源）。
   - `_draft_decode_func` 末尾的 `mtp_scatter_next_token_ids` 写回同一个 `req_to_next_token_ids`（新草稿的去向）。
   - 第 313 行 `model_input.mem_indexes_cpu[accepted_index_cpu == 0]`（被拒绝行的 KV 回收）。
3. 回答：为什么第 4 步「校验」必须发生在第 5 步「续猜」之前？（提示：续猜会覆盖 `req_to_next_token_ids`。）

**需要观察的现象**：你会确认 `req_to_next_token_ids` 是「上一轮写、这一轮读」的草稿缓冲，主模型前向的输入 batch 已经包含了待校验的草稿（由 `prepare_decode_inputs` 注入）。

**预期结果**：理清 MTP 一拍的因果链——`prepare_decode_inputs` 注入旧草稿 → 主模型并行前向 → `mtp_verify` 读旧草稿比对 → draft 续猜写新草稿 → 回收被拒 KV。

> 待本地验证：若你有 DeepSeek-V3 权重与对应 MTP draft 权重，可启动服务并观察日志中 `_update_mtp_accept_ratio` 上报的接受长度（见 4.3.4）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `prefill_mtp` 里 draft 模型的输出 token 被丢弃？
**答案**：prefill 阶段 draft 模型前向的目的只是「对齐 KV cache」，让 draft 模型在后续 decode 续猜时拥有正确的历史 KV；prefill 的「正式首 token」由主模型采样产生，draft 的猜测此刻还无主模型可校验，故丢弃（见 `_draft_prefill_forward` 上方注释）。

**练习 2**：`num_mtp_models` 在 eagle 与 vanilla 模式下分别是多少？
**答案**：eagle 模式为 1（一个 draft 模型循环复用 `mtp_step` 次），vanilla 模式为 `mtp_step`（每个推测深度一个独立 draft 模型）。见 [impl.py:L44-L45](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py#L44-L45)。

### 4.2 draft 模型

#### 4.2.1 概念说明

draft 模型是推测解码里「负责猜」的角色。在 DeepSeek-V3 MTP 设计中，每个 MTP 模块是一个**结构上与主模型同构**的 transformer 块：相同的层数、相同的注意力/FFN 结构，但**权重独立**。它的输入也不是 token id，而是「主模型最后一层的 hidden state」加上「上一步预测 token 的 embedding」。

LightLLM 的 draft 模型 `Deepseek3MTPModel` 有一个非常关键的设计：**大量复用主模型资源**。它继承自主模型类 `Deepseek2TpPartModel`，但通过覆写若干 `_init_*` 钩子，把以下资源直接指向主模型：

- **请求管理器 `req_manager`**：draft 模型不另建请求/KV 索引表，复用主模型的（KV 索引映射 `req_to_token_indexs` 共享）。
- **KV 内存池 `mem_manager`**：draft 模型不另开显存池，复用主模型的 `kv_buffer`。
- **词表 embedding `wte_weight_` 与 `lm_head_weight_`**：与主模型共享同一份权重对象，不重复加载。
- **RoPE 的 cos/sin 缓存表**：通过 `_init_custom` 复用主模型的 `_cos_cached`/`_sin_cached`。

draft 模型自身只额外加载：一个投影矩阵 `eh_proj`（把拼接后的 \[\text{norm}(e), \text{norm}(h)\] 投回 hidden 维）、两个 RMSNorm（`enorm`/`hnorm`）、一个 final norm，以及自己的 \(n\_layer\) 层 transformer 权重。

这样设计的根本原因是 **draft 模型与主模型在同一次前向里交替执行、共享同一批 KV 槽位与请求状态**，复用既省显存又免去同步开销。

#### 4.2.2 核心流程

`Deepseek3MTPModel.__init__` 的初始化仍走基类 `TpPartBaseModel` 的标准流水线（u3-l1 讲过的「插槽+组装」），但每个 `_init_*` 步骤都被改写成「拿主模型的」：

```text
_pre_init(kvargs):           # 在标准初始化前，先从 kvargs 取出主模型引用与前置 draft 模型链
    self.main_model = kvargs.pop("main_model")
    self.mtp_previous_draft_models = kvargs.pop("mtp_previous_draft_models")

super().__init__(kvargs)      # 走基类标准流水线，但下面的钩子都被覆写
  ├── _init_custom():         self._cos_cached/_sin_cached = main_model 的
  ├── _init_req_manager():    self.req_manager = main_model.req_manager
  ├── _init_mem_manager():    self.mem_manager = main_model.mem_manager
  ├── _init_weights():        自建 pre_post_weight 与 n_layer 层权重，
  │                           再把 wte/lm_head 指向 main_model 的共享对象
  └── _init_infer_layer():    层号偏移 = 主模型层数 + 前置 draft 模型层数之和
```

draft 模型前向的「投影」步骤（`pre_layer_infer`）数学上为：

\[
h' = W_{eh}\,\big[\,\text{RMSNorm}(e;\gamma_e)\ \Vert\ \text{RMSNorm}(h;\gamma_h)\,\big]
\]

其中 \(e\) 是上一步预测 token 的 embedding，\(h\) 是主模型（或上一个 draft 模块）输出的 hidden state，\(\Vert\) 表示沿特征维拼接，\(W_{eh}\) 即 `eh_proj` 权重，\(h'\) 再进入 draft 模型自己的 transformer 层。

#### 4.2.3 源码精读

**draft 模型主体**（[deepseek_mtp/model.py:L8-L65](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek_mtp/model.py#L8-L65)）。先看资源复用的三个钩子（[model.py:L21-L37](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek_mtp/model.py#L21-L37)）：

```python
def _pre_init(self, kvargs):
    self.main_model: TpPartBaseModel = kvargs.pop("main_model")
    self.mtp_previous_draft_models: List[TpPartBaseModel] = kvargs.pop("mtp_previous_draft_models")

def _init_custom(self):
    self._cos_cached = self.main_model._cos_cached     # 复用主模型 RoPE 表
    self._sin_cached = self.main_model._sin_cached

def _init_req_manager(self):
    self.req_manager = self.main_model.req_manager      # 复用请求/KV 索引管理

def _init_mem_manager(self):
    self.mem_manager = self.main_model.mem_manager      # 复用 KV 显存池
```

再看权重共享（[model.py:L39-L55](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek_mtp/model.py#L39-L55)）：draft 模型自建 `pre_post_weight` 与 \(n\_layer\) 层 `trans_layers_weight`（这些是它自己的权重，会从 draft 权重目录加载），但把词表与 lm_head 直接指向主模型：

```python
self.pre_post_weight.wte_weight_ = self.main_model.pre_post_weight.wte_weight_
self.pre_post_weight.lm_head_weight_ = self.main_model.pre_post_weight.lm_head_weight_
```

层号偏移（[model.py:L57-L65](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek_mtp/model.py#L57-L65)）：`_init_infer_layer` 把 `start_layer_index` 设为「主模型层数 + 所有前置 draft 模型层数」。这个偏移服务于 vanilla 模式下多个 draft 模块的链式拼接——每个 draft 模块知道自己在整条「主模型 + draft 链」里的层号起点，便于 RoPE 等按层号取参数的逻辑保持一致。

**draft 前处理的投影实现**（[pre_layer_infer.py:L17-L30](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek_mtp/layer_infer/pre_layer_infer.py#L17-L30)）：`_mtp_context_forward` 取出 `infer_state.mtp_draft_input_hiddens`（即主模型传来的 \(h\)），对 embedding 输入与 \(h\) 各做 RMSNorm，拼接后过 `eh_proj_weight_` 矩阵乘，得到 draft 模型 transformer 层的输入。`_mtp_token_forward`（[L32-L43](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek_mtp/layer_infer/pre_layer_infer.py#L32-L43)）在 decode 阶段做完全对称的事。

**draft 权重定义**（[pre_and_post_layer_weight.py:L11-L44](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek_mtp/layer_weights/pre_and_post_layer_weight.py#L11-L44)）：`eh_proj_weight_` 是一个 `in_dim=hidden*2, out_dim=hidden` 的 `ROWMMWeight`（对应上面的拼接投影），`enorm`/`hnorm`/`final_norm` 是三个 `RMSNormWeight`；而 `wte_weight_` 与 `lm_head_weight_` 显式置 `None`，注释写明「与 DeepseekV3 模型共享，不通过 load 加载」——这两份在 `_init_weights` 里被替换为主模型对象。

**draft 模型构造现场**（[base_backend.py:L292-L358](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L292-L358)）：`init_mtp_draft_model` 决定要建几个 draft 模型——`vanilla` 模式建 `mtp_step` 个（链式），`eagle` 模式只建 1 个（循环复用）。每建一个，都把 `main_model=self.model` 与 `mtp_previous_draft_models=self.draft_models.copy()`（已建好的 draft 列表的快照）塞进 `kvargs`，这正是 4.2.2 里 `_pre_init` 取出的两个字段。随后按 draft 权重的 `model_type`（如 `deepseek_v3`）选具体 draft 模型类，并断言其与 `mtp_mode` 的 with_att/no_att 匹配（如 `deepseek_v3` 必须配 `*_with_att`）。

#### 4.2.4 代码实践

**实践目标**：确认 draft 模型「复用主模型、不另起炉灶」的资源清单。

**操作步骤**：

1. 打开 [base_backend.py 的 init_mtp_draft_model](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L292-L358)，找到构造 `mtp_model_kvargs` 的字典（约 L308–L329）。
2. 列出哪些字段是从 `main_kvargs` 直接复用的（如 `load_way`、`data_type`、`mem_fraction`、`quant_cfg`），哪些是 MTP 专属新增的（`main_model`、`mtp_previous_draft_models`）。
3. 注意 `max_total_token_num` 取的是 `self.model.mem_manager.size`（[L310](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L310)）——它只是个「告知基类尺寸」的占位，因为 draft 模型随后会用 `_init_mem_manager` 把 `mem_manager` 换成主模型的。
4. 打开 [model.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek_mtp/model.py#L31-L37)，确认 `_init_req_manager` / `_init_mem_manager` 把上面那个占位的 `mem_manager` 又替换回了主模型的对象。

**需要观察的现象**：draft 模型虽然走了完整的 `TpPartBaseModel.__init__` 流程，但其 `mem_manager`、`req_manager`、`wte`、`lm_head`、RoPE 表最终都不是自己新建的，而是主模型的引用。

**预期结果**：你应当能写出一张「draft 模型资源来源」表，左边是资源名、右边是「主模型」或「自建」。

| 资源 | 来源 |
| --- | --- |
| `req_manager` | 主模型 |
| `mem_manager`（KV 池） | 主模型 |
| `wte_weight_` / `lm_head_weight_` | 主模型 |
| `_cos_cached` / `_sin_cached` | 主模型 |
| `eh_proj` / `enorm` / `hnorm` / `final_norm` | 自建（从 draft 目录加载） |
| \(n\_layer\) 层 transformer 权重 | 自建（从 draft 目录加载） |

#### 4.2.5 小练习与答案

**练习 1**：draft 模型为什么必须复用主模型的 `req_manager` 而不是自建？
**答案**：因为 draft 模型在同一次前向里与主模型共享同一批 token 的 KV 槽位与请求状态。`req_manager` 维护 `req_to_token_indexs`（请求→KV 索引映射）等结构，若 draft 自建一份，两边索引会对不上，KV 写入/读取会错位。见 [model.py:L31-L33](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek_mtp/model.py#L31-L33)。

**练习 2**：draft 模型的 `eh_proj` 权重输入维为什么是 `hidden_size * 2`？
**答案**：因为它的输入是把 embedding 的 RMSNorm 输出与 hidden state 的 RMSNorm 输出沿特征维拼接（长度 \(2\times\text{hidden}\)），再用 `eh_proj` 投回 hidden 维。见 [pre_and_post_layer_weight.py:L16-L24](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek_mtp/layer_weights/pre_and_post_layer_weight.py#L16-L24) 与 [pre_layer_infer.py:L25-L29](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek_mtp/layer_infer/pre_layer_infer.py#L25-L29)。

### 4.3 多 token 生成

#### 4.3.1 概念说明

「多 token 生成」是 MTP 落到工程上的两个关键动作：**主模型如何并行校验多个草稿**、**draft 模型如何把新一轮草稿写回**。这两个动作都依赖一个核心技巧——**批量扩展（batch expansion）**。

普通 decode 里，一个请求在 batch 中占 1 行。MTP 下，`prepare_decode_inputs` 把每个请求扩展成 \(1+\text{mtp\_step}\) 行：第 0 行（`b_mtp_index=0`）是真实 token，第 \(1..k\) 行（`b_mtp_index=1..k`）是上一轮 draft 模型猜的草稿。于是主模型一次前向就同时处理了「真实 token + \(k\) 个待校验草稿」，这就是「并行校验」的实现方式——用 batch 维换序列维。

校验结果用两个量表达：

- **`mtp_accept_len`**（每请求一个 int）：从第 1 个草稿起，主模型预测与草稿连续一致的长度（含基准）。设第 \(i\) 个草稿位置首次与主模型预测不一致，则接受长度为：

\[
\text{accept\_len} = \min\{\,i \mid \text{draft}[i] \ne \text{main\_pred}[i]\,\} + 1
\]

  若全部 \(k\) 个草稿都一致，则 \(\text{accept\_len} = k+1\)。被采纳的草稿数为 \(\text{accept\_len}-1\)。

- **`accepted_index`**（每行一个 0/1）：前 `accept_len` 行标 1（接受）、其余标 0（拒绝）。标 0 的行的 KV 槽位会被回收。

校验完成后，draft 模型基于主模型的 hidden state 续猜 \(k\) 个**新草稿**，用 `mtp_scatter_next_token_ids` 按 `accept_len` 把它们写回请求的草稿缓冲 `req_to_next_token_ids`，供下一拍校验。

#### 4.3.2 核心流程

```text
prepare_decode_inputs(reqs):           # 批量扩展
  对每个 req：
    追加 b_mtp_index=0  （真实 token，1 行）
    for step in range(req.mtp_step):
        追加 b_mtp_index=step+1 （待校验草稿，各 1 行）
  → batch_size = num_reqs * (1 + mtp_step)

main_model.forward(model_input):       # 并行前向 (1+k)*num_reqs 行
sample(...) → next_token_ids           # 主模型每行的「正确下一个 token」

# 用 b_mtp_index==0 的位置切分出每请求的起点 b_req_mtp_start_loc
mtp_verify(req_to_next_token_ids, b_req_mtp_start_loc, next_token_ids, b_req_idx)
  → mtp_accept_len[num_reqs], accepted_index[batch_size]

# draft 续猜 + 回写（vanilla 链式 / eagle 循环复用）
_draft_decode_vanilla / _draft_decode_eagle:
  for step in range(mtp_step):
      draft_input.input_ids = 上一步预测 token
      draft_input.mtp_draft_input_hiddens = 上一步 hidden
      draft_output = draft_models[...].forward(draft_input)
      all_next_token_ids.append(argmax(draft_output.logits))
  mtp_scatter_next_token_ids(..., all_next_token_ids, mtp_accept_len)
      # 按 accept_len 把新草稿写回 req_to_next_token_ids
```

`b_req_mtp_start_loc` 是「每个请求在扩展 batch 里的起始行号」——因为校验是逐请求做的，需要知道每个请求的 \(1+k\) 行从哪里开始。它由 `b_mtp_index==0` 的位置构成（[impl.py:L249](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py#L249)）。

#### 4.3.3 源码精读

**批量扩展在 `prepare_decode_inputs`**（[generic_pre_process.py:L98-L162](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/generic_pre_process.py#L98-L162)）：

```python
for req in req_objs:
    ...
    b_mtp_index.append(0)                       # 真实 token
    for step in range(req.mtp_step):
        run_reqs.append(req)                    # 同一请求重复入列
        b_req_idx.append(req.req_idx)
        seq_len += 1
        b_seq_len.append(seq_len)
        b_mtp_index.append(step + 1)            # 待校验草稿
```

注意草稿行的 `seq_len` 是递增的（每多一个草稿，序列长度 +1），这对应「真实 token 之后接 draft1、draft1 之后接 draft2……」的链式语义。`req.mtp_step` 来自请求对象（[infer_batch.py:L558-L564](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/infer_batch.py#L558-L564)），其值就是全局 `--mtp_step`。

**并行校验 kernel `mtp_verify`**（[mtp_utils.py:L46-L87](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/mtp_utils.py#L46-L87)）及其核函数（[mtp_utils.py:L6-L43](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/mtp_utils.py#L6-L43)）。每个请求由一个 program 处理，关键比对逻辑：

```python
# offset 遍历该请求的草稿槽位
cur_next_token_id     = load(req_to_next_token_ids[req][offset + 1])   # 草稿[offset+1]
cur_new_next_token_id = load(new_next_token_ids[start + offset])       # 主模型对位置 offset 的预测
match_mask = (cur_next_token_id == cur_new_next_token_id)
mismatch_positions = where(match_mask, BLOCK_SIZE, offset)
first_mismatch_pos = min(mismatch_positions)
accept_len = first_mismatch_pos + 1
store(mtp_accept_len[cur], accept_len)
store(accepted_index[start + offset], offset < accept_len ? 1 : 0)
```

这里「主模型对位置 `offset` 的预测」对应 batch 中 `b_mtp_index=offset` 那一行之后的正确 token，它应当等于草稿缓冲里第 `offset+1` 个草稿（因为草稿缓冲第 0 位是真实 token 本身）。逐位比较即可得到连续命中前缀长度。注意 `BLOCK_SIZE=16`，故 `mtp_step` 不得超过 16（[L66](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/mtp_utils.py#L66) 的断言）。

**回写新草稿 `mtp_scatter_next_token_ids`**（[mtp_utils.py:L122-L148](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/mtp_utils.py#L122-L148)，核函数 [L90-L119](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/mtp_utils.py#L90-L119)）：draft 续猜产出的 `all_next_token_ids` 形状是 `[batch_size, mtp_step+1]`（每行 mtp_step+1 个新草稿候选）。kernel 依据每请求的 `accept_len`，从该请求的候选行里取「自 `accept_len-1` 起的 mtp_step 个」写回 `req_to_next_token_ids[req][0..mtp_step-1]`——即把「接续在被接受前缀之后的新草稿」铺到草稿缓冲，下一拍校验就从这里读。

**两种 draft 续猜策略**：

- vanilla 链式（[impl.py:L349-L382](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py#L349-L382)）：循环 `mtp_step` 次，每次调用链中下一个 draft 模型（`draft_models[draft_model_idx]`），把上一步的 hidden 与预测 token 喂进去，argmax 得到下一步草稿，最终 stack 成 `[batch, mtp_step+1]` 并 scatter。
- eagle 循环复用（[impl.py:L384-L432](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py#L384-L432)）：只用 1 个 draft 模型（`draft_model_idx = _step % num_mtp_models`），但每步要为「新草稿」额外分配 KV 槽位（`eagle_mem_indexes`，因为单模型复用模式下草稿的 KV 不在扩展 batch 里预占），并相应推进 `b_seq_len`/`max_kv_seq_len`。

**draft 输入构造 `prepare_mtp_prefill_inputs`**（[mtp_pre_process.py:L7-L24](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/mtp_pre_process.py#L7-L24)）：它把上一步预测 token（`b_next_token_ids`）拼到每个序列末尾、整体左移一位，构造 draft 模型的 `input_ids`，并把主模型 hidden（`mtp_draft_input_hiddens`）挂到新 `ModelInput` 上。具体的「左移+末尾插入」由 Triton kernel `gen_mtp_new_input_ids` 完成（[gen_mtp_prefill_params.py:L26-L51](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/gen_mtp_prefill_params.py#L26-L51)）——它对每个序列把原 input_ids 整体前移一位、腾出的末位填入预测 token。

**主模型额外输出 hidden**（[basemodel.py:L736-L739](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L736-L739)）：当 `is_mtp_mode` 为真时，主模型在 post 层把最后一层的 `input_embs`（即进 lm_head 前的 hidden）做 `_tpsp_allgather` 后存入 `model_output.mtp_main_output_hiddens`，这正是 draft 模型 `mtp_draft_input_hiddens` 的来源（字段定义见 [batch_objs.py:L110-L113](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/batch_objs.py#L110-L113)）。

#### 4.3.4 代码实践

**实践目标**：用一个内置单测直观理解 `mtp_verify` 的「连续命中前缀」语义；并理清三个启动参数的校验关系。

**操作步骤（源码阅读 + 本地验证）**：

1. 阅读 [mtp_utils.py 末尾的 test_mtp_verify](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/mtp_utils.py#L254-L272)：
   ```python
   req_to_next_token_ids = [[1, 2,-2,-1,-1],   # 请求0：真实=1，草稿=[2,-2,...]
                            [1, 2, 0,-1,-1]]    # 请求2：真实=1，草稿=[2,0,...]
   new_next_token_ids     = [1, 4, 2, 4,13]     # 主模型对各行(含草稿行)的预测
   ```
   手算：请求0 中主模型对位置0预测=1，应等于草稿[1]=2 → 不匹配 → accept_len=1；请求2 中主模型对位置0预测=2，等于草稿[1]=2 → 对位置1预测=4，等于草稿[2]=0？ 否 → accept_len=2。
2. 在具备 GPU 的环境执行（**待本地验证**）：
   ```bash
   cd <repo_root>
   python -m lightllm.common.basemodel.triton_kernel.mtp_utils
   ```
   预期打印 `mtp_accept_len ≈ [1, 2]`，`accepted_index` 前 `accept_len` 位为 1、其余为 0。
3. 阅读启动参数校验（[api_start.py:L213-L219](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L213-L219)）：
   ```python
   if args.mtp_mode is not None:
       assert args.mtp_draft_model_dir is not None
       assert args.mtp_step > 0
   else:
       assert args.mtp_draft_model_dir is None
       assert args.mtp_step == 0
   ```
   含义：开启 MTP（`mtp_mode != None`）必须同时给 draft 模型目录且 `mtp_step>0`；关闭时三者必须全部「空/零」。三者强绑定，缺一即启动断言失败。
4. 对照参数定义（[api_cli.py:L688-L720](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L688-L720)）：`--mtp_mode` 取值为四种模式或 `None`；`--mtp_draft_model_dir` 用 `nargs="+"` 接收**一个或多个**目录（vanilla 模式下每深度一个目录）；`--mtp_step` 默认 0，help 明确「DeepSeekV3 currently only support 1 step」。

**需要观察的现象**：手算与 kernel 输出一致，证明 `accept_len` 就是「连续命中前缀长度」；启动校验体现「三个参数要么全开、要么全关」的强一致约束。

**预期结果**：能口头复述——「`mtp_verify` 比对的是主模型对位置 `i` 的预测与草稿缓冲第 `i+1` 位，连续相等的前缀长度即接受长度；`mtp_scatter_next_token_ids` 把接续的新草稿按接受长度铺回缓冲」。并理解 `--mtp_step` 受限（DeepSeek-V3 仅支持 1）的工程现状。

> 待本地验证：步骤 2 的 kernel 运行结果与手算对齐；若有 DeepSeek-V3 主模型 + MTP draft 权重，可启动服务观察日志里 `_update_mtp_accept_ratio`（[base_backend.py:L773-L781](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L773-L781)）按 `accept_len-1` 累计的每请求接受 token 数。

#### 4.3.5 小练习与答案

**练习 1**：`prepare_decode_inputs` 里草稿行的 `b_seq_len` 为什么是递增的（`seq_len += 1`）而不是都等于真实 token 的长度？
**答案**：因为草稿行表示「真实 token 之后再接 draft1、draft1 之后再接 draft2」的递进序列。第 `i` 个草稿对应的序列长度是「真实长度 + i」，注意力需要看到它之前的全部 token（含更早的草稿），故 `b_seq_len` 递增。见 [generic_pre_process.py:L117-L125](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/generic_pre_process.py#L117-L125)。

**练习 2**：为什么 `mtp_verify` 里主模型预测取 `new_next_token_ids[start+offset]`，而草稿取 `req_to_next_token_ids[req][offset+1]`（偏移 +1）？
**答案**：草稿缓冲第 0 位存的是真实 token 本身，第 1 位才是第一个草稿。主模型对 batch 中 `b_mtp_index=offset`（即第 offset 行）的预测，应当对应「该行 token 之后」的 token，也就是草稿缓冲的第 `offset+1` 位，所以比较时草稿侧要 +1。见 [mtp_utils.py:L29-L39](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/mtp_utils.py#L29-L39)。

**练习 3**：若只指定 `--mtp_mode vanilla_with_att` 却忘了给 `--mtp_draft_model_dir`，会发生什么？
**答案**：启动期 `api_start.py` 的断言 `assert args.mtp_draft_model_dir is not None` 失败，进程直接 `AssertionError` 退出，不会进入模型加载阶段。见 [api_start.py:L214-L216](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L214-L216)。

## 5. 综合实践

把三个模块串起来，完成一次「源码级端到端追踪」：

**任务**：假设以 `--mtp_mode vanilla_with_att --mtp_step 1 --mtp_draft_model_dir <ds3_mtp_dir> --model_dir <ds3_dir>` 启动，请按时间顺序画出「服务启动 → 首次 prefill → 第一拍 decode」涉及 MTP 的关键调用与数据流转图。

**建议步骤**：

1. **启动期**：`api_start.py` 参数校验（三参数强绑定）→ `ModeBackend.init_model` 建主模型 → `init_mtp_draft_model`（因 `mtp_step=1` 且 vanilla，建 1 个 `Deepseek3MTPModel`，把 `main_model` 注入）→ draft 模型经 `_init_*` 钩子复用主模型 `mem_manager`/`req_manager`/`wte`/`lm_head`/RoPE。
2. **首次 prefill**：backend 调 `prefill_mtp` → 主模型 forward 并在 `is_mtp_mode` 下额外输出 `mtp_main_output_hiddens` → `_draft_prefill_forward` 用 `prepare_mtp_prefill_inputs` 拼输入、跑 draft 模型**只为对齐 KV**、丢弃其 token 输出。
3. **第一拍 decode**：`decode_mtp` → `prepare_decode_inputs` 把每个请求扩展成 2 行（`b_mtp_index=0,1`）→ 主模型 forward 2 行 → `sample` → `_verify_mtp_v2`（`mtp_verify` 比对，得到 accept_len∈{1,2}）→ `_draft_decode_vanilla`（循环 1 次猜新草稿 + `mtp_scatter_next_token_ids` 回写）→ 被拒行的 `mem_indexes` 回收 → 被接受行经 `_post_handle` 产出 token。
4. 在图上标注每一步读/写 `req_to_next_token_ids` 的方向（旧草稿被读、新草稿被写）。

**验收标准**：你的图能回答三个问题——(a) draft 模型复用了主模型的哪 5 项资源？(b) 主模型如何做到「一次前向校验多个草稿」？(c) 一拍 decode 里 `req_to_next_token_ids` 被读一次（校验）又被写一次（续猜），顺序为何不能颠倒？

## 6. 本讲小结

- **MTP 是对 decode 的加速**：用轻量 draft 模型先猜 \(k\) 个草稿，主模型把「真实 token + \(k\) 草稿」拼成 \(k+1\) 行**一次性并行校验**，命中前缀全部采纳，瓶颈从「主模型串行 k 次」变为「主模型并行 1 次」。
- **方法绑定在构造期切换**：MTP 模式下 `ChunkedPrefillBackend` 把 `prefill`/`decode` 换成 `prefill_mtp`/`decode_mtp`，并按 `mtp_mode` 选 vanilla（多 draft 模型链式）或 eagle（单 draft 模型循环复用）策略。
- **draft 模型重度复用主模型**：`Deepseek3MTPModel` 经覆写 `_init_req_manager`/`_init_mem_manager`/`_init_weights`/`_init_custom` 共享主模型的请求管理器、KV 内存池、词表、lm_head 与 RoPE 表，自身只多出 `eh_proj` 投影与若干 norm。
- **多 token 生成的核心是批量扩展**：`prepare_decode_inputs` 用 `b_mtp_index` 把每请求扩成 \(1+k\) 行；`mtp_verify` 逐请求比对主模型预测与草稿、产出 `accept_len` 与 `accepted_index`；`mtp_scatter_next_token_ids` 把下一轮新草稿按接受长度回写。
- **时间线是「校验旧草稿 → 猜新草稿」**：`req_to_next_token_ids` 上一拍被写、本拍被读，校验必须在续猜之前；被拒行的 KV 槽位立即按 token 级回收。
- **三个启动参数强绑定**：`--mtp_mode` 非 None 时必须同时给 `--mtp_draft_model_dir` 且 `--mtp_step>0`，否则 `api_start.py` 断言失败；目前 DeepSeek-V3 仅支持 `mtp_step=1`。

## 7. 下一步学习建议

- **动手验证**：若具备 DeepSeek-V3 与配套 MTP draft 权重，按本讲 4.3.4 的命令启动服务，观察 `_update_mtp_accept_ratio` 上报的接受长度，体会「命中率→加速比」的关系。
- **横向对比**：阅读 [lightllm/models/qwen3_moe_mtp/model.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/qwen3_moe_mtp/model.py) 与 [lightllm/models/mistral_mtp/model.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/mistral_mtp/model.py)，对比 `*_no_att` 模式下 draft 模型与 DeepSeek 的 `*_with_att` 在结构上的差异。
- **eagle 深读**：回到 [impl.py 的 `_draft_decode_eagle`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py#L384-L432)，理解单 draft 模型循环复用时为何要额外 `alloc` KV 槽位（`eagle_mem_indexes`）。
- **延伸阅读**：MTP 的原始设计见 DeepSeek-V3 技术报告的 Multi-Token Prediction 章节；推测解码的一般理论可参考 Leviathan 等的 *Fast Inference from Transformers via Speculative Decoding*。
- **后续讲义**：结合 u7-l6（约束解码）理解 MTP 与结构化输出约束的兼容性，结合 u7-l7（指标监控）理解如何把 `mtp_accept_ratio` 暴露为可观测指标。
