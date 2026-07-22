# EAGLE 草稿模型投机

> 本讲承接 [u10-l1 投机解码概览与算法注册](u10-l1-spec-overview-registry.md)。u10-l1 讲了投机解码「草稿 + 验证」的通用原理与 `spec_registry` 的插件注册机制；本讲把镜头推近到 EAGLE 这一支，精读它如何用一个轻量草稿模型预测多 token、再由主模型批量验证。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 EAGLE 的「草稿树生成 → 树注意力验证 → 接受前缀」三段式流程，以及它为什么能加速。
- 看懂 `eagle_worker_v2.py` 中 `EagleDraftWorker`（草稿侧）与 `EAGLEWorkerV2`（编排侧）的分工，定位「草稿生成」与「主模型验证」在源码中的分界。
- 读懂 `eagle_info.py` 中三类 `SpecInput`（`EagleDraftInput` / `EagleVerifyInput` / `EagleDraftExtendInput`）分别携带什么张量。
- 理解本次更新引入的关键惯用法：**草稿 worker 现在通过 `get_spec()` 读取投机相关配置，而不是直接读 `server_args`**，并能说清自适应投机（adaptive spec）下为什么要 `_apply_adaptive_config` 同时改写「配置袋」与 `server_args` 两处。

## 2. 前置知识

在进入 EAGLE 之前，先用三段话把直觉建立起来。

**为什么自回归 decode 需要「投机」？** LLM 逐 token 生成时，每生成一个 token 都要把整张网络跑一遍（一次 forward），但每次 forward 实际计算量很小，真正的时间花在「把模型权重和 KV 缓存从显存搬到计算单元」的带宽上——这就是常说的 **decode 是访存受限（memory-bandwidth bound）**。于是单步 forward 的算力被大量浪费。如果能在一次主模型 forward 里同时验证好几个候选 token，就能把多步的带宽成本摊到一次 forward 上，从而提升每秒产出 token 数（吞吐）。

**EAGLE 的核心改进是什么？** 朴素投机解码用一个独立小模型当草稿器，但它看不到主模型「在想什么」，准确率有限。EAGLE 让草稿模型**吃主模型的隐状态（hidden states）**作为额外输入——即「主模型刚刚算出来的特征」——再叠加 token embedding，去预测接下来的若干 token。草稿器因为借了主模型的「思路」，命中率显著提高。

**为什么要「树」而不是「链」？** 草稿器在每一步可以给出 top-k 个候选（而不是只选概率最高的一个），于是一个 `num_steps` 步、每步 `topk` 分支的草稿构成一棵候选树。主模型用**树注意力（tree attention）**在这一次 forward 里同时给树上所有候选打分，再沿着树找出最长被接受的前缀。被接受的 token 越多，一次 forward 产出的有效 token 就越多。

