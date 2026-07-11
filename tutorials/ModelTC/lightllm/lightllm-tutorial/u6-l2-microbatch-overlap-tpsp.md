# microbatch overlap 与 TPSP 混合并行

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 **TPSP 混合并行**（Tensor Parallel + Sequence Parallel 混合）与纯张量并行的区别，理解它如何把"大块 all-reduce"拆成"小块 all-gather / reduce-scatter"，从而让通信变得"可重叠"。
- 掌握 **microbatch overlap** 的整体框架：一个 batch 是如何被切成两个 microbatch、各自建一个 `InferStateInfo`、再交错跑的，以及它为什么强制依赖 `--enable_tpsp_mix_mode`。
- 看懂 **计算通信重叠（overlap hook 折叠）** 的核心套路：把异步通信返回的"等待句柄"挂成 hook，用另一个 microbatch 的计算去填这段等待时间，在 MoE（DeepEP dispatch/combine）场景下尤其收益明显。
- 能够在 `basemodel.py` 里对比 `_prefill` 与 `microbatch_overlap_prefill`，并解释两个 `infer_state` 交错执行如何隐藏延迟。

## 2. 前置知识

本讲是 advanced 阶段内容，建议你已经学完：

- **u3-l2 prefill 与 decode 推理主流程**：知道 `forward → _prefill/_decode → _context_forward/_token_forward` 的分发，以及 `ModelInput / InferStateInfo / ModelOutput` 数据流。
- **u3-l4 权重加载与张量并行切分**：知道 TP（张量并行）下 `q/kv/gate/up` 沿输出维切（列并行）、`o/down` 沿输入维切（行并行，结果需要 all-reduce 求和）。
- **u6-l1 CUDA Graph 捕获与重放**：知道"地址不变、内容可变"的重放约束，以及 `capture_decode / replay`。
- **u5-l4 MoE 模型推理**：知道 MoE 层有"路由 + 专家计算"两段，EP（专家并行）下专家通信由 DeepEP 的 dispatch/combine 完成。

下面用通俗语言补几个本讲要用到的概念：

- **张量并行（TP）的通信代价**：行并行矩阵乘（如 `o_proj`、`down_proj`）每个 rank 只算一部分，必须做一次 **all-reduce** 把各 rank 的部分和加起来。all-reduce 的通信量正比于"隐藏维大小 × token 数"，在 decode（每 token 一次）时是高频小通信，在长 prefill 时是大通信。
- **序列并行（Sequence Parallel, SP）**：把一层的 token 维也切开，每个 rank 只持有 `1/N` 的 token。层内计算各算各的，但在"需要完整 token 视图"的地方（层首、层尾）用 **all-gather**（把切片拼回完整）和 **reduce-scatter**（求和后再切片）替代 all-reduce。SP 的妙处在于：通信量与 TP 切分数成正比地变小，且 all-gather / reduce-scatter 天然更"可拆"。
- **NCCL communicator（通信域）**：GPU 之间做集合通信要建一个"通信域"。同一个通信域上的集合操作会**串行**排队；要让两组集合通信**并发**跑在同一批 GPU 上，就需要**两个独立的通信域**。这是本讲"两个 microbatch 通信能重叠"的硬件基础。
- **CUDA Stream（流）**：GPU 上的任务队列。同一个流里的 kernel 串行执行；不同流的 kernel 可以并发（受限于 SM 资源）。把通信和计算放到能并发的位置，就能"重叠"它们。
- **异步通信 + 等待句柄（handle/hook）**：一次异步集合通信发起后立刻返回一个"句柄"，真正的等待（拿到结果）发生在你调用这个句柄时。把"调用句柄"的动作推迟、用别的计算塞进这段空隙，就是重叠的本质。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `lightllm/common/basemodel/basemodel.py` | **核心**。定义 `_prefill/_decode`、`microbatch_overlap_prefill/decode`、`_overlap_tpsp_context_forward/_overlap_tpsp_token_forward`，以及 TPSP 相关的 `enable_tpsp_mix_mode` 读取、`graph_max_batch_size` 减半逻辑。 |
| `lightllm/common/basemodel/layer_infer/base_layer_infer.py` | 提供 TPSP 三个通信原语 `_tpsp_sp_split / _tpsp_allgather / _tpsp_reduce`，以及层级的 `overlap_tpsp_context_forward / overlap_tpsp_token_forward` 抽象接口（模型子类实现）。 |
| `lightllm/common/basemodel/cuda_graph.py` | overlap 专用的 CUDA Graph 捕获/重放：`_capture_decode_overlap / _replay_overlap / warmup_overlap`，同时持有两个 `infer_state`。 |
| `lightllm/common/basemodel/infer_struct.py` | `call_overlap_hook`：调用绑定在 `InferStateInfo` 上的"折叠等待"函数。 |
| `lightllm/server/router/model_infer/mode_backend/base_backend.py` | 启动期按 overlap 开关把通信域数 `group_size` 设为 2，并据此选择 `prefill_overlap/decode_overlap` 方法。 |
| `lightllm/server/router/model_infer/mode_backend/dp_backend/impl.py` | overlap 调用入口：把 batch 一分为二、在专属 overlap stream 上调 `microbatch_overlap_prefill/decode`、合并两份输出。 |
| `lightllm/server/router/model_infer/mode_backend/generic_padded_pre_process.py` | `padded_overlap_prepare_prefill_inputs / padded_overlap_prepare_decode_inputs`：把一组请求切成两个 microbatch 并各自 padding。 |
| `lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py` | MoE 层 overlap 的"折叠"样板：用 DeepEP 的 `low_latency_dispatch/combine` 返回的 hook，把两个 microbatch 的专家通信与注意力/FFN 计算交错。 |
| `lightllm/models/llama/layer_infer/transformer_layer_infer.py` | 稠密层 overlap 的最简实现：两个 microbatch 顺序各跑一遍 `context_forward/token_forward`。 |
| `lightllm/distributed/communication_op.py` | `dist_group_manager.create_groups / get_group`：按 `group_size` 建立多个独立通信域。 |
| `lightllm/server/api_cli.py` | 三个开关参数 `--enable_tpsp_mix_mode / --enable_prefill_microbatch_overlap / --enable_decode_microbatch_overlap` 的定义。 |

