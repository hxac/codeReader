# CUDA Graph 与 torch.compile / piecewise

> 本讲对应大纲 `u10-l4`，依赖 `u3-l3`（ModelEngine 与模型前向）。在 `u3-l3` 里我们把 `PyTorchModelEngine.forward` 内部「CUDA Graph 回放」与「eager `_forward_step`」当作两条并列的执行路径，本讲就把这个「CUDA Graph 回放」彻底打开：它其实是**两套相互配合又各自独立**的机制——为纯解码迭代服务的**生成期 CUDA Graph（`cuda_graph_runner`）**，以及建立在 `torch.compile` 之上的**分片 CUDA Graph（piecewise）**。同时本讲也兑现 `u10-l1`/`u10-l2` 埋下的伏笔：为什么自定义算子必须用 `torch.library.custom_op(..., mutates_args=())` 写成纯函数——正是因为 piecewise 路径需要 `torch.compile` 把整张图追踪（trace）下来。

---

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 **CUDA Graph** 为什么能省 host 开销，以及 TensorRT-LLM 里「生成期 CUDA Graph」的**捕获（capture）/ 回放（replay）/ 补齐**三段式是如何用 `KeyType` 组织的。
- 理解 **piecewise CUDA Graph** 为什么必须建在 `torch.compile` 之上：它如何在一张 `fullgraph` 追踪出来的图里，把**不可捕获的 attention 段**切出来走 eager，其余段落各自捕获成图。
- 掌握 **`torch.compile` 自定义后端（`Backend`）** 的 IR 流水线：Torch IR → ATen IR → 算子融合 / re-inplace / 多流 / piecewise 切分。
- 解释 **auto multi-stream** 调度器如何用粗糙代价模型把算子排到多条 CUDA stream 上，并理解它和 piecewise 的协作关系。
- 读懂 `CudaGraphConfig` 与 `TorchCompileConfig` 里可调的捕获参数，知道哪些旋钮影响显存、并发与延迟。

---

## 2. 前置知识

### 2.1 什么是 CUDA Graph，为什么要它

一次模型前向会发射（launch）成百上千个 kernel。每个 kernel launch 都伴随一次 **CPU 侧的驱动调用开销**（几十微秒级）。当单步算力很小（典型场景：**decode 阶段每个请求只生成 1 个 token**）时，GPU 算得很快、CPU 却来不及喂 kernel，于是性能被 **host-bound（CPU 瓶颈）** 卡住。

**CUDA Graph** 把「一串 kernel launch」录制成一个可整体重放（replay）的对象，把上千次驱动调用压成一次。代价是：录制时用到的输入张量地址会被「烤」进图里，回放时输入必须落在**同一块显存**上。这就要求**静态输入缓冲**与**显存池共享**。

### 2.2 Prefill vs Decode：为什么有两套图

- **Decode（生成）阶段**：每个请求 1 个 token，batch 内请求数变化但单步 token 形状简单 → 适合按 **batch size** 分桶捕获，这是 `cuda_graph_runner` 干的事。
- **Prefill（上下文）阶段**：每个请求 prompt 长度千变万化，token 总数不可枚举 → 单独按 batch 捕获不现实，于是有了 **piecewise**：只把「除 attention 外的稳定段落」捕获成图，attention 段走 eager，并按 **token 总数**分桶。

### 2.3 torch.compile 的最小心智模型

`torch.compile(model, backend=Backend(), fullgraph=True)` 做两件事：

1. **Dynamo** 把 `model.forward` 的 Python 执行**追踪**成一张 FX 图（Torch IR），`fullgraph=True` 要求整段无图断裂（graph break）。
2. 把这张 FX 图交给自定义 `backend(gm, example_inputs)`，由你决定怎么优化、编译、捕获。

TensorRT-LLM 没有用 Inductor 做主力，而是写了自己的 `Backend`，在 ATen IR 上做融合与 piecewise。本讲的核心就是这条自定义后端流水线。

> 如果你对「自定义算子为何要写成纯函数」「fake kernel 负责形状推断」还不熟，建议先回看 `u10-l1`（MoE）/`u10-l2`（量化）里对 `torch.library.custom_op` 与 `mutates_args=()` 的说明——那是 `torch.compile` 能追踪下去的前提。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `tensorrt_llm/_torch/pyexecutor/cuda_graph_runner.py` | **生成期 CUDA Graph** 的运行器：判定批次是否可入图、补齐（padding）、捕获、回放，按 `(batch_size, draft_len, ...)` 组织多张图 |
| `tensorrt_llm/_torch/compilation/backend.py` | **`torch.compile` 自定义后端 `Backend`**：IR 流水线入口，融合 pass、re-inplace、多流、piecewise 的总调度 |
| `tensorrt_llm/_torch/compilation/piecewise_optimizer.py` | **piecewise 切分**：在 ATen IR 图里按 attention 边界算子切段落，为每个可捕获段造一个 `PiecewiseRunner` |
| `tensorrt_llm/_torch/compilation/multi_stream/auto_multi_stream.py` | **auto multi-stream**：粗糙代价模型 + 关键路径 + 把算子排到多条 stream，插入流控制算子 |
| `tensorrt_llm/llmapi/llm_args.py` | `CudaGraphConfig`（生成期图配置）与 `TorchCompileConfig`（piecewise / 多流 / 融合配置）的字段定义 |
| `tensorrt_llm/_torch/pyexecutor/model_engine.py` | 装配点：构造 `Backend`、调用 `torch.compile`、跑 warmup 捕获生成期图与 piecewise 图 |
| `docs/source/features/torch_compile_and_piecewise_cuda_graph.md` | 官方特性文档，含使用方法、调参建议、开发约束 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 cuda_graph_runner**（生成期 CUDA Graph）、**4.2 piecewise 优化**（建在 `torch.compile` 上的分片图）、**4.3 多流**（auto multi-stream）。三者关系如下：