几个关键量（CLI 参数，均属 `NS("spec")` 命名空间，见 [server_args.py:1945-1961](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/server_args.py#L1945-L1961)）：

| 参数 | 含义 |
| --- | --- |
| `--speculative-num-steps` | 草稿器连续前向的步数 |
| `--speculative-eagle-topk` | 每步取的候选分支数；`1` 时退化成链 |
| `--speculative-num-draft-tokens` | 一次草稿产出的总 token 数（树的大小） |

三者满足 `speculative_num_draft_tokens == speculative_num_steps * topk + 1`（多出的 `+1` 是「bonus」根 token，详见后文）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [eagle_worker_v2.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_v2.py) | 核心实现。`EagleDraftWorker` 负责草稿模型的前向与草稿树构造；`EAGLEWorkerV2` 编排「草稿→验证→草稿扩展」三步，并管理自适应投机 |
| [eagle_info.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_info.py) | 三类 `SpecInput` 数据结构：草稿输入、验证输入、草稿扩展输入 |
| [eagle_utils.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_utils.py) | 树掩码构造 `build_tree_kernel_efficient`、接受判定 `eagle_sample`、验证前准备 `eagle_prepare_for_verify` 等纯函数 |
| [eagle_worker_common.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_common.py) | 草稿/验证的「共享编排函数」：`prepare_for_draft`、`build_eagle_verify_input`、`run_eagle_verify`。`EAGLEWorkerV2` 把细节委托给这里 |
| [spec_info.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/spec_info.py) | `SpeculativeAlgorithm` 枚举与 `create_worker` 工厂；`is_eagle()` 判定 |

> 小贴士：本次更新把 `run_eagle_verify`、`build_eagle_verify_input`、`prepare_for_draft` 等已抽取到 `eagle_worker_common.py`。阅读时若发现 `EAGLEWorkerV2` 的方法只是「调一个共享函数」，去 `eagle_worker_common.py` 找实现即可。

---

## 4. 核心概念与源码讲解

### 4.1 EAGLE 的草稿-验证总体流程与三类数据结构

#### 4.1.1 概念说明

EAGLE 在 SGLang 里以 `EAGLEWorkerV2` 这个「spec worker」的形式存在，它包裹了一个**目标 worker**（真正的大模型 `TpModelWorker`）和一个**草稿 worker**（轻量草稿模型）。整个投机流程围绕三类数据结构（`SpecInput` 的子类）流转：

- **`EagleDraftInput`**：喂给草稿器做多步展开的输入——上一轮草稿产出的 top-k 概率/索引、主模型给的 hidden states、bonus token。
- **`EagleVerifyInput`**：喂给主模型做一次树验证的输入——候选 token 树、树注意力掩码、位置、以及「按树形状取回结果」所需的 `retrieve_*` 索引。
- **`EagleDraftExtendInput`**：喂给草稿器做「草稿扩展」的输入——把本轮被接受的 token 写进草稿模型的 KV 缓存，并为下一轮草稿准备 hidden states。

为什么需要「草稿扩展（draft_extend）」这一步？因为草稿器在前向时也要维护自己的 KV 缓存；被接受的 token 必须正确地补进草稿器的 KV，下一轮草稿才能基于正确的上下文继续展开。这一步在代码里是独立的一次草稿前向。

#### 4.1.2 核心流程

EAGLE 的入口是 [EAGLEWorkerV2.forward_batch_generation](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_v2.py#L1126-L1221)。它按 `forward_mode` 分两条路径：

**路径 A：prefill（首 token 阶段）**——这时还没有草稿树可言：

1. 调 `target_worker.forward_batch_generation` 做主模型 prefill，捕获主模型整段 hidden states（`CaptureHiddenMode.FULL`）。
2. 调 `_draft_extend_for_prefill`：草稿器对 prefill 结果做一次扩展，产出一个 `EagleDraftInput` 作为下一轮 decode 的草稿种子。

**路径 B：decode（稳态投机阶段）**——这是真正的草稿-验证循环：

1. 若 `spec_info` 为空，先用 `EagleDraftInput.create_idle_input` 建一个空闲桩。
2. **草稿生成**：`self.draft_worker.draft(batch)` → 产出一个 `EagleVerifyInput`（候选树）。
3. **树验证**：`self.verify(batch)` → 主模型跑一次 `TARGET_VERIFY` 前向，用 `eagle_sample` 判定接受，返回 `GenerationBatchResult`（含 `accept_lens`、`next_token_ids`）。
4. **草稿扩展**：`_draft_extend_for_decode(batch, batch_output)` → 草稿器把被接受 token 写进自己的 KV，并把新的 hidden states/topk 写进 `next_draft_input`，供下一轮 `draft` 使用。

伪代码如下：

```
forward_batch_generation(batch):
    if batch 是 prefill/extend:
        target_out = target_worker.forward(...)            # 主模型 prefill
        return draft_extend_for_prefill(...) -> EagleDraftInput  # 种子
    else:  # decode
        verify_input = draft_worker.draft(batch)           # ① 草稿树
        out = verify(batch)                                # ② 主模型树验证 + 接受
        draft_worker.draft_extend_for_decode(batch, out)   # ③ 写回草稿 KV
        return out
```

一个值得注意的边界情形：当 `speculative_num_steps == 0`（高批尺寸下临时关闭投机）时，`forward_batch_generation` 走 [_build_trivial_verify_input](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_v2.py#L1223-L1278)——构造一个只有根节点的 1-token 验证树，等价于一次普通 decode，但复用了已有的 `TARGET_VERIFY` 图。

#### 4.1.3 源码精读

看 [forward_batch_generation](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_v2.py#L1126-L1221) 的 decode 分支核心几行（草稿→验证→扩展三步在这里一目了然）：

```python
# ① 草稿：draft_worker 产出候选树
with (... spec_stage_span("draft")):
    verify_input: EagleVerifyInput = self.draft_worker.draft(batch)
assert verify_input.is_verify_input()
batch.spec_info = verify_input
# ② 验证：主模型树验证 + 接受判定
batch_output = self.verify(batch, grammar_barrier=grammar_barrier)
...
# ③ 草稿扩展：把接受结果写回草稿 KV，准备下一轮
with (... spec_stage_span("draft_extend")):
    self.draft_worker._draft_extend_for_decode(batch, batch_output)
```

三类输入的归属看 [eagle_info.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_info.py) 的三个类定义。`EagleVerifyInput` 携带验证树本体（[eagle_info.py:17-83](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_info.py#L17-L83)）：

```python
@dataclass
class EagleVerifyInput(SpecInput):
    draft_token: torch.Tensor          # 候选 token 树（拍平）
    custom_mask: torch.Tensor          # 树注意力掩码
    positions: torch.Tensor            # 每个 token 的位置
    retrieve_index: torch.Tensor       # 按树形状取回结果的索引
    retrieve_next_token: torch.Tensor
    retrieve_next_sibling: torch.Tensor
    spec_steps: int
    topk: int
    draft_token_num: int               # 树大小（= num_draft_tokens）
    ...
```

`EagleDraftInput` 携带草稿器循环所需的「记忆」（[eagle_info.py:144-180](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_info.py#L144-L180)）：

```python
@dataclass
class EagleDraftInput(SpecInput):
    topk_p: torch.Tensor = None        # (b, topk) 概率
    topk_index: torch.Tensor = None    # (b, topk) token id
    hidden_states: Optional[torch.Tensor] = None  # (b, hidden) 主模型 hidden
    bonus_tokens: torch.Tensor = None  # 「+1」根 token
    ...
```

`EagleDraftExtendInput` 携带草稿扩展前向的输入（[eagle_info.py:297-338](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_info.py#L297-L338)），关键字段是主模型 hidden states 与每条请求的接受计数 `num_accept_tokens`。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：在源码中把「草稿生成」与「主模型验证」两阶段的分界钉死，并看清三类 `SpecInput` 在一轮 decode 中的流转。

**操作步骤**：

1. 打开 [eagle_worker_v2.py:1126](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_v2.py#L1126) 的 `forward_batch_generation`。
2. 在 decode 分支里找到 `self.draft_worker.draft(batch)`，这是**草稿阶段**的出口；它的返回值类型是 `EagleVerifyInput`，被赋给 `batch.spec_info`。
3. 紧接着的 `self.verify(batch, ...)` 是**验证阶段**的入口（最终委托给 [run_eagle_verify](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_common.py#L456-L686)）。
4. 最后的 `_draft_extend_for_decode` 是**草稿扩展阶段**。

**需要观察的现象**：三步之间 `batch.spec_info` 的类型如何变化——`EagleDraftInput`（进入 draft）→ `EagleVerifyInput`（draft 产出、verify 消费）→ 通过 `batch_output.next_draft_input` 传回下一轮的 `EagleDraftInput`。

**预期结果**：你能用一句话画出 `EagleDraftInput ⇄ EagleVerifyInput` 的循环：草稿吃 `EagleDraftInput` 吐 `EagleVerifyInput`，验证吃 `EagleVerifyInput` 吐 `GenerationBatchResult`，草稿扩展再从结果里攒出下一轮 `EagleDraftInput`。

#### 4.1.5 小练习与答案

**练习 1**：`speculative_num_steps == 0` 时为什么不直接走普通 decode，而要构造一个「平凡的 1-token 验证树」？

> **参考答案**：为了复用已经捕获好的 `TARGET_VERIFY` CUDA Graph（其 `draft_token_num=1`）。这样即使临时关闭投机，也能走同一条已优化的图路径，避免额外分支；同时草稿 KV 仍由 `draft_extend` 保温，等批尺寸回落、投机重新开启时能「热启动」。参见 [_build_trivial_verify_input](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_v2.py#L1223-L1234) 的注释。

**练习 2**：`bonus_tokens` 为什么叫「+1」根 token？它从哪来、到哪去？

> **参考答案**：验证接受的最长前缀之后，主模型还会额外采样一个 token（即「下一 token」预测），这个 token 不属于草稿树、却要作为下一轮草稿树的根。`bonus_tokens` 由验证阶段的 `eagle_sample`/`fill_bonus_tokens_func` 产出（[eagle_worker_common.py:639-648](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_common.py#L639-L648)），并在下一轮 `build_eagle_verify_input` 里成为树的根。

---

### 4.2 草稿生成：EagleDraftWorker.draft 与 draft_forward

#### 4.2.1 概念说明

草稿阶段要回答一个问题：**给定主模型刚算出的 hidden states，草稿器认为接下来最可能的若干 token 是什么？** 这是一棵树的构造过程。

草稿器本身也是一个 `TpModelWorker`（加载的是轻量草稿模型权重），它被连续前向 `speculative_num_steps` 次。每一步：吃上一步的 top-k token 与 hidden states → 算出 logits → 取 top-k 候选 → 把候选的 hidden states 反馈给下一步。`num_steps` 步、每步 `topk` 个分支，就长成一棵候选树。

关键优化：当 `topk == 1` 时，树退化成一条**链**，`parent_list`（每个候选的父节点）和 `top_scores_index`（排序后的候选位置）在运行期是不变量，可以预算成常量；并且有一个专门的 CUDA kernel `draft_topk1_postprocess` 把「取 argmax + 推进 position」融合进单次 kernel 调用，避免每步都 `cat`/`topk`/`sort`。这就是代码里反复出现的 **topk=1 链式快路径**。

#### 4.2.2 核心流程

`EagleDraftWorker.draft` 的流程（[eagle_worker_v2.py:504-565](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_v2.py#L504-L565)）：

1. `prepare_for_draft`：为草稿前向分配每步的 KV 写入槽位 `out_cache_loc`，并构造 `ForwardBatch`。
2. 若能走 CUDA Graph（`can_cuda_graph`），用 `cuda_graph_runner.execute` 一次回放整条草稿链；否则走 `draft_forward` 的 eager 路径。
3. 把草稿结果（`parent_list`、`top_scores_index`、`draft_tokens`、`draft_probs`）交给 `build_eagle_verify_input` 组装成 `EagleVerifyInput`。

`draft_forward` 内部（[eagle_worker_v2.py:567-741](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_v2.py#L567-L741)）是一个 `for i in range(speculative_num_steps)` 循环：

- 第 `i` 步选 token（`select_top_k_tokens` 或 topk=1 的 `draft_tokens_topk1` 路径）。
- 最后一步（`i == num_steps - 1`）只取 token、不再前向——因为草稿 prefill 已经贡献了 1 个 token，循环里再前向 `num_steps - 1` 次即可凑齐。
- 中间步骤：设置 `input_ids` / `out_cache_loc[i]` / `hidden_states`，在带 `forward_context`（指定第 `i` 步的注意力后端）下跑 `draft_runner.forward`，再从 logits 取 top-k。

每步草稿写 KV 的槽位布局由 [per_step_draft_out_cache_loc](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_utils.py#L51-L72) 统一描述：把 `out_cache_loc` reshape 成 `(num_steps, bs*topk)`，第 `i` 行就是第 `i` 步要写入的物理槽。

#### 4.2.3 源码精读

草稿循环的核心（节选自 [draft_forward](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_v2.py#L627-L711)）：

```python
for i in range(self.speculative_num_steps):
    input_ids, hidden_states, scores, tree_info = select_top_k_tokens(
        i, topk_p, topk_index, hidden_states, scores, self.topk)
    ...
    if i == self.speculative_num_steps - 1:
        break  # 最后一步不再前向
    forward_batch.input_ids = input_ids
    forward_batch.out_cache_loc = out_cache_loc[i]
    spec_info.hidden_states = hidden_states
    with forward_context(ForwardContext(
            attn_backend=self.draft_attn_backend.attn_backends[i])):
        logits_output = self.draft_runner.forward(forward_batch).logits_output
    probs = renorm_draft_probs(logits_output.next_token_logits, ...)
    topk_p, topk_index = fast_topk(probs, self.topk, dim=-1)
    forward_batch.positions.add_(1)   # 链式推进位置
    hidden_states = logits_output.hidden_states
```

topk=1 的快路径在循环外预先判断（[eagle_worker_v2.py:596-615](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_v2.py#L596-L615)），命中时直接用一个连续缓冲 `draft_tokens_topk1` 让 kernel 逐列写入，循环结束后用预算常量返回（[eagle_worker_v2.py:724-735](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_v2.py#L724-L735)）。这些预算常量由 [_rebuild_topk1_chain_buffers](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_v2.py#L258-L290) 在初始化（及自适应切换步数）时构建。

非快路径（`topk > 1`）的草稿树由 [organize_draft_results](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_utils.py#L91-L118) 装配：把各步分数拼起来，用 `torch.topk` 选出 `num_draft_token - 1` 个最优候选，并整理出 `parent_list`。

#### 4.2.4 代码实践（运行型）

**实践目标**：对比开启 EAGLE 前后的吞吐与接受率，并在源码中标注草稿/验证两阶段的分界。

**操作步骤**：

1. 先不开投机，基线启动（待本地验证，示例命令，请替换为本地可用的小模型）：
   ```bash
   python -m sglang.launch_server --model-path <target_model> --port 30000
   ```
2. 关闭该服务，开启 EAGLE（CLI 形式取自真实部署片段 `docs_new/.../qwen36-deployment.jsx`）：
   ```bash
   python -m sglang.launch_server --model-path <target_model> \
     --speculative-algorithm EAGLE \
     --speculative-draft-model-path <draft_model> \
     --speculative-num-steps 3 \
     --speculative-eagle-topk 1 \
     --speculative-num-draft-tokens 4 \
     --port 30000
   ```
3. 用仓库自带基准压测两次（命令仅为示意，请按本地 benchmark 工具实际参数运行）：
   ```bash
   python -m sglang.bench_server --backend vllm --host 127.0.0.1 --port 30000 ...
   ```

**需要观察的现象**：

- 吞吐（tokens/s）应高于基线；decode 延迟（每 token）应下降。
- 服务日志中 EAGLE 会周期打印接受率统计（草稿被主模型接受的比例）。

**预期结果 / 待本地验证**：EAGLE 在接受率较高时带来明显吞吐增益；接受率很低（草稿模型与目标不匹配）时收益有限甚至有开销。具体数值依赖模型与硬件，**待本地验证**。

> 也可改用「源码阅读型」替代运行：读 [test/registered/unit/spec/test_eagle_worker_v2_topk1_fastpath.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/test/registered/unit/spec/test_eagle_worker_v2_topk1_fastpath.py) 的断言，理解 topk=1 快路径与慢路径 `organize_draft_results` 的等价性。注意：该测试目前因「ServerArgs 配置命名空间迁移」被整体 `skip`（见文件顶部 `_DEFER_REASON`），但它的断言仍是理解快路径不变量的好材料。

#### 4.2.5 小练习与答案

**练习 1**：为什么草稿循环只前向 `num_steps - 1` 次，却能产出 `num_steps` 个 token？

> **参考答案**：草稿 prefill / draft_extend 已经免费贡献了第 0 个 token（吃主模型 hidden 直接预测一个 token），所以循环里只需再前向 `num_steps - 1` 次补齐剩余 token。循环中 `if i == self.speculative_num_steps - 1: break` 就是为此跳过最后一次前向，参见 [eagle_worker_v2.py:638-640](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_v2.py#L638-L640)。

**练习 2**：`topk == 1` 时，`parent_list` 和 `top_scores_index` 为什么可以预算成常量？

> **参考答案**：topk=1 时树退化成链，第 `i` 个候选的父就是第 `i-1` 个（根的父记为 -1），`top_scores_index` 就是 `[0,1,...,num_steps-1]`。这些值与具体 token、batch 无关，只依赖步数，所以可在初始化时预算；`_rebuild_topk1_chain_buffers` 还会在自适应改步数时重建。参见 [eagle_worker_v2.py:282-290](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_v2.py#L282-L290)。

---

### 4.3 树验证与接受：build_tree + eagle_sample

#### 4.3.1 概念说明

草稿吐出一棵候选树后，主模型要在**一次前向**里给整棵树的所有节点打分。难点在于注意力：树里每个候选只能「看到」它祖先链上的 token（不能看到兄弟分支）。这用一个**树注意力掩码（tree mask）**表达——掩码是一个布尔矩阵，`mask[i][j] = True` 表示候选 `i` 可以注意候选 `j`。

打完分后做**接受判定**：

- **贪心（greedy）情形**：把主模型对每个候选位置的 argmax 与草稿候选比较，沿着树自根而下找最长匹配前缀；匹配到的 token 全部接受，再加 1 个主模型新采样的「bonus」token。
- **非贪心情形**：用 `tree_speculative_sampling_target_only` 做「树式投机采样」，比对主模型与草稿的概率分布来决定接受/拒绝（这要求草稿也产出 `draft_probs`）。

接受的 token 数记为 `accept_lens`（含 bonus，即 `num_correct_drafts + 1`）。

#### 4.3.2 核心流程

验证编排由 [run_eagle_verify](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_common.py#L456-L686) 完成（`EAGLEWorkerV2.verify` 委托给它）：

1. `eagle_prepare_for_verify`：为候选树分配主模型 KV 槽，构造 `TARGET_VERIFY` 的 `ForwardBatch`。
2. 主模型前向（`target_worker.forward_batch_generation(..., is_verify=True)`）拿到每个候选位置的 logits。
3. `eagle_sample`：应用采样参数与可选的文法掩码，调用接受判定 kernel，得到 `predict`、`accept_index`、`accept_lens`。
4. 后处理：`fill_bonus_tokens_func` 取 bonus；topk>1 时 `_finalize_accept_tree_path` 把被接受路径搬到每请求块前部，方便下游链式布局。

树掩码与取回索引由 [build_tree_kernel_efficient](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_utils.py#L133-L274) 一次性算出（多后端：`sgl_kernel` CUDA、Triton、NPU、CPU）。它产出 `tree_mask`、`positions`、`retrieve_index`/`retrieve_next_token`/`retrieve_next_sibling`——后三者描述「如何按树形状从拍平的 logits 里取回每个请求的有效 token」。

#### 4.3.3 源码精读

接受判定的分派在 [eagle_sample](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_utils.py#L560-L788)，贪心分支调用 [verify_tree_greedy_func](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_utils.py#L359-L424)：

```python
if sampling_info.is_all_greedy or _is_cpu or ...:
    target_predict = torch.argmax(next_token_logits, dim=-1).reshape(
        bs, verify_input.draft_token_num)
    predict, accept_index, num_correct_drafts = verify_tree_greedy_func(
        predicts=predict, accept_index=accept_index,
        accept_token_num=num_correct_drafts,
        candidates=candidates,
        retrieve_index=verify_input.retrieve_index, ...)
```

注意函数末尾的命名约定（[eagle_utils.py:785-788](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_utils.py#L785-L788)）：内部 `num_correct_drafts` 只算草稿命中数，返回时通过 out-of-place `+1` 把 bonus 算进去，使 `accept_lens` 语义一致（命名文档 C2）：

```python
return predict, num_correct_drafts + 1, accept_index
```

接受统计回写到 `GenerationBatchResult.accept_lens`，并由 [run_eagle_verify](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_common.py#L675-L686) 返回：

```python
return GenerationBatchResults(
    logits_output=logits_output, next_token_ids=predict,
    next_draft_input=next_draft_input, accept_lens=accept_lens, ...)
```

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：跟踪一次「草稿 token 被接受」的判定链路，确认 `accept_lens` 的来源。

**操作步骤**：

1. 从 [EAGLEWorkerV2.verify](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_v2.py#L1521-L1536) 进入 `run_eagle_verify`。
2. 定位 [eagle_worker_common.py:609-613](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_common.py#L609-L613) 的 `eagle_sample(...)` 调用，记录它返回的 `accept_lens`。
3. 在 [eagle_utils.py:641-651](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_utils.py#L641-L651) 看 greedy 分支如何调用 `verify_tree_greedy_func`。

**需要观察的现象**：`accept_lens[i]` 是每条请求被接受的 token 数（含 bonus）。`batch.seq_lens + accept_lens` 就是该请求验证后的新序列长度（见 [eagle_worker_common.py:614](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_common.py#L614)）。

**预期结果**：你能指出 `accept_lens` 由接受 kernel 产出，bonus 在 `eagle_sample` 返回时统一 `+1`，最终经 `GenerationBatchResult` 流向调度器。

#### 4.3.5 小练习与答案

**练习 1**：树注意力掩码 `tree_mask` 的形状由什么决定？为什么 CPU 与 GPU 用不同的 `TreeMaskMode`？

> **参考答案**：`FULL_MASK` 模式下掩码大小为 `seq_lens_sum * num_verify_tokens + num_verify_tokens^2 * bs`（每个候选可注意整段前缀 + 树内祖先链）。CPU 的 verify kernel（intel_amx）直接消费 `qlen × qlen` 的 `QLEN_ONLY` 掩码，GPU kernel 用 `FULL_MASK`。参见 [build_tree_kernel_efficient](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_utils.py#L152-L190) 与 [default_tree_mask_mode](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_utils.py#L127-L130)。

**练习 2**：`accept_lens` 与 `num_correct_drafts` 差 1，差在哪？

> **参考答案**：差在 bonus token。`num_correct_drafts` 只统计草稿命中数，bonus 是验证后主模型额外采样的「下一 token」，恒为 1。返回时 `num_correct_drafts + 1` 才是下游需要的「本轮总产出 token 数」。参见 [eagle_utils.py:785-788](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_utils.py#L785-L788)。

---

### 4.4 get_spec() 投机配置访问与热改（本次更新的核心）

#### 4.4.1 概念说明

本次代码更新（diff `4a55fdb..977ea336`）对 EAGLE 做了一次**配置读取惯用法的机械迁移**：把过去直接读 `self.server_args.speculative_*` / `get_server_args().speculative_*` 的写法，统一改成读**命名空间配置袋** `get_spec().speculative_*`。

回顾 [u2-l5 RuntimeContext](u2-l5-runtime-context-config-bags.md)：运行期配置被 `publish(server_args, role=)` 按 `NS(...)` 标注快照成多个只读「配置袋」，其中 `NS("spec")` 的字段（`speculative_eagle_topk`、`speculative_num_steps`、`speculative_num_draft_tokens`、`speculative_use_rejection_sampling`、`speculative_token_map`、`speculative_accept_threshold_*` 等）归入 **spec 袋**，由 `get_spec()` 访问器返回。袋是只读快照，读取可被 `torch.compile` 追踪；而 `server_args` 在 `__post_init__` 后冻结为只读留档。

迁移后的规则很清晰：

- **读配置**：用 `get_spec().xxx`（运行期读者，如草稿 worker）。
- **改配置**：运行期唯一审计入口是 `get_context().override(source, **fields)`（只写袋、不碰 `server_args`、全或无校验）。

#### 4.4.2 核心流程

迁移点遍布 `eagle_worker_v2.py` / `eagle_info.py` / `eagle_utils.py`。典型迁移：

| 旧写法 | 新写法 | 位置 |
| --- | --- | --- |
| `self.server_args.speculative_use_rejection_sampling` | `get_spec().speculative_use_rejection_sampling` | worker 各处 |
| `self.server_args.speculative_token_map` | `get_spec().speculative_token_map` | `init_token_map` |
| `self.server_args.model_impl` | `get_model().model_impl` | `_capture_cuda_graphs` |
| `self.server_args.cuda_graph_config.decode.backend` | `get_exec().graph.cuda_graph_config.decode.backend` | `_capture_cuda_graphs` |
| `get_server_args().speculative_use_rejection_sampling` | `get_spec().speculative_use_rejection_sampling` | `eagle_info.py`、`eagle_utils.eagle_sample` |
| `get_server_args().speculative_accept_threshold_*` | `get_spec().speculative_accept_threshold_*` | `eagle_utils.eagle_sample` |

**一个重要例外——自适应投机的双写**。`EAGLEWorkerV2` 支持自适应投机（`--speculative-adaptive`）：它会临时改变 `speculative_num_steps` / `speculative_num_draft_tokens` 来预建多套 per-step 运行态。此时同一个字段被**两类读者**读取：

- 运行期读者（`tp_worker` / `tokenizer_manager`）经 `get_spec()` 读袋；
- 注意力后端与 CUDA Graph runner 仍以 `model_runner.server_args.<field>` 直接读 `server_args`（**尚未迁移到袋**）。

如果只改袋，就会出现「为覆盖后的步数捕获了图，但注意力后端却用 `server_args` 里陈旧的步数去算元数据」的不一致，导致 CUDA Graph 捕获时非法内存访问。因此新增的 [_apply_adaptive_config](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_v2.py#L1432-L1447) **同时写两处**。

#### 4.4.3 源码精读

spec 袋的读取示例（[eagle_worker_v2.py:142-145](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_v2.py#L142-L145)）：

```python
self.topk = server_args.speculative_eagle_topk
if get_spec().speculative_use_rejection_sampling:
    assert self.topk == 1, "Chain speculative sampling supports only topk=1"
```

注意 `self.topk` 在 `__init__` 时从 `server_args` 取一次（构造期可读），此后运行期判断走 `get_spec()`。

双写方法是本次更新最有教学价值的一处（[_apply_adaptive_config](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_v2.py#L1432-L1447)）：

```python
def _apply_adaptive_config(self, source: str, **fields) -> None:
    """Rebind adaptive-spec config on both stores that back these fields.
    ...
    Those fields are read from two places: the resolved config bag
    (runtime readers via get_spec()) *and* server_args — attention backends
    and graph runners read the count as model_runner.server_args.<field>
    and are not migrated to the bag. Write both so an in-flight CUDA-graph
    capture and later reads agree; ..."""
    get_context().override(source, **fields)
    self.server_args.override(source, **fields)
```

这两行一前一后：第一行走「合法的运行期改写入口」改袋；第二行用 `server_args` 自带的 `override()`（凭 `_in_override` 令牌放行，参见 [u2-l2](u2-l2-launch-and-server-args.md)）改 `server_args`。`apply_runtime_state` / `_override_worker_state` 在切换自适应态时都经它同步。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：把 EAGLE 源码里读取投机配置的地方找全，并说清「读哪个访问器」。

**操作步骤**：

1. 在 `python/sglang/srt/speculative/` 下检索 `get_spec()`：
   ```bash
   grep -rn "get_spec()" python/sglang/srt/speculative/eagle_worker_v2.py
   grep -rn "get_spec()" python/sglang/srt/speculative/eagle_info.py python/sglang/srt/speculative/eagle_utils.py
   ```
2. 对每个命中点，确认它读的是哪个字段（如 `speculative_eagle_topk`、`speculative_use_rejection_sampling`）。
3. 在 [server_args.py:1945-1961](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/server_args.py#L1945-L1961) 确认这些字段都标注了 `NS("spec")`，因此归入 spec 袋、由 `get_spec()` 暴露。

**需要观察的现象**：草稿 worker 里所有「运行期需要知道的投机开关」都已迁移到 `get_spec()`；而构造期一次性取值的字段（如 `self.topk`、`self.speculative_num_steps`）仍可在 `__init__` 直接读 `server_args`。

**预期结果**：你能画出一张表——`speculative_eagle_topk` / `speculative_num_steps` / `speculative_num_draft_tokens` / `speculative_use_rejection_sampling` / `speculative_token_map` / `speculative_accept_threshold_single` / `speculative_accept_threshold_acc` 全部「属 spec 袋、读 `get_spec()`」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_apply_adaptive_config` 要同时调 `get_context().override(...)` 和 `self.server_args.override(...)`？只改一处会怎样？

> **参考答案**：因为 `speculative_num_steps` / `speculative_num_draft_tokens` 同时被两类读者读取：运行期读者经 `get_spec()` 读袋，注意力后端与 graph runner 仍直接读 `server_args`（未迁移）。只改袋会导致「图为覆盖后的步数捕获、但注意力元数据按陈旧的 `server_args` 步数计算」，在 CUDA Graph 捕获时触发非法内存访问。详见 [_apply_adaptive_config](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_v2.py#L1432-L1447) 的 docstring。

**练习 2**：`self.topk = server_args.speculative_eagle_topk` 为什么可以直接读 `server_args`，而循环里的 `get_spec().speculative_use_rejection_sampling` 不能？

> **参考答案**：`self.topk` 在 `__init__` 构造期取值一次并固化到实例属性——构造期 `server_args` 是合法可读源（且这是该字段「快照到 spec 袋」之前的读取）。而 `draft_forward` 等运行期热路径里的判断必须读袋 `get_spec()`，因为运行期改写（如自适应投机）只写袋，读 `server_args` 会拿到陈旧值。这是 [general-code-style](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/.claude/rules/general-code-style.md)「构造期抽取、运行期读属性」与 [u2-l5](u2-l5-runtime-context-config-bags.md)「读袋不读 server_args」两条规则的交汇。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来，画出一条 EAGLE decode 请求在 `EAGLEWorkerV2` 内的完整数据流，并标注每个阶段的配置来源。

请完成：

1. **流程图**：以 `forward_batch_generation`（decode 分支）为骨架，画出 `draft` → `verify` → `draft_extend_for_decode` 三步，标出每步的输入/输出 `SpecInput` 类型（`EagleDraftInput` / `EagleVerifyInput` / `EagleDraftExtendInput`）与关键张量（`draft_token`、`custom_mask`、`hidden_states`、`bonus_tokens`、`accept_lens`）。
2. **阶段分界**：在图上标出「草稿生成」（`draft_worker.draft` / `draft_forward`）与「主模型验证」（`verify` / `run_eagle_verify` / `eagle_sample`）的分界线。
3. **配置溯源**：对图上涉及的每个配置（`speculative_eagle_topk`、`speculative_num_steps`、`speculative_num_draft_tokens`、`speculative_use_rejection_sampling`），标注它「在哪个源码点被读、经哪个访问器（`get_spec()` 还是构造期 `server_args`）」。对自适应投机，额外标注 `_apply_adaptive_config` 双写的两处。
4. **验证**（可选运行）：用 [4.2.4](#424-代码实践运行型) 的命令开启 EAGLE，观察日志中的接受率，验证你画的「接受 token 数 = `accept_lens`（含 bonus）」与实际产出 token 数一致。

> 提示：若不便运行，可改为阅读 [run_eagle_verify](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_common.py#L456-L686) 与 [draft_forward](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/eagle_worker_v2.py#L567-L741) 的源码，凭函数签名与返回值把数据流补全。

## 6. 本讲小结

- EAGLE = **吃主模型 hidden states 的轻量草稿器** + **树注意力批量验证**；一次主模型 forward 验证整棵候选树，接受最长前缀并多产 1 个 bonus token。
- `EAGLEWorkerV2.forward_batch_generation` 是编排中枢：decode 阶段按 **草稿 → 验证 → 草稿扩展** 三步循环，分别对应 `EagleDraftInput`、`EagleVerifyInput`、`EagleDraftExtendInput` 三类 `SpecInput` 的流转。
- 草稿树由 `draft_forward` 的 `num_steps` 步循环构造；`topk == 1` 时退化成链，走预算常量 + 融合 kernel 的快路径。
- 验证由 `run_eagle_verify` 编排、`eagle_sample` 判定接受；`accept_lens` 含 bonus（`num_correct_drafts + 1`）。
- **本次更新的核心**：投机配置读取从 `server_args` / `get_server_args()` 迁到 **`get_spec()`**（spec 命名空间袋）；自适应投机因有「未迁移到袋的读者」而新增 `_apply_adaptive_config` **双写袋与 `server_args`**。

## 7. 下一步学习建议

- **延续投机主线**：阅读 [ngram_worker.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/ngram_worker.py)（u10-l3），对比「无草稿模型」的 N-gram 投机与 EAGLE 的取舍——前者零模型成本但候选质量依赖语料。
- **深入自适应投机**：读 [adaptive_runtime_state.py](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/speculative/adaptive_runtime_state.py)，理解 `AdaptiveController` 如何按批尺寸在多套 `SpecRuntimeState` 间切换，并把本讲的 `_apply_adaptive_config` 双写放进它的调用链。
- **多后端验证 kernel**：若对高性能算子感兴趣，读 `sgl-kernel` 的 `build_tree_kernel_efficient` / `verify_tree_greedy` 实现，并对照 [u11-l2 统一算子体系](u11-l2-sgl-kernel-jit-kernel.md) 理解 AOT/JIT 选择。
- **CUDA Graph 捕获细节**：EAGLE 草稿/扩展各有独立 graph runner（`EAGLEDraftCudaGraphRunner` / `EAGLEDraftExtendCudaGraphRunner`），可结合 [u7-l1 CUDA Graph](u7-l1-cuda-graph.md) 理解捕获与回放。