## 4. 核心概念与源码讲解

### 4.1 TPSP 混合并行：microbatch overlap 的并行地基

#### 4.1.1 概念说明

先回忆纯 TP：行并行层（`o_proj`、`down_proj`）每个 rank 只拿到"部分和"，必须在层尾做一次 **all-reduce** 把所有 rank 的部分和加起来，才能得到正确的输出。问题是：

- 这是一次**整层 token**都要参与的同步通信，体积大、且必须等所有 rank 到齐，**很难被拆开重叠**。
- 它把"通信"和"计算"硬性串成两段：算完一层 → all-reduce → 算下一层。

**TPSP 混合（TP + SP）** 的思路是：在 TP 的基础上，把一层的 **token 维也按 TP 切开**（序列并行），层内每个 rank 只处理自己那 `1/N` 的 token。这样：

- 层与层之间，用 **all-gather**（把切片拼成完整 token，用于需要全局视图的算子）和 **reduce-scatter**（求和后再切成 `1/N`）替代一次性的 all-reduce。
- 通信量从"正比于隐藏维 × 全部 token"降到"正比于隐藏维 × `1/N` token"，**更小、更细粒度**，因而更容易与计算重叠。

一句话总结：**TPSP 把"又大又必须同步"的 all-reduce，换成"又小又能拆"的 all-gather / reduce-scatter，让通信变得可以被藏进计算里。** 这是后续 microbatch overlap 能真正重叠通信与计算的并行前提。

注意：是否启用 TPSP 是一个**运行时**开关，由命令行 `--enable_tpsp_mix_mode` 控制，代码里到处用 `get_env_start_args().enable_tpsp_mix_mode` 现场判断（参见 [basemodel.py:94](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L94)）。不开它时，模型退化为普通 TP（用 all-reduce）。

#### 4.1.2 核心流程

一次 TPSP 下的 prefill/decode 前向，通信点从"层尾一次 all-reduce"变成"层首/层尾两次细粒度集合通信"：

```text
输入 input_embs (完整 token)
   │
   ├── _tpsp_sp_split  : 按 sp_rank 把 token 切成 1/N，复制/重排成 SP 形状
   │                     (每个 rank 只持有自己那一份)
   │
   ├── for 每一层 transformer:
   │      layer.context_forward / token_forward   ← 层内计算，各 rank 独立处理 1/N token
   │
   ├── _tpsp_allgather : 把各 rank 的 1/N token 拼回完整 (用于 post 层的 lm_head)
   │
   └── post 层出 logits
```

关键替换关系：

| 位置 | 普通 TP | TPSP 混合 |
| --- | --- | --- |
| 层内中间结果汇总 | 行并行层尾 `all-reduce` | 不做，各 rank 各算各的 |
| 进入层（切 token） | 不切 | `_tpsp_sp_split`（all-gather 友好的重排） |
| 离开层（拼回 / 求和） | — | `_tpsp_allgather`（拼回完整）/ `_tpsp_reduce`（求和后 reduce-scatter） |

#### 4.1.3 源码精读