```
                     PyTorchModelEngine.forward (u3-l3)
                              │
        ┌─────────────────────┼──────────────────────────┐
        ▼                     ▼                          ▼
  纯解码迭代            混合 / prefill 迭代          （不可入图时）
        │                     │                          │
  cuda_graph_runner     torch.compile(Backend)        eager _forward_step
  (生成期 CUDA Graph)         │                     (u3-l3)
        │              piecewise_optimizer
        │              把 attention 段切出走 eager
        │              其余段各自捕获成图
        │                     │
        │              (段内) auto multi-stream
        └────────── 共享 graph_pool_handle / 静态缓冲 ──┘
```

---

### 4.1 cuda_graph_runner：生成期 CUDA Graph

#### 4.1.1 概念说明

`CUDAGraphRunner` 是「纯解码迭代」的加速器。它的工作可以用一句话概括：**对每一个「形状签名」录一张图，回放时把新输入拷进静态缓冲再整体重放**。

这里的「形状签名」不是简单的 batch size。在启用投机解码（见 `u10-l3`）、sparse attention、attention 数据并行等特性时，同一 batch size 下还可能有多条不同的前向路径，必须分别录图。TensorRT-LLM 用一个元组 `KeyType` 当作图的键。

它解决三个问题：

1. **什么时候能入图？** —— 资格判定（`maybe_get_cuda_graph`）：必须全是生成请求、不在统计模式、batch size 在支持列表内。
2. **形状对不上怎么办？** —— 补齐（`pad_batch`）：把 batch size 向上凑到最近的已捕获档位。
3. **输入地址变了怎么办？** —— 静态缓冲（`shared_static_tensors`）+ 共享显存池（`memory_pool`）：回放前把真实输入 `copy_` 进固定地址的缓冲。

#### 4.1.2 核心流程

生成期 CUDA Graph 的生命周期分三段：

```text
[Warmup/捕获期]  allow_capture() 上下文内
   for 每个 (batch_size, draft_len, ...) 组合:
       1. warmup 若干步（让 PyTorch/attention 内部状态稳定、workspace 定型）
       2. torch.cuda.CUDAGraph() + torch.cuda.graph(graph, pool=memory_pool) 录制一次前向
       3. 把输出做 weak_ref，存进 graph_outputs[key]
       4. memory_pool = graph.pool()  ← 多张图共享同一个池

[运行期：每一步前向]
   maybe_get_cuda_graph(batch) → 返回 (graph_attn_metadata, key) 或 None
   │
   ├─ None：本批次不可入图 → 回退 eager
   └─ 命中 key：
        needs_capture(key)?  → 首次见到，走 capture()
        否则                  → replay(key, inputs)
            · 把 input_ids/position_ids copy_ 进 shared_static_tensors
            · graphs[key].replay()
            · 返回 graph_outputs[key]（weak_ref）
```

关键设计：**捕获默认关闭**（`_capture_allowed = False`），只在 warmup 的 `allow_capture()` 上下文里打开。运行期遇到没见过的 key 直接回退 eager，而不是当场捕获——因为运行期捕获会带来数毫秒的毛刺，还会撑大 workspace、破坏已有图的地址稳定性。

#### 4.1.3 源码精读

**(a) 图的键 `KeyType`**

图键是一个五元组，见 `get_graph_key`：

