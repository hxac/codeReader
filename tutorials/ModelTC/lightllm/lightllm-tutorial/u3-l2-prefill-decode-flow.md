# prefill 与 decode 推理主流程

## 1. 本讲目标

本讲紧接 [u3-l1 TpPartBaseModel 推理框架](./u3-l1-tp-part-base-model.md)：那里我们看到了「一个模型 = 子类填好的六个插槽 + 基类一条写死的初始化流水线」。本讲要回答的是：**模型初始化完成后，当一次前向推理真正到来时，代码究竟是怎么跑的？**

读完本讲，你应该能够：

1. 说清楚 **prefill（上下文填充）** 与 **decode（逐 token 解码）** 两阶段在职责、输入、调度上的根本差异。
2. 画出一条 **`ModelInput → InferStateInfo → ModelOutput`** 的数据流，并指出每个对象在哪一行被创建、被消费。
3. 读懂 `_prefill` / `_decode` 两个方法，理解 padding（补齐）与 unpadding（还原）为什么是 CUDA Graph 的前置条件。
4. 说清楚 `_context_forward` 与 `_token_forward` 这两个真正"跑 transformer 层"的函数各自的分工。

---

## 2. 前置知识

在进入源码前，先用最朴素的语言建立三个直觉。

### 2.1 大模型生成文本的两段式过程

一个自回归语言模型（GPT/Llama/Qwen 这类）生成一段回答，本质上分两步：

- **prefill（预填充）**：把用户输入的整条 prompt（比如 2000 个 token）一次性喂给模型，算出每个位置的隐藏状态、并把这些 token 的 K/V 写进 KV Cache。这一步是**并行**的——所有 token 一起算。计算量大、显存压力大，但只做一次。
- **decode（解码）**：prefill 产出的最后一个 logits 采样出第 1 个新 token；之后每一步只把**上一步刚生成的 1 个 token** 喂进去，复用已有 KV Cache，再采样出下一个 token。每步计算量很小，但要做很多次（生成多少个字就做多少步）。

一句话：**prefill 是"读题"，decode 是"一个字一个字地答"**。所有推理框架的核心都是把这两步高效地跑起来。

### 2.2 为什么要 token 级别地管理 KV Cache

回顾 [u1-l1](./u1-l1-project-overview.md) 提到的 LightLLM 三大特色之一——**token 级 KV Cache 管理**。prefill 时模型会把每个输入 token 的 K/V 存到一片预分配的显存里；decode 时新 token 的 K/V 追加进去。为了知道"第 i 个请求的第 j 个 token 的 K/V 存在显存的哪个槽位"，LightLLM 维护了一张映射表 `req_to_token_indexs`（请求→token 索引表）。本讲你会反复看到向这张表"登记"的动作：

- prefill 时调用 `init_req_to_token_indexes`（一次性登记一批 token）；
- decode 时调用 `copy_kv_index_to_req`（每个请求只登记 1 个新 token）。

### 2.3 CUDA Graph 与"形状对齐"

CUDA Graph 是把一串 GPU 操作录制成一张"图"，之后直接整体重放，省掉每次 kernel launch 的 CPU 开销。但**录制时张量的形状是固定的**，重放时形状必须一致。所以为了能用 CUDA Graph 加速 decode，LightLLM 会把 batch size（如 7）补齐到最近的可录制档位（如 8），推理完再裁掉补的假数据。这就是 `_prefill` / `_decode` 里反复出现的 **pad → 推理 → unpad** 三段式。