**通信原语三件套** 在 [base_layer_infer.py:53-98](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/base_layer_infer.py#L53-L98)，三者都先判断 `self.tp_world_size_ > 1 and enable_tpsp_mix_mode`，不满足就直接原样返回（兼容普通 TP / 单卡）：

```python
# lightllm/common/basemodel/layer_infer/base_layer_infer.py:53-61
def _tpsp_allgather(self, input, infer_state):
    if self.tp_world_size_ > 1 and get_env_start_args().enable_tpsp_mix_mode:
        sp_token_num, hidden_dim = input.shape
        gather_input = self.alloc_tensor((sp_token_num * self.tp_world_size_, hidden_dim), ...)
        all_gather_into_tensor(gather_input, input, group=infer_state.dist_group, async_op=False)
        return gather_input
    return input
```

- `_tpsp_sp_split`（[base_layer_infer.py:85-98](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/base_layer_infer.py#L85-L98)）：调用 `sp_pad_copy` 按 `sp_rank/sp_world_size` 把输入重排成 SP 形状（注释举例：`[16,1024]` 在 `tp=4` 下变成 `[4,1024]`）。
- `_tpsp_reduce`（[base_layer_infer.py:63-83](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/base_layer_infer.py#L63-L83)）：TPSP 开时做 **reduce-scatter**（求和并切成 `1/N`），否则退化为普通 **all-reduce**。注意 `dist_group` 是从 `infer_state` 拿的——这点在 4.2 节会变得很关键。

**前向里的调用点** 在 [basemodel.py:655-716](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L655-L716) 的 `_context_forward`：

```python
# basemodel.py:664
input_embs = self.pre_infer._tpsp_sp_split(input=input_embs, infer_state=infer_state)   # 进层前切 token
...
input_embs = self.post_infer._tpsp_allgather(input=input_embs, infer_state=infer_state) # 出层后拼回
```

`_token_forward`（[basemodel.py:718-745](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L718-L745)）结构对称：层首 `_tpsp_sp_split`、层尾 `_tpsp_allgather`。

**通信域来自 infer_state**：`_create_inferstate` 里写明了 `infer_state.dist_group = dist_group_manager.get_group(microbatch_index)`（[basemodel.py:386](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L386)）。普通模式下只有 1 个通信域；overlap 模式下会有 2 个，分别给两个 microbatch 用（详见 4.2）。

#### 4.1.4 代码实践

1. **实践目标**：看清 TPSP 三件套"何时切、何时拼、通信域从哪来"。
2. **操作步骤**：
   - 打开 [base_layer_infer.py:53-98](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/base_layer_infer.py#L53-L98)，分别列出 `_tpsp_sp_split / _tpsp_allgather / _tpsp_reduce` 在"开 TPSP"与"不开 TPSP"两种情况下的返回行为。
   - 打开 [basemodel.py:664](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L664) 与 [basemodel.py:723](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L723)，确认 prefill 与 decode 都在层首 `sp_split`、层尾 `allgather`。
3. **需要观察的现象**：三个原语都只在 `tp_world_size_ > 1` 且 `enable_tpsp_mix_mode` 为真时才真正通信，否则是"透传"——这意味着同一份代码兼容单卡、纯 TP、TPSP 三种模式。
4. **预期结果**：你能用一句话说清"all-reduce 被哪两步替代、各自发生在层的哪一头"。
5. 运行结果：**待本地验证**（本实践为源码阅读型，不依赖实际启动）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_tpsp_reduce` 在不开 TPSP 时退化为 `all_reduce`，而不是 reduce-scatter？
**答案**：不开 TPSP 时 token 维没有切分，每个 rank 持有完整 token 的部分和，必须用 all-reduce 把"完整 token 维上的部分和"加全；reduce-scatter 会顺便切 token 维，但此时根本没有 SP 切分可言，切了反而丢失数据。只有在 TPSP 下 token 维本来就是切开的，reduce-scatter 才等价于"求和后维持 SP 形状"。

**练习 2**：`_tpsp_allgather` 用的是 `infer_state.dist_group` 而不是某个全局 group，这样做对本讲主题有什么好处？
**答案**：让每个 microbatch 走自己的通信域。两个 microbatch 用两个独立 communicator，集合通信才能在同一批 GPU 上并发排队，不被同一个 communicator 串行化——这正是 4.3 节"通信与通信重叠"的前提。

---

### 4.2 microbatch 重叠：一个 batch 拆成两个 infer_state 交错跑

#### 4.2.1 概念说明

TPSP 把通信变细、变可重叠，但单看一个 batch 的前向，通信仍然夹在计算之间"空转"。**microbatch overlap** 的做法更激进：**把一个 batch 一分为二，变成两个 microbatch（microbatch0、microbatch1），各自建一个独立的 `InferStateInfo`，让它们交错执行**——当 microbatch0 在等通信时，GPU 可以去算 microbatch1 的计算，反之亦然。理想情况下，通信时间被"藏"进了对方的计算时间里，总耗时接近 `max(计算, 通信)` 而非 `计算 + 通信`。

它有三个不可忽视的约束（都写在源码里）：

1. **强制依赖 TPSP**：`microbatch_overlap_prefill` 与 `microbatch_overlap_decode` 函数体开头都 `assert self.args.enable_tpsp_mix_mode`。没有 TPSP 把通信拆细，microbatch overlap 无从重叠。
2. **需要两个独立通信域**：启动期把 `group_size` 设为 2，给两个 microbatch 各配一个 communicator。
3. **CUDA Graph 档位减半**：两个 microbatch 各占一半 batch，所以 `graph_max_batch_size` 要除以 2，保证总显存占用不变。

#### 4.2.2 核心流程

整体从"backend 拿到一组请求"到"拿到合并后的 logits"，分四步：

```text
① padded_overlap_prepare_prefill/decode_inputs
   一组请求 req_objs  ──对半切──>  req_objs_0 , req_objs_1
                                  各自 padding 到 micro_batch_size
                                  → model_input0, model_input1

② （在专属 overlap stream 上）model.microbatch_overlap_prefill(model_input0, model_input1)
   内部：建 infer_state0(用 group0) + infer_state1(用 group1)
         → _overlap_tpsp_context_forward / _overlap_tpsp_token_forward
         → 返回 (model_output0, model_output1)

③ 各自 unpad：_create_unpad_prefill/decode_model_output 还原到原始 token/请求数

④ 合并：把 logits0[0:req_num0] 与 logits1[0:req_num1] 拼成一份 logits，统一采样
```

注意第 ② 步把两个 microbatch 的输出在 GPU 上 `copy_` 拼接后再统一采样（[dp_backend/impl.py:256-259](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/dp_backend/impl.py#L256-L259)），对外仍是一个 batch 的语义。

#### 4.2.3 源码精读

**(a) 启动期准备：两个通信域 + 档位减半**

[base_backend.py:70-71](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L70-L71) 读两个开关；[base_backend.py:119-123](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L119-L123) 据此把通信域数设为 2：

```python
# base_backend.py:119-123
group_size = (
    2 if (self.args.enable_decode_microbatch_overlap or self.args.enable_prefill_microbatch_overlap) else 1
)
dist_group_manager.create_groups(group_size=group_size)  # set the default group
```

`create_groups(group_size=2)` 会建 2 个独立 `CustomProcessGroup`（[communication_op.py:118-127](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/communication_op.py#L118-L127)），随后 `get_group(0)/get_group(1)` 分别给两个 microbatch。`graph_max_batch_size` 在 overlap 下减半（[basemodel.py:79-83](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L79-L83)），因为两个 microbatch 各要一份 CUDA Graph 档位。

**(b) backend 选路：开 overlap 就走 overlap 版**

[dp_backend/impl.py:53-62](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/dp_backend/impl.py#L53-L62) 在 backend 初始化时按开关选方法：

```python
# dp_backend/impl.py:53-62
if self.enable_prefill_microbatch_overlap:
    self.prefill = self.prefill_overlap
else:
    self.prefill = self.prefill_normal
if self.enable_decode_microbatch_overlap:
    self.decode = self.decode_overlap
else:
    self.decode = self.decode_normal
```

`prefill_overlap` 在专属 stream 上调用 `microbatch_overlap_prefill`（[dp_backend/impl.py:250-251](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/dp_backend/impl.py#L250-L251)）：

```python
# dp_backend/impl.py:250-251
with torch.cuda.stream(g_infer_context.get_overlap_stream()):
    model_output0, model_output1 = self.model.microbatch_overlap_prefill(model_input0, model_input1)
```

`get_overlap_stream` 懒初始化一条独立 CUDA stream（[infer_batch.py:74-77](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/infer_batch.py#L74-L77)），让整段 overlap 计算与主流解耦。

**(c) 核心：`microbatch_overlap_prefill` 的"双 infer_state"结构**

对比 [basemodel.py:538-592](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L538-L592) 的 `_prefill`，overlap 版 [basemodel.py:747-836](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L747-L836) 关键差异如下：

```python
# basemodel.py:775
assert self.args.enable_tpsp_mix_mode            # ← 硬依赖 TPSP
...
# basemodel.py:790 & 803：注意第二个参数 0/1 就是 microbatch_index
infer_state0 = self._create_inferstate(model_input0, 0)   # → dist_group = get_group(0)
...
infer_state1 = self._create_inferstate(model_input1, 1)   # → dist_group = get_group(1)
...
# basemodel.py:819：一次前向同时处理两个 infer_state
model_output0, model_output1 = self._overlap_tpsp_context_forward(infer_state0, infer_state1=infer_state1)
```

普通 `_prefill` 只建一个 `infer_state`、走 `_context_forward`；overlap 版建**两个**、走 `_overlap_tpsp_context_forward`，并且把两份输出分别 unpad（[basemodel.py:821-830](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L821-L830)）。

`microbatch_overlap_decode`（[basemodel.py:838-936](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L838-L936)）结构对称，但 decode 有 CUDA Graph：捕获与重放都额外接收 `infer_state1`，一张图同时录两个 microbatch（详见 4.2 节末与 4.3）：

```python
# basemodel.py:894-904
if need_capture:
    model_output0, model_output1 = self.graph.capture_decode(
        self._overlap_tpsp_token_forward, infer_state0, infer_state1=infer_state1)
else:
    model_output0, model_output1 = self.graph.replay(infer_state0, infer_state1=infer_state1)
```

**(d) 一分为二的切法**

[padded_overlap_prepare_prefill_inputs](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/generic_padded_pre_process.py#L250-L257) 用 `triton.cdiv(len(req_objs), 2)` 把请求列表对半切（decode 版同理，[generic_padded_pre_process.py:233-248](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/generic_padded_pre_process.py#L233-L248)），各自 padding 到相同 `micro_batch_size`，保证两个 microbatch 形状一致（CUDA Graph 要求）。

#### 4.2.4 代码实践

1. **实践目标**：对比 `_prefill` 与 `microbatch_overlap_prefill`，说清后者如何用两个 `infer_state` 交错隐藏延迟。
2. **操作步骤**：
   - 打开 [basemodel.py:538-592](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L538-L592)（`_prefill`）与 [basemodel.py:747-836](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L747-L836)（`microbatch_overlap_prefill`），逐行做三处对照：
     - `infer_state` 数量：1 → 2；
     - `microbatch_index` 参数：无 → `0` 与 `1`（影响 `dist_group`）；
     - 前向入口：`_context_forward` → `_overlap_tpsp_context_forward`。
   - 打开 [basemodel.py:938-989](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L938-L989)（`_overlap_tpsp_context_forward`），注意它对每一层调用的是 `layer.overlap_tpsp_context_forward(input_embs, input_embs1, ...)`——**两个 microbatch 的数据同时传进同一层**，由该层决定如何交错（见 4.3）。
3. **需要观察的现象**：`microbatch_overlap_prefill` 在函数体开头的 `assert self.args.enable_tpsp_mix_mode`（[basemodel.py:775](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L775)），以及两个 `infer_state` 分别拿到 `get_group(0)` / `get_group(1)` 的通信域。
4. **预期结果**：你能解释"为什么必须开 TPSP"——overlap 依赖细粒度、可拆分的 all-gather/reduce-scatter；以及"为什么建两个通信域"——同一批 GPU 上要让两个 microbatch 的集合通信并发。
5. 运行结果：**待本地验证**。若手头有 DeepSeek-V3 权重与多卡环境，可分别用 `--enable_tpsp_mix_mode` 单独启动、再加 `--enable_decode_microbatch_overlap` 启动，对比 decode 阶段的 token/s（参考 `test/benchmark/static_inference/model_infer.py`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `microbatch_overlap_decode` 里有一句 `assert model_input0.batch_size == model_input1.batch_size`（[basemodel.py:857](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L857)）？
**答案**：decode 走 CUDA Graph 重放，图的形状（这里是 batch 维）在捕获时固定。两个 microbatch 必须形状一致，才能共用同一张 overlap 图、用 `copy_for_cuda_graph` 灌入新数据重放。`padded_overlap_prepare_decode_inputs` 正是把两者 padding 到同一个 `micro_batch_size` 来满足这个约束。

**练习 2**：开 `--enable_decode_microbatch_overlap` 后，`graph_max_batch_size` 为什么减半（[basemodel.py:79-83](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L79-L83)）？
**答案**：因为单个 batch 被切成两个 microbatch，每个 microbatch 都要按自己的 batch_size 录一张 CUDA Graph。档位减半后，两个 microbatch 各自的图档位上限之和 ≈ 原单图档位，总显存占用基本持平，避免 overlap 因双倍图显存而 OOM。

---

### 4.3 计算通信重叠：overlap hook 的"折叠"机制

#### 4.3.1 概念说明

4.2 把两个 microbatch 同时传进了一层。那么在一层**内部**，到底怎么"交错"？答案是 **overlap hook（折叠）机制**：

- 把一次**异步通信**发起后返回的"等待句柄"称为一个 **hook**，挂到对应的 `infer_state` 上。
- 在真正调用这个 hook（等待结果）之前，**先去做另一个 microbatch 的计算**。
- 等另一个 microbatch 算到也需要通信、或到层尾时，再回头调用 hook 完成等待。

这样 microbatch0 的通信时间，被 microbatch1 的计算"填满"了，反之亦然——这就是"折叠（fold）"：通信与计算在时间轴上叠在一起，而不是串行排队。

这个机制对 **MoE + EP（DeepEP）** 收益尤其大：DeepEP 的 `low_latency_dispatch`（把 token 派发给持有专家的 rank）和 `low_latency_combine`（把专家结果汇总回来）本身就是带句柄的异步通信，天然适合折叠。

注意：稠密层（如 Llama）没有这种大块异步专家通信，它的 overlap 实现最朴素——两个 microbatch 在一层里**顺序各跑一遍** `context_forward/token_forward`（[llama/.../transformer_layer_infer.py:143-165](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L143-L165)），重叠主要靠"两个独立通信域 + 不同 microbatch 在 SM 上交错占用"被动实现；真正主动的"计算填通信"折叠发生在 MoE 层。

#### 4.3.2 核心流程

以 MoE 层的 token forward 为例，"折叠"的时间轴大致是（`mb0`=microbatch0，`mb1`=microbatch1）：

```text
mb0: attention + FFN_norm + gate  →  dispatch(异步) 返回 hook0
                                       (此时 mb0 的 dispatch 在通信，GPU 空闲)
mb1: attention + FFN_norm + gate  →  dispatch(异步) 返回 hook1
                                       ↑ 这段计算正好填进 mb0 的 dispatch 通信里
调用 hook0(): 等 mb0 dispatch 完成 → mb0 专家计算(masked_group_gemm)
调用 hook1(): 等 mb1 dispatch 完成 → mb1 专家计算
mb0: combine(异步) 返回 hook0'
mb1: 专家计算 ...
调用 hook0': 等 mb0 combine 完成 → mb0 残差加回
mb1: combine(异步) → 注册到层尾 hook，由 _overlap_tpsp_token_forward 统一 call
```

关键点：每一次"调用 hook"都是一个**显式的同步点**，但它被推迟到"另一个 microbatch 的计算"之后，于是同步等待的时间被计算覆盖。

层级入口在 [basemodel.py:991-1028](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L991-L1028) 的 `_overlap_tpsp_token_forward`，逐层调用 `layer.overlap_tpsp_token_forward(input_embs, input_embs1, infer_state, infer_state1, ...)`，循环结束后统一调用两个 infer_state 上的 hook 收尾（[basemodel.py:1004-1006](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L1004-L1006)）：

```python
# basemodel.py:999-1006
for i in range(self.layers_num):
    input_embs, input_embs1 = self.layers_infer[i].overlap_tpsp_token_forward(
        input_embs, input_embs1, infer_state, infer_state1, self.trans_layers_weight[i])

# 折叠模式调用完 infer_state 上的 hook 函数后，input_embs 和 input_embs1 才具备正确的运算数据。
infer_state.call_overlap_hook()
infer_state1.call_overlap_hook()
```

注释点破了核心：**折叠模式下，必须等层尾的 hook 跑完，`input_embs` 才有正确数据**——因为最后一个 microbatch 的 combine 结果是被推迟到 hook 里才加回残差的。

#### 4.3.3 源码精读

**(a) hook 的调用工具**

`call_overlap_hook` 是个快速调用器（[infer_struct.py:151-159](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/infer_struct.py#L151-L159)）：若 `self.hook` 不为空就执行它再置空，普通模式（没有 hook）什么都不做。这统一了"有/无 overlap"两种代码路径。

**(b) MoE 层的折叠样板（DeepSeek-V2/V3）**

[deepseek2/.../transformer_layer_infer.py:291-415](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py#L291-L415) 是最完整的折叠示例。关键节选：

```python
# deepseek2 transformer_layer_infer.py:325-334  —— mb0 先 dispatch，拿到异步 hook
(_0_recv_x, _0_masked_m, _0_topk_idx, _0_topk_weight, _0_handle, _0_hook
 ) = layer_weight.experts.low_latency_dispatch(_0_input1, _0_router_logits)
infer_state.hook = _0_hook              # 先不等，挂起来

# :336-350  —— 紧接着做 mb1 的 attention + gate（这段计算填进 mb0 的 dispatch 通信）
_1_input1 = self._att_norm(input_embdings1, infer_state1, layer_weight)
... _1_router_logits = layer_weight.moe_gate.mm(_1_input1.to(moe_gate_dtype))

# :351-354  —— mb1 准备就绪，回头等 mb0 的 dispatch 完成
if getattr(infer_state, "hook", None) is not None:
    infer_state.hook(); infer_state.hook = None
```

随后 mb0 进入专家计算 `masked_group_gemm`，而 mb1 的 dispatch hook 又被挂起、等 mb0 算完再处理（[deepseek2/.../transformer_layer_infer.py:377-398](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py#L377-L398)）。最后 mb1 的 combine 结果被包进 `_1_hook_post` 闭包挂到 `infer_state1.hook`（[deepseek2/.../transformer_layer_infer.py:401-413](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py#L401-L413)），由外层 `_overlap_tpsp_token_forward` 的 `call_overlap_hook` 在层尾触发。

> 说明：上面 `# :行号` 形式的标注是**示例代码**（节选并改写自源码以便阅读），完整逻辑以仓库原文件为准。

**(c) CUDA Graph 如何承载 overlap**

decode 的 overlap 还要满足 CUDA Graph"地址不变"的约束。`_capture_decode_overlap`（[cuda_graph.py:114-152](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/cuda_graph.py#L114-L152)）把**两个** `infer_state` 一起录进同一张图、共享 mempool，并把 `(graph_obj, infer_state, infer_state1, output, output1)` 五元组存表；`_replay_overlap`（[cuda_graph.py:177-193](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/cuda_graph.py#L177-L193)）则对两个 infer_state 各做一次 `copy_for_cuda_graph` 灌新数据后一次 `replay()`，实现"一张图重放两个 microbatch"。`warmup_overlap`（[cuda_graph.py:259-322](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/cuda_graph.py#L259-L322)）对每个档位构造两个 dummy micro_batch 并调 `microbatch_overlap_decode` 完成逐档捕获。

#### 4.3.4 代码实践

1. **实践目标**：理解"hook = 推迟的同步点"，看清 MoE 层如何用另一个 microbatch 的计算填通信。
2. **操作步骤**：
   - 打开 [deepseek2/.../transformer_layer_infer.py:291-415](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/deepseek2/layer_infer/transformer_layer_infer.py#L291-L415)，把所有 `infer_state.hook = ...`（挂起）和 `infer_state.hook()` / `call_overlap_hook()`（触发）的位置标出来。
   - 在每两个相邻的"挂起"与"触发"之间，标注 GPU 此时在算哪个 microbatch、在等哪个 microbatch的通信。
   - 对照 [basemodel.py:1004-1006](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L1004-L1006)，确认层尾还有一次 `call_overlap_hook` 兜底（处理被推迟到最后才 combine 的那个 microbatch）。
3. **需要观察的现象**：每出现一次 `dispatch/combine`，紧接着的几乎都是"另一个 microbatch 的 norm/attention/gate"，而不是立刻等待——这就是"计算填通信"的可见证据。
4. **预期结果**：你能画出一张两层时间轴草图，证明 mb0 的通信段与 mb1 的计算段在时间上重叠。
5. 运行结果：**待本地验证**。可用 `nsys`/`nsight` 抓 DeepSeek-V3 decode 的 timeline，开/关 `--enable_decode_microbatch_overlap` 对比 NCCL 通信区间与 GEMM 区间的重叠程度。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_overlap_tpsp_token_forward` 在所有层跑完后，还要再调一次 `call_overlap_hook`（[basemodel.py:1005-1006](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L1005-L1006)）？
**答案**：因为 DeepSeek 的折叠把"最后一个 microbatch 的 combine + 残差加回"包成了 `_1_hook_post` 挂到 `infer_state1.hook` 上，故意推迟执行以重叠。如果不在层尾（或整条 forward 尾巴）显式触发它，`input_embs1` 的最终值还没把最后一段 FFN 输出加回，是错的。注释"折叠模式调用完 hook 后 input_embs 才具备正确的运算数据"说的正是这一点。

**练习 2**：稠密 Llama 的 `overlap_tpsp_token_forward`（[llama/.../transformer_layer_infer.py:143-153](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L143-L153)）里没有任何 hook，它还能"重叠"吗？
**答案**：能，但属于被动重叠。它把两个 microbatch 顺序各跑一遍 `token_forward`，重叠来自两点：① 两个 microbatch 用两个独立通信域，各自的 all-gather/reduce-scatter 能在同一批 GPU 上并发排队；② 两个 microbatch 的 kernel 在 SM 上交替占用，通信间隙自然被另一方的计算填上。相比 MoE 的"主动挂起 hook"，稠密层的重叠幅度更依赖硬件调度，收益不如 MoE+EP 显著。

## 5. 综合实践

把三个最小模块串起来，做一次"开/关对比"的源码追踪 + 可选实测：

**任务**：选择 DeepSeek-V3（MoE）作为目标模型，回答下列问题，并（若有条件）实测验证。

1. **参数依赖链**：从 [api_cli.py:363-385](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L363-L385) 出发，列出三个开关 `--enable_tpsp_mix_mode / --enable_prefill_microbatch_overlap / --enable_decode_microbatch_overlap`，并指出"开 microbatch overlap 必须先开 TPSP"在源码哪一行被强制（提示：`microbatch_overlap_prefill/decode` 的 assert）。
2. **启动期变化**：开 overlap 后，[base_backend.py:119-123](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L119-L123) 的 `group_size` 由 1 变 2，[basemodel.py:79-83](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L79-L83) 的 `graph_max_batch_size` 减半——解释两者各自的必要性。
3. **一次 overlap prefill 的完整调用链**：画出从 `Router → backend.prefill_overlap → padded_overlap_prepare_prefill_inputs → microbatch_overlap_prefill → _overlap_tpsp_context_forward → 各层 overlap_tpsp_context_forward → call_overlap_hook` 的调用栈，并标注两个 microbatch 在哪一步分别拿到 `get_group(0)` / `get_group(1)`。
4. **（可选，待本地验证）实测**：用 `test/benchmark/static_inference/model_infer.py` 或 `test/start_scripts/single_node_tp.sh` 启动 DeepSeek-V3，分别测量：
   - 仅 `--enable_tpsp_mix_mode`；
   - 再加 `--enable_prefill_microbatch_overlap`；
   - 再加 `--enable_decode_microbatch_overlap`。
   
   记录 prefill / decode 的吞吐与延迟，观察每加一层 overlap 带来的变化，并尝试用本讲的时间轴模型解释。

> 注意：本实践不要求你修改任何源码，只需阅读、画图、（可选）实测。请勿修改仓库源文件。

## 6. 本讲小结

- **TPSP 混合并行**是地基：用层首 `_tpsp_sp_split` + 层尾 `_tpsp_allgather/_tpsp_reduce`（本质是 all-gather / reduce-scatter）替代纯 TP 的一次性 all-reduce，把通信变得"又小又能拆"，且三个原语都受 `enable_tpsp_mix_mode` 现场开关控制。
- **microbatch overlap** 把一个 batch 对半切成两个 microbatch，各建一个 `InferStateInfo`、各用一个独立通信域（`group_size=2`、`get_group(0/1)`），并强制依赖 TPSP；为补偿显存，`graph_max_batch_size` 减半。
- **overlap hook 折叠**是真正的"计算填通信"机制：异步通信返回的等待句柄被挂成 `infer_state.hook`，调用前先做另一个 microbatch 的计算；在 MoE+EP（DeepEP dispatch/combine）场景收益最大，稠密层则靠双通信域被动重叠。
- 层级入口是 `_overlap_tpsp_context_forward / _overlap_tpsp_token_forward`，逐层调 `layer.overlap_tpsp_*_forward` 同时处理两个 microbatch，层尾用 `call_overlap_hook` 兜底处理被推迟的 combine。
- decode 的 overlap 还与 CUDA Graph 共存：`_capture_decode_overlap / _replay_overlap` 把两个 infer_state 录进同一张图、共享 mempool，重放时各 `copy_for_cuda_graph` 灌新数据，遵守"地址不变、内容可变"。
- 三个特性层层递进：TPSP 让通信可重叠 → microbatch overlap 提供两个可交错的执行体 → hook 机制把它们在时间轴上折叠起来。

## 7. 下一步学习建议

- **u6-l3 FP8 KV Cache 量化**：继续性能优化主线，看 KV 显存如何被压缩，与本讲的"通信/计算重叠"是正交的两类优化，常组合使用。
- **u7-l1 PD 分离部署与 KV 迁移**：overlap 的 overlap stream 与双 infer_loop 思想，在 PD 分离的 KV 传输里也有呼应，可对照阅读。
- **u5-l4 / u5-l5 MoE 与 MLA**：本讲的折叠样板来自 DeepSeek MoE 层；若想完全看懂 `low_latency_dispatch/combine` 与专家并行的关系，建议回看 MoE 推理讲义。
- **源码延伸阅读**：`lightllm/distributed/communication_op.py`（`CustomProcessGroup` 与 symm_mem/flashinfer all-reduce 后端）、`lightllm/common/basemodel/prefill_cuda_graph.py`（prefill 侧的 overlap 捕获），它们是本讲未展开的相邻实现。