[cuda_graph_runner.py:229-266](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/cuda_graph_runner.py#L229-L266) — 把 `(batch_size, draft_len, is_first_draft, short_seq_len_mode, is_all_greedy_sample)` 组合成键。其中：

- `draft_len` 来自投机解码的草稿长度（无投机时为 0）；EAGLE3 draft 模型还要区分 `is_first_draft`，因为第一层草稿长度与其他层不同。
- `short_seq_len_mode` 来自 sparse attention（见 `u6-l2`）：DSA 等算法对短序列走不同前向路径，需要分别录图。
- `is_all_greedy_sample` 区分「贪心 argmax 快路径」与「高级采样核路径」，两套采样都要各录一图。

> 这正是 `u10-l3` 里强调的「单步批次的 `SpecMetadata` 决定走哪条路」在 CUDA Graph 层面的体现：不同路径必须分别捕获。

**(b) 资格判定**

[cuda_graph_runner.py:297-369](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/cuda_graph_runner.py#L297-L369) — `maybe_get_cuda_graph` 的判定顺序：统计模式关闭 → attention-DP 时跨 rank 共识 → `can_run_cuda_graph` → MRoPE delta 缓存是否就绪 → 计算 key → 命中已存元数据则返回，否则要求 `_capture_allowed` 且 batch size 在支持列表内才创建新图元数据。

注意这一段——**统计模式（`ExpertStatistic.should_record()`）下直接返回 `None`**，这与 `u3-l2` 提到的「dummy/统计请求会被排除」一脉相承：凡是要做特殊统计或 padding 的批次都不能入图。

**(c) 捕获：warmup + 录制**

[cuda_graph_runner.py:398-481](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/cuda_graph_runner.py#L398-L481) — `capture` 方法的核心：

```python
with with_multi_stream(True), piecewise_cuda_graph(False):
    # 1) warmup：让 PyTorch 内部状态与 attention workspace 定型
    for _ in range(self.WARMUP_STEPS):
        output = _setup_spec_decoding_and_forward(key, forward_fn, capture_inputs)
        ...
    if self.is_warmup_only:
        return output
    # 2) 真正录制
    graph = torch.cuda.CUDAGraph()
    with torch.cuda.graph(graph, pool=self.memory_pool):
        output = _setup_spec_decoding_and_forward(key, forward_fn, capture_inputs)
    ...
# 3) 记图、共享池
self.graphs[key] = graph
self.graph_outputs[key] = make_weak_ref(output)
self.memory_pool = graph.pool()   # ← 后续图接着用同一个池
```

两个细节值得记：

- `with piecewise_cuda_graph(False)`：**生成期图捕获时显式关掉 piecewise 标志**，避免两条图机制互相干扰（`piecewise_cuda_graph` 是 `tensorrt_llm/_torch/utils.py` 里的线程局部开关）。
- `make_weak_ref(output)`：输出用弱引用持有，让中间激活在适当时机可被释放，但又保证回放时对象仍存活。
- 投机解码的 `kv_lens_cuda` 会在每次前向被原地改写，warmup 复用同一个 dummy 请求会让累积值失真，于是用 `_save_spec_decode_capture_state` / `_restore_spec_decode_capture_state` 在图外恢复单步输入态（[cuda_graph_runner.py:42-60](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/cuda_graph_runner.py#L42-L60)）。

**(d) 回放：拷进静态缓冲再重放**

[cuda_graph_runner.py:483-515](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/cuda_graph_runner.py#L483-L515) — `replay` 把当前 `input_ids` / `position_ids` 拷进预先分配、地址固定的 `shared_static_tensors`，再 `graphs[key].replay()`。静态缓冲在 `_create_shared_static_tensors` 里按「最大可能 batch」一次性分配（[cuda_graph_runner.py:158-180](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/cuda_graph_runner.py#L158-L180)），回放时只取相应长度的切片。

**(e) 补齐（padding）**

[cuda_graph_runner.py:657-700](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/cuda_graph_runner.py#L657-L700) — `_round_up_batch_size` 用 `bisect` 把 batch size 向上取到最近的已捕获档位；`pad_batch` 是个上下文管理器，用 **dummy 请求**（`is_cuda_graph_dummy=True`，见 `u3-l2`/`u8-l2`）把 batch 撑到目标 size，离开上下文时再把 dummy 砍掉。补齐是否启用由 `CudaGraphConfig.enable_padding` 控制。

#### 4.1.4 代码实践

**实践目标**：理解 batch size 是如何被映射到已捕获档位的。

**操作步骤**（源码阅读型，无需 GPU）：

1. 打开 `tensorrt_llm/_torch/pyexecutor/cuda_graph_runner.py`，定位 `_round_up_batch_size`（L657）与 `_create_shared_static_tensors`（L158）。
2. 假设 `supported_batch_sizes = [1, 2, 4, 8, 16, 24, 32, 64, 128]`（即 `enable_padding=True` 时 `BaseCudaGraphConfig._generate_cuda_graph_batch_sizes` 的前几档），手算 `_round_up_batch_size` 对输入 `5`、`24`、`200` 的返回值。
3. 阅读 `pad_batch`（L686），确认它在离开上下文时如何用切片 `[:-padding_size]` 移除 dummy 请求。

**需要观察的现象 / 预期结果**：

- `_round_up_batch_size(5)` → `8`；`_round_up_batch_size(24)` → `24`（精确命中）；`_round_up_batch_size(200)` → `0`（超出最大档位，返回 0 表示不补齐、回退 eager）。
- `enable_padding=True` 时档位较稀（`1,2,4` 后按 8 递增再按 64 递增），目的是减少要捕获的图数量、省显存；`enable_padding=False` 时档位更密（`1..31` 连续），减少补齐带来的多余算力，但显存占用更高。

> 待本地验证：在带 GPU 的环境用 `trtllm-bench` 跑 decode-only，对比 `cuda_graph_config.enable_padding: true/false` 下的显存峰值与吞吐。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_capture_allowed` 默认是 `False`，运行期见到新 key 时宁可回退 eager 也不当场捕获？

**参考答案**：运行期捕获会带来数毫秒的捕获毛刺；更严重的是，捕获过程可能 resize 共享的 `cuda_graph_workspace` 张量，从而改变地址，使**此前已捕获的图里烤死的地址失效**（见 `allow_capture` 的文档注释 L376-387）。因此捕获被限制在 warmup 的 `allow_capture()` 上下文里集中完成。

**练习 2**：`get_graph_key` 里为什么要带上 `is_all_greedy_sample`？

**参考答案**：投机解码的单引擎采样器有「贪心 argmax 快路径」和「高级采样核慢路径」两条代码路径，二者 kernel 序列不同。若共用一张图，回放时会按录制时的路径走，与运行期真实需要的路径不符。把它纳入 key，就能为两种情况各录一图，回放时按真实 batch 状态正确分派。

---

### 4.2 piecewise 优化：建在 torch.compile 上的分片 CUDA Graph

#### 4.2.1 概念说明

生成期 CUDA Graph 解决了 decode，但 prefill 阶段 token 总数千变万化，无法枚举捕获。**piecewise（分片）CUDA Graph** 的思路是：**把模型前向切成若干段，attention 这种不可捕获的段走 eager，其余段各自录成图**。

为什么不直接在 eager 里手工切？官方文档说得很直白：在纯 eager 模式下「在 CUDA Graph 与 eager 之间切分模型、并管理这些图」非常繁琐。于是 TensorRT-LLM **把这件事建在 `torch.compile` 之上**——先用 `fullgraph` 把整张模型追踪成一张 FX 图，再在图上做切分，就能用统一的图变换框架处理。

它依赖一个关键前提（呼应 `u10-l1`/`u10-l2`）：**模型里所有算子都必须能被 `torch.compile` 识别**。对原生无法表达的算子，必须包成 `torch.library.custom_op` 并提供 fake kernel（负责形状/dtype 推断）。仓库已经把 attention、MoE routed expert 等大模块包成了「黑盒」自定义算子，这样普通开发者改这些模块时不必担心 trace 失败。

#### 4.2.2 核心流程

piecewise 的总入口是自定义后端 `Backend`，流水线如下：

```text
torch.compile(model, backend=Backend(...), fullgraph=True)
        │  Dynamo 追踪
        ▼
   Torch IR (FX GraphModule)            ← recover_pass
        │  aot_module_simplified (AOT autograd)
        ▼
   ATen IR (SSA, <250 个 aten 算子)      ← fw_compiler = Backend.optimize
        │
        ├── 1) 融合 pass（add_norm / ar_residual_norm / userbuffers）
        ├── 2) eliminate_dead_code + remove_copy_for_mutates_args (re-inplace)
        ├── 3) (可选) auto multi-stream   ← 见 4.3
        │
        ▼  enable_piecewise_cuda_graph=True 时
   piecewise_optimizer(gm, ...)
        │
        │  按 attention 边界算子切图 → split_module → submod_0..n
        │  PiecewiseInterpreter 遍历，给每个「可捕获段」造一个 PiecewiseRunner
        │  每个 PiecewiseRunner 按 capture_num_tokens 分桶录图
        ▼
   运行期：PiecewiseRunner.__call__ 按「运行期 token 数」选桶回放
```

为什么需要 ATen IR 而不直接在 Torch IR 上优化？文档给出三条理由：ATen IR 是 **SSA 形式（无输入 mutation）**、是**严格子集（<250 个算子，等价算子归一）**、**保证 dtype/shape 元信息**。这让算子融合（pattern matching）和 re-inplace 优化变得可行。

piecewise 切分的「边界」由一组**原地（in-place）attention 算子**标记：

```python
op_names = [
    "attn_custom_op_inplace",
    "mla_custom_op_inplace",
    "mla_dsa_attn_inplace",
    "gdn_custom_op_inplace",
    "minimax_m3_attn_custom_op_inplace",
]
```

设计约定（来自文档）：

1. attention **不得有自己的输出**——它的输出张量由前一图段预分配，attention 以 in-place 写入，从而保证每个图段的输入都由 CUDA Graph 分配、地址稳定。
2. 每个子图**至少有一个含 token 数的输入维**，用于运行期选桶。
3. 只允许 `num_of_tokens` 这一维动态。

#### 4.2.3 源码精读

**(a) 后端入口 `Backend.__call__`**

[backend.py:193-233](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/compilation/backend.py#L193-L233) — 这是 `torch.compile` 实际调用的后端可调用对象。它先扫描 placeholder 节点，从 `l_input_ids_` / `l_inputs_embeds_` 等名字的 `example_value.shape[0]` 提取 `input_num_tokens`（piecewise 必须能找到它，否则报错），然后：

```python
gm = recover_pass(gm)
return aot_module_simplified(
    gm, example_inputs,
    fw_compiler=self.optimize,           # ← ATen IR 上跑优化
    decompositions=select_decomp_table(),
)
```

即 `optimize` 是作用在 **ATen IR 前向图**上的编译器。

**(b) 优化总调度 `Backend.optimize`**

[backend.py:142-191](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/compilation/backend.py#L142-L191) — 依次：反复应用融合 pass 直到收敛 → `eliminate_dead_code` → `remove_copy_for_mutates_args`（re-inplace）→ 根据开关分派：

```python
# 多流：仅当 非-piecewise 且 非-inductor 时在这里做
if self.num_streams > 1 and not self.piecewise_cuda_graph and not self.enable_inductor:
    num_events = multi_stream_schedule(gm, self.num_streams)
    self.generate_events(num_events)

gm.recompile()

if self.piecewise_cuda_graph:
    gm, num_events, runners = piecewise_optimizer(
        gm, example_inputs, self.enable_inductor, self.input_num_tokens,
        self.capture_num_tokens, self._graph_pool_handle, self.num_streams)
    ...
    return gm
elif self.enable_inductor:
    return compile_fx_inner(gm, example_inputs)
else:
    return gm
```

> 注意这段代码揭示了 **piecewise 与多流的关系**（实践任务第 3 问）：开启 piecewise 时，**顶层的 `multi_stream_schedule` 被跳过**，因为多流优化被推迟到 `piecewise_optimizer` 内部、**对每个可捕获子图分别做**（见 4.3.3）。原因有二：piecewise 自身会把图拆开，顶层整图做多流没意义；piecewise 需要控制事件数与图段边界对齐。

**(c) 边界算子与切图 `piecewise_optimizer`**

[piecewise_optimizer.py:272-328](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/compilation/piecewise_optimizer.py#L272-L328) — 遍历节点赋 `graph_id`：遇到边界算子（in-place attention）就开新段；遇到 `aten.index.Tensor` / `aten.cumsum.default` 这类会**破坏后续可切分性**的算子，也停止继续切分（`stop_partition=True`），之后的节点全部并入 attention 所在的 eager 段。然后用 `split_module` 物理切成 `submod_0..n`，交给 `PiecewiseInterpreter` 遍历。

[piecewise_optimizer.py:23-34](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/compilation/piecewise_optimizer.py#L23-L34) — `_piecewise_boundary_ops` 用 `get_optional_trtllm_op` 容错地解析这组算子（不存在则跳过），保证不同构建配置下都能运行。

**(d) 给每个可捕获段造 Runner**

[piecewise_optimizer.py:75-124](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/compilation/piecewise_optimizer.py#L75-L124) — `PiecewiseInterpreter.call_module` 对每个非排除子模块：先在子图里定位「哪个输入的哪一维等于动态 token 数 `compile_time_num_tokens`」，得到 `runtime_num_tokens_idx`；若开启多流则对该子图做 `multi_stream_schedule`；最后 new 一个 `PiecewiseRunner`，并直接 `self.module.__dict__[target] = runner` 把原子模块替换掉。

**(e) 运行期按 token 数选桶**

[piecewise_optimizer.py:193-269](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/compilation/piecewise_optimizer.py#L193-L269) — `PiecewiseRunner.__call__` 是运行期入口：

```python
# 1) 取运行期 token 数
runtime_num_of_token = int(args[self.runtime_num_tokens_idx[0]]
                           .shape[self.runtime_num_tokens_idx[1]])
# 2) 不在已捕获桶 / 标志关闭 → 走 default_callable（eager 或 inductor）
if (runtime_num_of_token not in self.entries
        or not get_piecewise_cuda_graph_flag()
        or not get_per_request_piecewise_cuda_graph_flag()):
    return self.default_callable(*args)

entry = self.entries[runtime_num_of_token]   # ← 按 token 数选桶
# 3) 首次：3 次 warmup → torch.cuda.graph(pool=graph_pool_handle) 录制 → 立即 replay 一次
#    之后：直接 entry.cuda_graph.replay()
```

三个要点：

- **桶键是 token 数**：动态形状靠「分桶 + 强制 padding」驯服——运行期 token 数必须精确命中某个 `capture_num_tokens`，否则回退 eager。这就是文档说的「piecewise 的 padding 是强制的」，由 `model_engine` 在准备输入时把 token 数向上补到下一个捕获档。
- **`graph_pool_handle` 共享**：所有 piecewise Runner 共用同一个 CUDA 显存池（`Backend._graph_pool_handle`，[backend.py:45-82](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/compilation/backend.py#L45-L82)），跨段、跨桶复用显存。
- **per-request 开关**：`get_per_request_piecewise_cuda_graph_flag` / `set_piecewise_running`（[utils.py:89-96](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/utils.py#L89-L96), [piecewise_optimizer.py:208-212](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/compilation/piecewise_optimizer.py#L208-L212)）让首/尾段在 multi-stream 下正确设置运行态。

#### 4.2.4 代码实践

**实践目标**：说清 piecewise 如何在动态形状下保持高效，并列出可调捕获参数。

**操作步骤**：

1. 阅读 [docs/source/features/torch_compile_and_piecewise_cuda_graph.md:67-85](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/features/torch_compile_and_piecewise_cuda_graph.md#L67-L85)（Padding 与 Performance Tuning 两节）。
2. 在 `llm_args.py` 找 `TorchCompileConfig`（[L4765-4806](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L4765-L4806)），列出它的字段及默认值。
3. 解释 `capture_num_tokens` 的默认值是如何生成的（`set_default_capture_num_tokens` 这个 `model_validator`）。

**预期结果（动态形状为何高效）**：

- piecewise 不试图为每个可能的 token 数录图（无穷无尽），而是**只录用户指定的离散档位** `capture_num_tokens`，运行期把真实 token 数 **padding 到下一个档位**，再回放该档的图。
- 这把「连续动态形状」降维成「有限桶 + 一次性 padding 拷贝」，用少量额外算力换掉成百上千次 kernel launch 的 host 开销。
- 调参折中（来自文档）：档位越密 → padding 浪费越少但显存占用越高、最大并发越低；档位越稀 → 省显存但 padding 与多余算力越多。建议小 token 区间用密步、大 token 区间用固定步（如 256）。

**`CudaGraphConfig` / `TorchCompileConfig` 可调参数清单**：

| 配置项 | 位置 | 默认 | 含义 |
|--------|------|------|------|
| `cuda_graph_config.enable_padding` | [llm_args.py:182-186](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L182-L186) | `False` | 是否把 batch 向上取整到已捕获档（生成期图） |
| `cuda_graph_config.batch_sizes` | [llm_args.py:175-177](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L175-L177) | 自动生成 | 生成期图要捕获的 batch size 列表 |
| `cuda_graph_config.max_batch_size` | [llm_args.py:179-180](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L179-L180) | `0`→派生 | 上限；不填则由 `batch_sizes` 派生或默认 128 |
| `torch_compile_config.enable_fullgraph` | [llm_args.py:4767-4769](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L4767-L4769) | `True` | 强制整图无断裂 |
| `torch_compile_config.enable_inductor` | [llm_args.py:4771-4772](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L4771-L4772) | `False` | 段内是否交给 Inductor |
| `torch_compile_config.enable_piecewise_cuda_graph` | [llm_args.py:4774-4776](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L4774-L4776) | `False` | piecewise 总开关 |
| `torch_compile_config.capture_num_tokens` | [llm_args.py:4778-4789](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L4778-L4789) | `None`→自动 | piecewise 要捕获的 token 数档位 |
| `torch_compile_config.enable_userbuffers` | [llm_args.py:4791-4794](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L4791-L4794) | `True` | AllReduce 走 userbuffers |
| `torch_compile_config.max_num_streams` | [llm_args.py:4796-4799](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L4796-L4799) | `1` | auto multi-stream 的流数上限 |

#### 4.2.5 小练习与答案

**练习 1**：为什么 piecewise 要求 attention 算子「没有输出、以 in-place 写入前一图段预分配的张量」？

**参考答案**：CUDA Graph 回放要求每个图段的输入地址在捕获后保持稳定。如果 attention 自己 `torch.empty` 分配输出，那块显存属于 attention 段（走 eager），无法被相邻的 CUDA Graph 段「看见」为稳定输入。改为由前一图段预分配输出缓冲、attention in-place 写入，就保证了段间数据交接点的地址由 CUDA Graph 管理、跨回放不变。

**练习 2**：`piecewise_optimizer` 为什么在遇到 `aten.index.Tensor` / `aten.cumsum.default` 后就 `stop_partition=True` 不再切新段？

**参考答案**：这些算子之后，张量形状/语义变得难以再安全地切分出可独立捕获的 CUDA Graph 段（它们常出现在 attention 之后的数据依赖路径里）。为避免录出语义错误的图，策略是：这些算子之后的节点全部并入 attention 所在的 eager 段，不再产生新的可捕获段。

---

### 4.3 多流：auto multi-stream 调度器

#### 4.3.1 概念说明

单条 CUDA stream 上的算子只能串行执行。但 MoE / 投机解码等场景里，**router GEMM 的输出与某些独立算子（如 `silu_and_mul + fp4_quantize`）之间没有数据依赖**，理论上可以并行——只要把它们派到不同 stream 上。

麻烦在于：`torch.compile` **不会为用户自定义的 CUDA stream 创建子图**，而是把它降级成一个没有消费者的 `set_stream` 节点，随后在 Torch IR → ATen IR 的转换中被死代码消除（DCE）掉，于是多流调度信息全丢。

TensorRT-LLM 的解法是 **auto multi-stream 调度器**：在 ATen IR 图上自己建依赖 DAG、算关键路径、把节点排到最多 `max_num_streams` 条 stream 上，并**直接往图里插入流控制算子**（`record_event` / `wait_event` / `set_stream` / `record_stream`）。因为 FX 图按节点列表顺序执行，把这些控制算子作为节点插进去，就能在运行期真正切换/同步 stream；又因为它们没有消费者，调度完成后不能再做 DCE。

#### 4.3.2 核心流程

```text
multi_stream_schedule(gm, max_num_streams):
   1) estimate_time(node)：用粗糙代价模型给每个节点打分
        - SymInt 节点（host 上算）：0
        - view/permute/alias 等：0
        - MoE 算子：20（高，提权重让 router→MOE 关键路径被识别）
        - GEMM 类算子：10
        - 其他：1
   2) 建 DAG：节点间显式依赖 + 对 in-place 算子的特殊处理
   3) 算关键路径（rough cost model）
   4) 把节点排到 ≤ max_num_streams 条 stream（调度器未必用满）
   5) 插入 record_event/wait_event/set_stream/record_stream 控制算子
   返回：创建的 event 数量