> 数学上记：若真实 batch size 为 \(b\)，可选档位集合为 \(S\)，则选择
> \[ b' = \min\{s \in S \mid s \ge b\} \]
> 补 \(b'-b\) 个占位请求，推理后取输出前 \(b\) 行还原。

---

## 3. 本讲源码地图

本讲聚焦两个核心文件，并用若干周边文件佐证调用关系。

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| `lightllm/common/basemodel/basemodel.py` | 模型基类，包含 `forward`/`_prefill`/`_decode`/`_context_forward`/`_token_forward` | **主战场** |
| `lightllm/common/basemodel/batch_objs.py` | 定义 `ModelInput` 与 `ModelOutput` 两个数据类 | **输入输出结构** |
| `lightllm/common/basemodel/infer_struct.py` | `InferStateInfo` 推理状态结构及其初始化 | 桥接结构与层推理 |
| `lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py` | 单层 transformer 推理模板（`context_forward`/`token_forward`） | 层内部的分工 |
| `lightllm/server/router/model_infer/mode_backend/generic_pre_process.py` | 后端如何把请求 `InferReq` 打包成 `ModelInput` | 看输入从哪来 |
| `lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py` | 后端如何调用 `forward` 并采样 | 看输出到哪去 |

---

## 4. 核心概念与源码讲解

### 4.1 统一入口 `forward` 与输入输出结构

#### 4.1.1 概念说明

无论是 prefill 还是 decode，从后端 `ModeBackend` 的视角看，模型就是一个函数：

\[ \text{model}: \text{ModelInput} \longrightarrow \text{ModelOutput} \]

- **`ModelInput`**：一次推理需要的全部输入——token id、每个请求的序号、KV 槽位索引、batch 形状信息等，全部打包成一个 dataclass。
- **`ModelOutput`**：推理产出——主要是最后一层的 `logits`（每个位置对词表的打分），以及少量特殊模式的额外输出。

模型基类对外只暴露一个 `forward(model_input)` 入口，内部用 `model_input.is_prefill` 这个布尔字段决定走 `_prefill` 还是 `_decode`。这种"一个入口、按标志分发"的设计，让上层后端代码完全不用关心当前是 prefill 还是 decode。

#### 4.1.2 核心流程

```text
后端 prepare_prefill_inputs / prepare_decode_inputs
          │  把若干 InferReq 打包成
          ▼
      ModelInput (is_prefill=True/False)
          │
          ▼
   TpPartBaseModel.forward(model_input)
          │  ① to_cuda() 把张量搬上 GPU
          │  ② 按 is_prefill 分发
          ├── is_prefill=True  ──► _prefill  ──► _context_forward
          └── is_prefill=False ──► _decode    ──► _token_forward
          │
          ▼
      ModelOutput (logits, ...)
          │
          ▼
   后端 _sample_and_scatter_token 做 top-k/top-p 采样
```

#### 4.1.3 源码精读

入口 `forward` 非常薄，只做两件事：把输入搬上 GPU，然后按 `is_prefill` 分发：

[`forward` 方法](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L342-L350)（`basemodel.py:342-350`）——把 `model_input.to_cuda()` 后按 `is_prefill` 走 `_prefill` 或 `_decode`，这是全篇的"总开关"。

`ModelInput` 用 `@dataclass` 声明，字段分通用与特殊两类。下面挑 prefill/decode 各自最关键的字段解读（见 [`batch_objs.py:9-98`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/batch_objs.py#L9-L98)）：

- `is_prefill`（L39）：阶段标志，决定整条链路走向。
- `input_ids`（L19）：要推理的 token 序列。**prefill 时是整段 prompt，decode 时为 `None`**（到 `_decode` 里再现场 gather，见 4.3.3）。
- `b_req_idx`（L20）：每个 token/请求属于哪个请求的编号。
- `b_seq_len`（L22）：每个请求当前的序列总长（已存 KV 长度 + 本轮新算的）。
- `b_ready_cache_len`（L40）：**仅 prefill 用**，表示该请求已有多少 KV 是命中前缀缓存、不用重算的（来自 RadixCache，见 [u4-l2](./u4-l2-radix-prefix-cache.md)）。
- `mem_indexes`（L38）：本轮新分配的 KV 槽位索引（由内存管理器 `alloc` 出来）。
- `b_position_delta`（L43）：**仅 decode 用**，多模态 MRoPE 模型需要的位移量；普通模型为 0。注意 `to_cuda()` 里有一条断言（`batch_objs.py:76-78`）：decode 必须提供 `b_position_delta`，prefill 必须不提供——这正是阶段区分的"硬约束"。

`to_cuda()`（[`batch_objs.py:59-91`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/batch_objs.py#L59-L91)）负责把这些 CPU 张量搬到 GPU：`input_ids`、`mem_indexes`、`b_req_idx`、`b_seq_len` 等逐个 `.cuda(non_blocking=True)`，并对 decode 的 diverse 模式相关字段做懒初始化。

输出侧的 `ModelOutput`（[`batch_objs.py:100-119`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/batch_objs.py#L100-L119)）则简单得多：核心字段 `logits`，外加 `prefill_mem_indexes_ready_event`（一个 CUDA Event，用来标记"本轮分配的 KV 槽位索引已安全写回"，供 PD 分离等延迟更新场景用）和 MTP 模式专用的 `mtp_main_output_hiddens`。

> 想知道 `ModelInput` 的字段是怎么从请求填出来的，可读后端的打包函数：`prepare_prefill_inputs`（[`generic_pre_process.py:12-95`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/generic_pre_process.py#L12-L95)）与 `prepare_decode_inputs`（[`generic_pre_process.py:98-162`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/generic_pre_process.py#L98-L162)）。注意两者的差别正好对应阶段差异：prefill 里 `input_ids` 是真 token、有 `b_ready_cache_len`、`is_prefill=True`；decode 里 `input_ids=None`、有 `b_position_delta`、`is_prefill=False`。

#### 4.1.4 代码实践

**实践目标**：亲手验证"阶段由 `is_prefill` 一个字段决定"，并看懂一次最小的 `forward` 调用需要哪些字段。

**操作步骤**：

1. 打开 `basemodel.py` 的 `_check_max_len_infer` 方法（[`basemodel.py:1030-1090`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L1030-L1090)）。这是初始化时"试跑一次最大长度 prefill"的代码，它手工构造了一个最小的 `ModelInput` 并调用 `self.forward(model_input)`，是理解输入字段最干净的范例。
2. 对照该处构造 `ModelInput` 时传入的参数，逐个在 `batch_objs.py` 的 `ModelInput` 定义里找到对应字段，并思考：为什么这里 `b_ready_cache_len` 全 0、`prefix_total_token_num=0`？（答案：试跑时没有任何前缀缓存复用。）
3. 在 `forward`（`basemodel.py:347`）的 `if model_input.is_prefill:` 分支处，把试跑里的 `is_prefill=True` 想象成 `False`，追问：此时 `_decode` 会因为缺少哪个字段而失败？（提示：看 `to_cuda()` 的断言。）

**需要观察的现象**：你会看到 `_check_max_len_infer` 走的是 prefill 分支，因为试跑模拟的是"读入一条最长 prompt"。

**预期结果**：能口头复述"一个 `ModelInput` 至少要带 `input_ids`、`mem_indexes`、`b_req_idx`、`b_seq_len`、`is_prefill`，prefill 还要 `b_ready_cache_len`，decode 还要 `b_position_delta`"。

> 待本地验证：如果你有 GPU 环境，可在 `_check_max_len_infer` 的 `forward` 调用前后加日志打印 `model_input.is_prefill` 与 `model_output.logits.shape`，确认 logits 形状为 `[total_token_num, vocab_size]`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ModelOutput` 里要放一个 `prefill_mem_indexes_ready_event`，而 decode 没有？

**参考答案**：prefill 会一次性分配并写回大量 KV 槽位索引（`init_req_to_token_indexes`），在某些延迟更新场景（如 PD 分离、overlap）下，后续步骤必须等这次写入真正落到 GPU 才能安全读取这些索引，所以用一个 CUDA Event 做同步屏障；decode 每轮只处理 1 个 token/请求，登记量极小，不需要这个事件。

**练习 2**：`to_cuda()` 里 `b_position_delta` 的两条断言（`batch_objs.py:76-78`）分别要求什么？

**参考答案**：要求 `b_position_delta is not None` 时必须是 decode（`is_prefill is False`）；`b_position_delta is None` 时必须是 prefill（`is_prefill is True`）。即"decode 必给、prefill 必不给"，用断言把阶段标志和字段一致性钉死。

---

### 4.2 prefill 推理流程

#### 4.2.1 概念说明

`_prefill` 负责处理一批新到的 prompt（或 chunked prefill 下的一块）。它要做的事情比 `_decode` 多，因为 prefill 是计算密集的，需要更多准备：

- **padding 到 CUDA Graph 档位**：当开启了 prefill CUDA Graph（`--enable_prefill_cudagraph`）时，把本轮要处理的 token 数补齐到可录制的档位。
- **登记 KV 索引**：把本轮新分配的槽位写进 `req_to_token_indexs` 表。
- **构造推理状态**：`InferStateInfo`，并现算派生张量（位置编码、cu_seqlens 等）。
- **真正跑层**：调用 `_context_forward` 走一遍 pre_infer → N 层 transformer → post_infer。
- **unpadding 还原**：把 padding 多算的那部分 logits 裁掉。

注意 prefill 的"补齐"对象是 **token 数**（`handle_token_num`），而不是 batch size——因为 prefill 一层要处理的总 token 数 = Σ(各请求本轮新增 token)。补齐时是**多挂一个假请求**（占位请求），见 `_create_padded_prefill_model_input` 的 `batch_size + 1`。

#### 4.2.2 核心流程

```text
_prefill(model_input):
  ① (可选) prefill_decode_mixed 时重写 decode 请求的 input_ids
  ② 记录原始 token 数 / batch size（unpad 时要用）
  ③ (TPSP 模式) 把 token 数向上取整为 tp_world_size 的整数倍
  ④ (prefill CUDA Graph 可跑时) 取最近的图档位作为 infer_handle_token_num
  ⑤ _create_padded_prefill_model_input：补一个占位请求，凑齐 token 数
  ⑥ _create_inferstate：ModelInput → InferStateInfo
  ⑦ init_req_to_token_indexes：把新槽位登记进 req_to_token_indexs 表
  ⑧ record 一个 CUDA Event 作为 prefill_mem_indexes_ready_event
  ⑨ init_some_extra_state + init_att_state：现算位置/注意力派生状态
  ⑩ _context_forward(infer_state)：真正跑层 → ModelOutput
  ⑪ _create_unpad_prefill_model_output：裁掉 padding 的 logits
  ⑫ 把 event 挂到 model_output 上返回
```

#### 4.2.3 源码精读

[`_prefill` 方法](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L538-L592)（`basemodel.py:538-592`）——逐段看：

- L552-553：先记下原始"要处理的 token 数"`origin_handle_token_num = total_token_num - prefix_total_token_num`（总 token 减去已命中前缀缓存的，才是真正要算的）和原始 batch size，这两者留给 unpad 用。
- L555-563：决定本轮推理的 token 数 `infer_handle_token_num`。若开启 TPSP 混合并行，向上取整为 `tp_world_size` 倍数；若 prefill CUDA Graph 能跑（`can_run`），则用 `find_closest_graph_handle_token_num` 取最近档位。
- L565-567：调用 `_create_padded_prefill_model_input` 补齐（[`basemodel.py:459-504`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L459-L504)），它把 `batch_size += 1`、用 `HOLD_REQUEST_ID`/`HOLD_TOKEN_MEMINDEX` 填一个占位请求，并重算 `b_prefill_start_loc`（cumsum 偏移）。
- L569：`_create_inferstate` 把 `ModelInput` 翻译成 `InferStateInfo`（详见 4.4 节对状态的拆解）。
- L570-578：`init_req_to_token_indexes` 登记新槽位——内部调用 triton kernel `copy_kv_index_to_req_prefill`（[`copy_kv_index_to_req.py:41-71`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/copy_kv_index_to_req.py#L41-L71)），把每个请求本轮新增的 token 槽位写进 `req_to_token_indexs[req_idx][offset]`。
- L579-580：录制 CUDA Event 作为"索引已写回"的同步点。
- L582-583：`init_some_extra_state(self)` 现算 `b_q_seq_len`/`cu_q_seq_len`/`position_ids` 等（prefill 走 `gen_prefill_params`，见 [`infer_struct.py:105-118`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/infer_struct.py#L105-L118)）；`init_att_state()` 让注意力后端初始化自己的状态。
- L584：调用 `_context_forward` 真正跑层（4.4 节详解）。
- L586-591：`_create_unpad_prefill_model_output` 裁掉 padding（默认只取前 `origin_batch_size` 个请求的 logits；`return_all_prompt_logics` 模式下取前 `origin_handle_token_num` 个 token），再挂上 event。

#### 4.2.4 代码实践

**实践目标**：跟踪一次 prefill 的"padding → 推理 → unpadding"闭环，理解补齐与还原是严格配对的。

**操作步骤**：

1. 在 `_prefill` 的 L552 打印 `origin_handle_token_num, origin_batch_size`，在 L565 之后打印 padding 后的 `model_input.batch_size`、`model_input.total_token_num`，在 L586 之后打印 `model_output.logits.shape[0]`。
2. 发送一条较长 prompt 的请求（或在 `_check_max_len_infer` 触发），观察三者关系。

**需要观察的现象**：当未开启 prefill CUDA Graph 且未开 TPSP 时，padding 前后 token 数不变（`_create_padded_prefill_model_input` 第一行 `if ... == new_handle_token_num: return model_input` 直接返回，见 `basemodel.py:460-461`），logits 行数等于原始请求数；一旦开启 `--enable_prefill_cudagraph` 且 token 数不在档位上，会看到 batch size 多 1、token 数变大、最后 unpatch 裁回原始行数。

**预期结果**：能解释"padding 加了一个假请求、unpad 又把它裁掉了"，即补齐不影响最终输出语义。

> 待本地验证：prefill CUDA Graph 默认关闭，需显式加 `--enable_prefill_cudagraph` 才能观察到 padding 行为。

#### 4.2.5 小练习与答案

**练习 1**：`origin_handle_token_num` 为什么要用 `total_token_num - prefix_total_token_num`，而不是直接用 `total_token_num`？

**参考答案**：`prefix_total_token_num` 是本轮请求中**已经命中前缀缓存、不必重算**的 token 总数（等于 `sum(b_ready_cache_len)`）。真正需要送进模型计算的只是"新增"那部分，所以"要处理的 token 数"必须减掉前缀部分。

**练习 2**：`_create_padded_prefill_model_input` 里占位请求的 `b_req_idx` 用的是 `self.req_manager.HOLD_REQUEST_ID`，它代表什么？

**参考答案**：`HOLD_REQUEST_ID` 是一个保留的"占位请求号"，表示这一行不是任何真实请求，仅用于把 token 数补齐到 CUDA Graph 槽位；它的 logits 会在 unpad 时被裁掉，绝不参与采样输出。

---

### 4.3 decode 推理流程

#### 4.3.1 概念说明

`_decode` 处理的是"已经在生成中的请求"——每个请求本轮只新增 1 个 token（MTP 推测解码下可能是多步，但每步仍只算 1 个）。和 prefill 相比，decode 有三点关键不同：

1. **input_ids 是现场拼的**：`prepare_decode_inputs` 传入的 `input_ids=None`，因为"上一步生成的 token id"在采样后才写到 `req_to_next_token_ids` 表里，`_decode` 开头要用 `gather_token` 现取。
2. **padding 对象是 batch size**：decode 一层处理的 token 总数 = batch size（每请求 1 个），所以补齐是对 batch size 取最近 CUDA Graph 档位（`find_closest_graph_batch_size`）。
3. **KV 索引登记极简**：每请求只新增 1 个 token，所以用更轻的 `copy_kv_index_to_req`（每请求写 1 个槽位），而不是 prefill 的批量版。

decode 是延迟敏感的（每生成一个字就要做一次），所以它最积极地把整步推理包进 CUDA Graph：`need_capture` 时录制、否则 `replay`。

#### 4.3.2 核心流程

```text
_decode(model_input):
  ① input_ids 为 None 时，gather_token 从 req_to_next_token_ids 现取本步 token
  ② 记录原始 batch size（unpad 用）
  ③ (TPSP) batch size 向上取整为 tp_world_size 倍
  ④ 分两路：
     [CUDA Graph 可跑]
       ④a find_closest_graph_batch_size 取档位
       ④b _create_padded_decode_model_input 补占位请求
       ④c _create_inferstate；is_cuda_graph = need_capture
       ④d copy_kv_index_to_req 登记每请求 1 个新槽位
       ④e init_some_extra_state + init_att_state
       ④f need_capture ? graph.capture_decode(_token_forward) : graph.replay
       ④g _create_unpad_decode_model_output 裁回
     [不可跑] 走非图路径：同样 pad→state→登记→_token_forward→unpad
```

#### 4.3.3 源码精读

[`_decode` 方法](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L594-L653)（`basemodel.py:594-653`）：

- L599-604：`input_ids is None` 时用 `gather_token` 现取（[`gather_token_id.py:114`起](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/gather_token_id.py#L114)）——一个 triton kernel，按 `b_req_idx`/`b_mtp_index` 从 `req_to_next_token_ids` 表里 gather 出本步要算的 token id 数组。
- L606-610：记原始 batch size，TPSP 模式下 `infer_batch_size` 向上取整为 `tp_world_size` 倍。
- L612-636：**CUDA Graph 分支**。`self.graph.can_run(batch_size, max_len_in_batch)` 判断能否走图（`graph` 即 decode 的 `CudaGraph`，由 `_init_cudagraph` 创建，见 [u3-l1](./u3-l1-tp-part-base-model.md)）。能跑则：
  - L615 `find_closest_graph_batch_size` 取最近档位；
  - L616-618 `_create_padded_decode_model_input` 补占位（[`basemodel.py:406-457`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L406-L457)），把 batch size 补到档位，占位用 `HOLD_REQUEST_ID`/`HOLD_TOKEN_MEMINDEX`/`b_seq_len=2`；
  - L620-621 `need_capture` 决定是"首次录制"还是"已有图直接重放"，并把 `infer_state.is_cuda_graph` 置位（`_token_forward` 末尾会据此把输出转成 no_ref tensor 以利显存复用）；
  - L622-627 `copy_kv_index_to_req` 登记每请求的 1 个新槽位（[`copy_kv_index_to_req.py:21-37`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/copy_kv_index_to_req.py#L21-L37)，kernel 把 `memindex` 写到 `req_to_token_indexs[req_idx][seq_len-1]`）；
  - L631-634：`need_capture` 时 `graph.capture_decode(self._token_forward, infer_state)` 录制，否则 `graph.replay(infer_state)` 重放；
  - L636 unpad 裁回原始 batch size。
- L637-651：**非图分支**（如 `--disable_cudagraph` 或 batch 超出图支持范围），流程几乎一致，只是直接调 `_token_forward` 而非 capture/replay。

> `_create_padded_decode_model_input`（`basemodel.py:406-457`）有个细节：占位请求的 `b_seq_len` 填 2、`total_token_num += padded_batch_size * 2`，并填一份空的 `multimodal_params`，保证 batch 维所有张量长度一致、不破坏后续 shape 敏感的算子。

#### 4.3.4 代码实践

**实践目标**：对比 `_decode` 与 `_prefill` 的"输入获取、登记、采样"差异。

**操作步骤**：

1. 在 `_decode` 的 L600 打印 `model_input.input_ids`（应为 None，gather 后才有值）；在 `_prefill` 对应位置确认 `input_ids` 一开始就有值。说明：decode 的 input 是"上一步刚采样出的 token"，必须等采样完才知道，所以延后到此处现取。
2. 打开 `copy_kv_index_to_req.py:8-17`，对照 prefill 版 `copy_kv_index_to_req_prefill`（`copy_kv_index_to_req.py:41-71`）：前者 grid 是 `(seq_len,)` 每请求一个 program、只写一个槽位；后者 grid 是 `(cdiv(max_q_seq_len,BLOCK), batch)`，每请求写一整段。这正是"decode 每请求 1 token，prefill 每请求一段 token"的体现。
3. 看后端如何调 `_decode` 与采样：`decode_normal`（[`chunked_prefill/impl.py:147-183`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py#L147-L183)）里 `self.model.forward(model_input)` 之后立刻 `_sample_and_scatter_token(... is_prefill=False ...)`，注意 decode 调用没传 `b_prefill_has_output_cpu`（那是 prefill 专有参数）。

**需要观察的现象**：decode 的 logits 形状是 `[batch_size, vocab_size]`（每请求 1 行），而 prefill 是 `[total_token_num, vocab_size]` 或 `[batch_size, vocab_size]`（取决于是否要所有位置的 logits）。

**预期结果**：能说出三处差异——① decode 现取 input_ids，prefill 直接带；② decode 按 batch size 补齐、prefill 按 token 数补齐；③ decode 登记每请求 1 槽、prefill 登记一段。

> 待本地验证：可在 `decode_normal` 的 `forward` 调用前后加日志，确认每次 decode 的 `model_input.batch_size` 等于当前在跑的请求数。

#### 4.3.5 小练习与答案

**练习 1**：`_decode` 里为什么有 `need_capture` 和 `replay` 两条路？

**参考答案**：CUDA Graph 第一次遇到某档位时需要"录制"（capture，把 `_token_forward` 整条算子序列录下来）；之后再遇到同一档位就直接"重放"（replay），免去 kernel launch 开销。`need_capture` 为真表示该档位还没录过、需要首次录制。

**练习 2**：为什么 `_decode` 用 `copy_kv_index_to_req`，而 `_prefill` 用 `init_req_to_token_indexes`（内部是 `copy_kv_index_to_req_prefill`）？

**参考答案**：decode 每个请求本轮只新增 1 个 token，只需把 `mem_index` 写到 `req_to_token_indexs[req][seq_len-1]` 一个位置，kernel 最简单；prefill 每个请求本轮新增一段 token（可能很长），需要按 `b_ready_cache_len` 偏移、用 block 并行写一整段，所以用专门的批量 kernel。

---

### 4.4 推理状态构造与层推理的分工（`_context_forward` vs `_token_forward`）

#### 4.4.1 概念说明

`_prefill` / `_decode` 本身不直接算 transformer 层，它们是"准备工作 + 收尾工作"；真正跑层的是 `_context_forward` 和 `_token_forward`。两者结构高度对称，都是：

\[ \text{embedding} \rightarrow \text{N 层 transformer} \rightarrow \text{post 层（logits）} \rightarrow \text{ModelOutput} \]

区别在于每一层调用的是 `layer.context_forward`（prefill，处理一段 token、用 prefill 注意力）还是 `layer.token_forward`（decode，处理每请求 1 个 token、用 decode 注意力 + 复用 KV Cache）。

夹在中间的还有两件事：

- **`_create_inferstate`**：把"扁平"的 `ModelInput` 翻译成层推理要用的 `InferStateInfo`，并挂上模型级常驻对象（`mem_manager`/`req_manager`）和注意力后端。
- **TPSP 通信钩子**：`_tpsp_sp_split`（进层前把 token 沿 SP 维切分）与 `_tpsp_allgather`（出层后把结果聚回），仅在 `--enable_tpsp_mix_mode` 下真正通信，否则是直通。本讲只需知道它们是"进层切、出层聚"的对称操作。

#### 4.4.2 核心流程

`_context_forward`（[`basemodel.py:655-716`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L655-L716)）：

```text
input_embs = pre_infer.context_forward(input_ids)     # embedding 层（prefill）
(可选) dp_prefill_balance 的 all_to_all 重分配
input_embs = pre_infer._tpsp_sp_split(input_embs)     # SP 切分
for i in range(layers_num):
    input_embs = layers_infer[i].context_forward(...) # 每层 prefill 推理
(post) last_input_embs = post_infer._tpsp_allgather   # SP 聚回
predict_logits = post_infer.token_forward(last_input_embs)  # norm + lm_head → logits
return ModelOutput(logits)
```

`_token_forward`（[`basemodel.py:718-745`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L718-L745)）结构几乎一样，只是 `context_forward` → `token_forward`。一个关键差异在结尾：decode 走 CUDA Graph 时（`infer_state.is_cuda_graph`）会调 `model_output.to_no_ref_tensor()`（`basemodel.py:742-743`），把 logits 转成"无引用"张量以加强显存池复用。

#### 4.4.3 源码精读

**状态构造 `_create_inferstate`**（[`basemodel.py:352-404`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L352-L404)）：把 `ModelInput` 字段逐一搬到新建的 `InferStateInfo`，并完成三件"挂接"：

- L381-386：挂上模型级常驻对象 `mem_manager`、`req_manager`，以及 `mem_index`、`microbatch_index`、`dist_group`（TPSP 通信域）。
- L369-378：按阶段分支——prefill 装 `b_ready_cache_len`，decode（diverse 模式）装 `b_shared_seq_len`/`b_mark_shared_group`。
- L391-402：按阶段创建注意力后端状态——prefill 调 `prefill_att_backend.create_att_prefill_state`，decode 调 `decode_att_backend.create_att_decode_state`。这正是 [u3-l5 注意力后端机制](./u3-l5-attention-backends.md) 要展开的内容。

**派生状态计算 `init_some_extra_state`**（[`infer_struct.py:105-127`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/infer_struct.py#L105-L127)）：prefill 走 `gen_prefill_params`，产出 `b_q_seq_len`、累积偏移 `b1_cu_q_seq_len`、KV 长度与累积、`position_ids`，并令 `b_q_start_loc = b1_cu_q_seq_len[0:-1]`；decode 走 `gen_decode_params`，产出对应的 decode 版本并令 `b_kv_start_loc = b1_cu_kv_seq_len[0:-1]`。这些 `b`/`b1` 前缀的命名约定在 `InferStateInfo` 注释里有说明（`infer_struct.py:67-77`）：`b_` 形状为 `[batch_size]`，`b1_` 形状为 `[batch_size+1]`（累积偏移多一位哨兵）。

**单层模板的对称结构**（[`transformer_layer_infer_template.py:67-99`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L67-L99)）：

```python
def context_forward(self, input_embdings, infer_state, layer_weight):
    input1 = self._att_norm(input_embdings, ...)          # attention 前的 norm
    o = self.context_attention_forward(input1, ...)       # 注意力（prefill 版）
    input_embdings.add_(o.view(-1, self.embed_dim_))      # 残差
    input1 = self._ffn_norm(input_embdings, ...)          # ffn 前的 norm
    ffn_out = self._ffn(input1, ...)                      # 前馈网络
    input_embdings.add_(ffn_out.view(-1, self.embed_dim_))# 残差
    return input_embdings
```

`token_forward`（L89-99）与之一一对应，只把 `context_attention_forward` 换成 `token_attention_forward`。再往下一层：`context_attention_forward`（L56-65）= `_get_qkv` → `_post_cache_kv`（把新 K/V 写进 KV Cache）→ `_context_attention_wrapper_run` → `_get_o`；`token_attention_forward`（L80-87）把核函数换成 `_token_attention_kernel`。也就是说**prefill 与 decode 在层的层面，唯一的差别就是"算注意力的 kernel"**：prefill 用 context attention（一段 token 互相 attend），decode 用 token attention（1 个新 token 对全历史 KV attend）。

> `_context_attention_wrapper_run`（L101-147）里有一段 `if torch.cuda.is_current_stream_capturing()` 的特殊处理，是 prefill CUDA Graph 录制时把注意力核函数也"分段录制"的机制，本讲了解其存在即可，细节留到 [u6-l1 CUDA Graph](./u6-l1-cuda-graph.md)。

#### 4.4.4 代码实践

**实践目标**：把"`_prefill` 调 `_context_forward`、`_decode` 调 `_token_forward`，二者层内只差注意力核"这条主线串起来。

**操作步骤**：

1. 在 `basemodel.py` 里分别跳到 `_context_forward`（L656）与 `_token_forward`（L719），并排对比：两者的 `for i in range(self.layers_num)` 循环体，前者调 `layer.context_forward`，后者调 `layer.token_forward`，其余（embedding、`_tpsp_sp_split`、post 层 logits）几乎逐行一致。
2. 打开 `transformer_layer_infer_template.py`，对比 `context_forward`（L67-78）与 `token_forward`（L89-99），确认结构完全对称；再对比 `context_attention_forward`（L56-65）与 `token_attention_forward`（L80-87），圈出唯一不同的核函数调用：`_context_attention_kernel`（经 `_context_attention_wrapper_run`）vs `_token_attention_kernel`。
3. 追一个"采样落点"：`_context_forward`/`_token_forward` 返回的 `ModelOutput.logits`，在后端 `prefill_normal`（[`chunked_prefill/impl.py:103-145`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/chunked_prefill/impl.py#L103-L145)）/`decode_normal`（L147-183）里被传给 `_sample_and_scatter_token` 做 top-k/top-p 采样，采样出的新 token id 写回 `req_to_next_token_ids`，下一轮 decode 再由 `gather_token` 取出——闭环形成。

**需要观察的现象**：除了注意力核，prefill 与 decode 的层推理代码路径高度同构；这说明 LightLLM 用"同一套模板 + 可替换的注意力核函数"统一了两阶段。

**预期结果**：能用一句话总结——"prefill 走 `_context_forward`+context attention 核，decode 走 `_token_forward`+token attention 核，层模板其余部分完全一致"。

> 待本地验证：若想看派生状态长什么样，可在 `init_some_extra_state`（`infer_struct.py:106`）的 prefill/decode 分支处分别打印 `b_q_seq_len`、`b1_cu_q_seq_len`，对比其形状与含义。

#### 4.4.5 小练习与答案

**练习 1**：`InferStateInfo` 里 `b_` 前缀和 `b1_` 前缀的张量分别是什么形状？为什么累积偏移要多一位？

**参考答案**：`b_` 前缀形状为 `[batch_size]`（每请求一个值），`b1_` 前缀形状为 `[batch_size+1]`（累积偏移 cu_seqlens）。累积偏移多一位"0 哨兵"是为了方便用 `b1_cu_q_seq_len[0:-1]` 直接得到每段的起始位置（`b_q_start_loc`/`b_kv_start_loc`），省去额外位移运算——这是 flash attention 类 kernel 常用的 varlen 表示法。

**练习 2**：为什么 `_token_forward` 结尾要判断 `infer_state.is_cuda_graph` 并调 `to_no_ref_tensor`，而 `_context_forward`（非 prefill cuda graph 路径）没有这步？

**参考答案**：decode 高频运行且几乎总走 CUDA Graph，把输出 logits 转成"无引用"张量能让显存池更快复用、降低 decode 期的显存占用；非 cuda graph 的 prefill 路径不在此高频复用场景内，故不做该转换。`to_no_ref_tensor` 会切断张量对底层显存块的引用计数，使其可被池化回收。

---

## 5. 综合实践

把本讲三块内容串成一个完整的"请求一生"追踪任务。

**任务**：以一条 prompt 从进入后端到产出第一个新 token 为线索，画出一张包含以下要点的流程图，并为每个箭头标注源码位置：

1. 后端 `prepare_prefill_inputs` 把请求打包成 `ModelInput(is_prefill=True)`（`generic_pre_process.py:75`）。
2. `prefill_normal` 调 `self.model.forward(model_input)`（`chunked_prefill/impl.py:111`）。
3. `forward` 经 `is_prefill` 分发到 `_prefill`（`basemodel.py:347-348`）。
4. `_prefill` 完成 padding → `_create_inferstate` → `init_req_to_token_indexes` → `init_some_extra_state` → `_context_forward`（`basemodel.py:565-584`）。
5. `_context_forward` 跑 embedding → N 层 `context_forward` → post 层产出 logits（`basemodel.py:658-704`）。
6. logits 回到 `prefill_normal`，被 `_sample_and_scatter_token` 采样出第 1 个新 token（`chunked_prefill/impl.py:112-120`）。
7. 该 token 写入 `req_to_next_token_ids`；下一拍 `decode_normal` → `prepare_decode_inputs` 打包 `ModelInput(is_prefill=False, input_ids=None)`（`generic_pre_process.py:146`）。
8. `forward` 分发到 `_decode`，`gather_token` 现取该 token（`basemodel.py:599-604`），走 CUDA Graph 录制/重放（`basemodel.py:631-634`），产出下一个 token。

**进阶**：在图上用两种颜色标出"只在 prefill 出现"的步骤（如 `init_req_to_token_indexes`、`b_ready_cache_len`）和"只在 decode 出现"的步骤（如 `gather_token`、`copy_kv_index_to_req`、`graph.replay`），体会两阶段的对称与不对称。

> 待本地验证：若条件允许，可对比同一请求 prefill 首步与后续 decode 步的 `forward` 耗时（prefill 通常远大于单步 decode），印证"prefill 计算密集、decode 延迟敏感"的直觉。

---

## 6. 本讲小结

- 模型对外只有一个 `forward(ModelInput) → ModelOutput` 入口，内部靠 `ModelInput.is_prefill` 这一个布尔字段分发到 `_prefill` 或 `_decode`（`basemodel.py:342-350`）。
- `ModelInput` 是一次推理的完整输入包，prefill 带 `input_ids` + `b_ready_cache_len`，decode 带 `b_position_delta` 且 `input_ids=None`（`batch_objs.py:9-98`）；`ModelOutput` 核心是 `logits`。
- `_prefill` 做的是"按 token 数 padding → 建状态 → 批量登记 KV 索引 → `_context_forward` → unpad"，补齐对象是 token 数、靠多挂一个占位请求实现（`basemodel.py:538-592`）。
- `_decode` 做的是"现场 gather input_ids → 按 batch size padding → 每请求登记 1 个 KV 槽 → 走 CUDA Graph 录制/重放 `_token_forward` → unpad"，对延迟最敏感（`basemodel.py:594-653`）。
- `_context_forward` 与 `_token_forward` 结构对称，都走 embedding → N 层 transformer → post 层 logits；层模板 `context_forward`/`token_forward` 唯一区别是注意力核函数（`transformer_layer_infer_template.py:56-99`）。
- `_create_inferstate` 把扁平输入翻译成层推理用的 `InferStateInfo`，并按阶段挂上 prefill/decode 注意力后端状态（`basemodel.py:352-404`）。

---

## 7. 下一步学习建议

- 想深入"层内部到底算了什么"——注意力核、FFN、norm 的具体实现，请读 [u3-l3 推理层模板与层推理](./u3-l3-layer-infer-template.md)。
- 想搞懂 prefill/decode 末尾 logits 如何变成最终 token——top-k/top-p 与惩罚项，请读 [u3-l6 采样与后处理](./u3-l6-sampling-postprocess.md)。
- 想理解本讲反复出现的 CUDA Graph 录制/重放细节（`can_run`/`find_closest_graph_batch_size`/`capture`/`replay`），请读 [u6-l1 CUDA Graph 捕获与重放](./u6-l1-cuda-graph.md)。
- 想看 prefill/decode 之外的两条"重叠执行"路径 `microbatch_overlap_prefill`/`microbatch_overlap_decode`（用两个 infer_state 交错隐藏延迟），请读 [u6-l2 microbatch overlap 与 TPSP 混合并行](./u6-l2-microbatch-overlap-tpsp.md)。