```

代价模型是「粗糙但够用」的：它不需要精确耗时，只要保证**关键路径**（如 `router_gemm → moe`）被正确识别并串在同一条关键流上，其余无依赖算子塞进并行流。文档给出的例子是 `trtllm.dsv3_router_gemm_op` 与 `trtllm.silu_and_mul + trtllm.fp4_quantize` 在两条流上并行执行，中间靠 `record_event` / `wait_event` 同步。

#### 4.3.3 源码精读：piecewise 与多流的关系

**(a) 代价模型**

[auto_multi_stream.py:23-86](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/compilation/multi_stream/auto_multi_stream.py#L23-L86) — `estimate_time`：SymInt 节点返回 0（host 计算、不占 stream 时间）；`no_cost_ops`（view/permute/alias/empty）返回 0；MoE 算子 20、GEMM 算子 10、其余 1。注释明确说这些估计「不精确，但足以让关键路径被正确调度」。

**(b) 调度函数签名与契约**

[auto_multi_stream.py:419-425](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/compilation/multi_stream/auto_multi_stream.py#L419-L425) — `multi_stream_schedule(gm, max_num_streams) -> int`：原地修改 `gm`，返回创建的 event 数（供 `Backend.generate_events` 预分配事件对象）。注意「调度器未必用满所有流」。

**(c) 两处调用点的分工（回答实践任务第 3 问）**

piecewise 关闭时，多流在顶层 `optimize` 里对整张 ATen IR 图做：

[backend.py:166-171](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/compilation/backend.py#L166-L171) —
```python
if self.num_streams > 1 and not self.piecewise_cuda_graph and not self.enable_inductor:
    num_events = multi_stream_schedule(gm, self.num_streams)
    self.generate_events(num_events)
```

piecewise 开启时，顶层这段被跳过，改在 `PiecewiseInterpreter.call_module` 里**对每个可捕获子图分别做**：

[piecewise_optimizer.py:103-106](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/compilation/piecewise_optimizer.py#L103-L106) —
```python
if self.max_num_streams > 1 and not self.enable_inductor:
    num_events = multi_stream_schedule(submod, self.max_num_streams)
    self.num_events = max(self.num_events, num_events)
    submod.recompile()
```

**关系小结**：

- piecewise 关闭 → 多流在**整图**层做一次。
- piecewise 开启 → 多流推迟到 **`piecewise_optimizer` 内部**、**逐子图**做，因为此时图已被切成多段，整图多流无意义；同时事件数要逐段取 max 汇总。
- 两者都关闭 → 既不多流也不 piecewise，`optimize` 直接返回 `gm`（仅做了融合 + re-inplace）。
- `enable_inductor=True` 时多流被跳过：Inductor 内部有自己的 pass，外部不再插手（见 `optimize` 注释）。

#### 4.3.4 代码实践

**实践目标**：理解多流调度对算子依赖的依赖，以及它为何必须「插控制算子」而非依赖 PyTorch 原生 stream。

**操作步骤**：

1. 打开 [auto_multi_stream.py:33-86](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/compilation/multi_stream/auto_multi_stream.py#L33-L86)，列出 `no_cost_ops`、`moe_ops`、`gemm_ops` 三类算子集合，思考为何 MoE 权重（20）远高于默认（1）。
2. 阅读文档 [torch_compile_and_piecewise_cuda_graph.md:212-248](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/features/torch_compile_and_piecewise_cuda_graph.md#L212-L248) 的 Auto Multi-stream 一节与那段 FX 节点列表，找出 `set_stream` / `wait_event` / `record_event` / `record_stream` 各出现的位置。
3. 解释：为什么这些控制算子「没有消费者」，却**不能**在多流调度后被 DCE 删掉？

**预期结果**：

- MoE 权重高是为了让 `router_gemm → MOE dispatch → group GEMM → combine` 这条数据依赖长链被识别为关键路径，串在主流上；而与之无依赖的 `silu_and_mul + quantize` 才能被塞进旁路流并行。
- 控制算子没有张量消费者，按常规 DCE 会被删除——但它们有**副作用**（切换/同步 stream）。所以多流调度完成后，后续不能再跑 DCE（`backend.py` 里 `remove_copy_for_mutates_args` 之后明确注释「After this pass, cannot run any dce!!!」，见 [backend.py:163-164](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/compilation/backend.py#L163-L164)）。

> 待本地验证：在支持多流的模型（如 DeepSeek-V3 FP4）上，对比 `max_num_streams: 1` 与 `max_num_streams: 2` 的 prefill 吞吐，观察多流带来的并行收益。

#### 4.3.5 小练习与答案

**练习 1**：为什么不直接在模型代码里写 `with torch.cuda.stream(s)`，而要搞一套 auto multi-stream？

**参考答案**：因为 `torch.compile` 会把用户 stream 降级成无消费者的 `set_stream` 节点，在 IR 转换中被 DCE 掉，手写的多流调度全部丢失。auto multi-stream 在 ATen IR 上重新建 DAG、调度，并把控制算子作为节点插入图中，才能让多流在编译后的图里真正生效。

**练习 2**：开启 piecewise 后，`max_num_streams` 还有效吗？

**参考答案**：有效，但作用位置变了。开启 piecewise 时，顶层 `optimize` 不再多流，而是在 `piecewise_optimizer` 里**对每个可捕获子图分别**调 `multi_stream_schedule(submod, max_num_streams)`。也就是说，多流在 piecewise 的每个图段内部生效。

---

## 5. 综合实践

**任务**：给一个假想的 DeepSeek-V3 FP4 服务场景，设计一份「开启 piecewise + 多流」的 `config.yml` 片段，并预测运行期一次 prefill 迭代的执行结构。

**步骤**：

1. 阅读装配点 [model_engine.py:550-577](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/model_engine.py#L550-L577)，确认 `Backend(...)` 的参数（`enable_piecewise_cuda_graph`、`capture_num_tokens`、`max_num_streams`、`enable_userbuffers`）全部来自 `TorchCompileConfig`，以及 `torch.compile(self.model.model, backend=..., fullgraph=...)` 的调用形式。
2. 阅读捕获流程 [model_engine.py:1962-1995](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/model_engine.py#L1962-L1995)（`_capture_piecewise_cuda_graphs`）与生成期图 warmup [model_engine.py:1184-1192](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/pyexecutor/model_engine.py#L1184-L1192)，理解两套图各自在 warmup 阶段如何被捕获。
3. 写出 `config.yml`（示例代码，需按你的硬件/模型调参）：

```yaml
# 示例代码：仅示范字段，实际数值需按硬件/模型调参
cuda_graph_config:
  enable_padding: true
  max_batch_size: 1024      # 生成期图的最大捕获 batch

torch_compile_config:
  enable_fullgraph: true
  enable_piecewise_cuda_graph: true
  capture_num_tokens: [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 3072]
  enable_userbuffers: true
  max_num_streams: 2
```

4. 用一句话回答三个问题（本讲实践任务的落点）：
   - **piecewise 如何在动态形状下保持高效**：按离散 `capture_num_tokens` 分桶录图，运行期把 token 数 padding 到下一档回放，用一次 padding 拷贝换掉海量 kernel launch 的 host 开销。
   - **可调捕获参数**：`capture_num_tokens`（piecewise 档位）、`cuda_graph_config.{batch_sizes, max_batch_size, enable_padding}`（生成期图档位与补齐）、`max_num_streams`（多流上限）、`enable_inductor` / `enable_userbuffers` / `enable_fullgraph`（编译开关）。
   - **piecewise 与 multi_stream 的关系**：开启 piecewise 时，整图层多流被跳过，多流下沉到 `piecewise_optimizer` 内部对每个可捕获子图分别调度；不开启 piecewise 时多流在整图 ATen IR 上做一次。

**验收**：你能向别人讲清「同一次前向里，哪些段落是 CUDA Graph 回放、哪些段落是 eager、多流在哪一层生效」，且能解释每个配置项对显存/并发/延迟的影响。

---

## 6. 本讲小结

- TensorRT-LLM 有**两套 CUDA Graph 机制**：生成期图（`cuda_graph_runner`，按 batch size 分桶，服务纯解码）与 piecewise 图（建在 `torch.compile` 上，按 token 总数分桶，服务 prefill/混合）。
- `CUDAGraphRunner` 用 `KeyType = (batch_size, draft_len, is_first_draft, short_seq_len_mode, is_all_greedy_sample)` 区分多种前向路径，分别录图；捕获集中在 warmup 的 `allow_capture()` 上下文，运行期见到新 key 回退 eager 而非当场捕获，以避免地址失效与毛刺。
- piecewise 依赖 `torch.compile` 的 `fullgraph` 追踪：所有算子必须可被识别，因此 attention/MoE 被包成 in-place「黑盒」自定义算子（呼应 `u10-l1`/`u10-l2` 的纯函数约定与 fake kernel）。
- 自定义后端 `Backend` 在 ATen IR 上做：融合 pass（AllReduce+RMSNorm、userbuffers）→ re-inplace（`remove_copy_for_mutates_args`）→ 多流/piecewise 分派。
- `piecewise_optimizer` 按一组 in-place attention 边界算子把图切成段，给每个可捕获段造一个 `PiecewiseRunner`，按 `capture_num_tokens` 分桶；运行期靠**强制 padding 到下一档**驯服动态形状。
- auto multi-stream 因 `torch.compile` 会丢弃用户 stream 而存在：它建 DAG、算关键路径、把控制算子直接插进图；开启 piecewise 时它下沉到每个子图内部执行。

---

## 7. 下一步学习建议

- 想看「两套图在单步循环里如何与调度器、采样器协同」：回到 `u3-l2`（PyExecutor 单步循环）与 `u8-l1`（调度器），结合本讲理解 `pad_batch` 造出的 dummy 请求如何参与批次。
- 想深入「自定义算子为何能被 torch.compile 追踪」：阅读 `tensorrt_llm/_torch/custom_ops/` 下的 `@torch.library.custom_op` 与 `register_fake` 实现，并对照 `u10-l2`（量化）的 `trtllm::` 命名规约。
- 想了解「另一条编译路径」：进到 `u12-l1`（AutoDeploy 后端），对比 AutoDeploy 的 FX 图变换 + `torch.export` 与本讲 PyTorch 后端的 `torch.compile` 自定义后端有何异同。
- 想做性能验证：用 `trtllm-bench`（`u11-l3`）跑「关 piecewise / 开 piecewise / 开 piecewise + 多流」三组对比，观察 prefill host 开销与吞吐变化。
