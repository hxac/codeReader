# 多 GPU 与张量并行

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「张量并行（tensor parallelism）」在 MLC LLM 里是如何用**一个 `shard_strategy` 标签**声明出来的，以及它把权重按哪个维度、什么方式切开。
- 区分两条分片路径——**编译期 preprocs（现场切）**与**转换期 preshard（提前切）**——理解它们各自的产物形态、触发开关与共享底座。
- 看懂运行期 C++ 侧的 `multi_gpu_loader.cc` 如何在 disco 分布式会话里把权重 broadcast / scatter 到各张卡，以及 Python 侧 `__init__.py` 与 `cli/worker.py` 如何为 disco 拉起多进程 worker。
- 把 u4-l3（convert_weight 的 preshard）与 u9-l4（model lib 与 FunctionTable）两条线在本讲里收口成一条「声明 → 切分 → 跨卡分发」的完整链路。

## 2. 前置知识

本讲是 **advanced** 阶段，默认你已经读完下面两篇：

- **u4-l3 convert_weight 全流程与预分片**：你已经知道 `MLC_INTERNAL_PRESHARD_NUM` 环境变量、`apply_preshard`、`tvmjs` ndarray cache 的产物结构。
- **u9-l4 模型运行时与 FunctionTable**：你已经知道 model lib、`FunctionTable`、编译期↔运行期「同名契约」字符串、`use_disco` 标志。

本讲要用到但只需直觉理解的概念：

- **张量并行（Tensor Parallelism, TP）**：把单次矩阵乘里的权重沿某一维切成 N 份，分到 N 张卡上各算一部分，再用集合通信（all-reduce）合并。它是「把一个大模型塞进多张显存不够大的卡」的主流做法，对应 Megatron-LM 的 column/row parallel 思想。MLC 里由 `tensor_parallel_shards` 这个配置值控制。
- **disco**：TVM 自带的轻量分布式运行时（distributed runtime）。一个 disco **Session** 管着多个 **worker** 进程，提供 `BroadcastFromWorker0` / `ScatterFromWorker0` / all-reduce 等集合通信原语，让一份代码可以同时跑在多卡上。
- **TIR / PrimFunc**：TVM 的底层张量表达式 IR。本讲里它被用来写「把一个权重张量切成 N 份」的小函数。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/mlc_llm/support/tensor_parallel.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/tensor_parallel.py) | 定义分片策略 `ShardSingleDim`，能生成「切权重」的 TIR 函数与分片元信息；附 `shard_bias` 上下文管理器。 |
| [python/mlc_llm/model/llama/llama_model.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py) | 在模型定义里给 `qkv_proj / o_proj / gate_up_proj / down_proj` 打上 `shard_strategy` 标签的典型范例。 |
| [python/mlc_llm/support/preshard.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/preshard.py) | 转换期 preshard：把 `ShardSingleDim` 包成 Relax 函数、编译成 VM，在 `convert_weight` 阶段真切权重落盘。 |
| [python/mlc_llm/interface/convert_weight.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py) | 读 `MLC_INTERNAL_PRESHARD_NUM`，在转换流程里挂上 `preshard_funcs`。 |
| [python/mlc_llm/interface/compile.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py) | 编译期 preprocs：把 `shard_strategy` 翻译成运行期切分配方（`gen_shard_info`）与切分 TIR（`gen_tir`）。 |
| [cpp/multi_gpu/multi_gpu_loader.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/multi_gpu/multi_gpu_loader.cc) | 运行期多 GPU 权重加载器：`LoadMultiGPU`（现场切）与 `LoadMultiGPUPresharded`（读已切好的）。 |
| [cpp/serve/function_table.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc) | `FunctionTable::LoadParams` 按是否 preshard 选加载器；`Init` 建立 disco 会话视图。 |
| [cpp/serve/engine.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc) | `CreateDiscoSession` 按卡数决定是否拉起 ProcessSession / SocketSession，并 `InitCCL`。 |
| [python/mlc_llm/__init__.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/__init__.py) | 注册 `runtime.disco.create_socket_session_local_workers`，给 disco 提供拉本地 worker 进程的 Python 回调。 |
| [python/mlc_llm/cli/worker.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/worker.py) | 每个 worker 子进程的入口：解析 5 个命令行参数，调 TVM 的 `runtime.disco.WorkerProcess`。 |

## 4. 核心概念与源码讲解

### 4.1 shard_strategy：用一个标签声明张量并行分片

#### 4.1.1 概念说明

MLC LLM 的张量并行有一个非常优雅的设计：**模型代码本身不写任何「切分」逻辑**。模型作者只用 Relax nn（见 u3-l2）按「**单张卡上的形状**」把模型写一遍，然后在每个需要切分的权重上挂一个名叫 `shard_strategy` 的属性标签。这个标签告诉编译器/加载器：「这个权重在多卡时要沿哪一维、按什么分段切成 N 份」。

这样做的好处是：

- **同一份模型代码单卡/多卡通用**：`tensor_parallel_shards=1` 时标签被忽略，模型就是普通单卡模型。
- **切分知识集中在一处**：分片规则不是散落在各层 if 分支里，而是一个数据类 `ShardSingleDim`，可被 convert_weight、compile、loader 三处共用。

`ShardSingleDim` 表达的是「沿单一维度切分」，这是目前 TP 的主流形态（Megatron 的 column/row parallel 都是沿单一维切）。

#### 4.1.2 核心流程

一个权重从「声明分片」到「真正被切」要经过三步，分别由三个最小模块承担：

1. **声明（本模块 4.1）**：模型构造时给 `param.attrs["shard_strategy"]` 赋一个 `ShardSingleDim(name, dim, segs)`。
2. **翻译（4.2）**：`ShardSingleDim` 提供两个方法：
   - `gen_shard_info(shards, weight)` → 产出一份「运行期切分配方」（函数名、输入/输出形状、dtype）。
   - `gen_tir(shards, weight)` → 产出一个 TIR `PrimFunc`，真正执行切分计算。
3. **执行（4.3）**：配方和 TIR 被送进编译产物或加载器，在运行期由 disco 把切好的片分发到各卡。

`ShardSingleDim.gen_tir` 的关键技巧是 **reshape + transpose**。它不是简单地把某一维等长劈成 N 段，而是先把该维「重排成 `(shards, sub_seg)` 再把 `shards` 轴翻到最前」，从而能正确处理「**分段**」情形（如 QKV 三段、gate/up 两段），保证每张卡拿到的切片在每一段里都是连续且按比例的。

设权重沿 `dim` 的总长为 `D`，shards 数为 `N`，则每张卡在该维上分到 `D/N`。对于有 `segs=[s_0, s_1, ...]`（如 QKV 的 `[q, k, v]`）的层，输入权重沿 `dim` 的布局是「**每段连续排布、每段长度为 `s_j * N`**」，输出则是 N 个「**每段长度为 `s_j`**」的切片。可以形式化为：

\[
\text{out}[n,\ *,\ i \in \text{seg}_j] = \text{in}[\ *,\ i + \sum_{k<j} s_k \cdot N + n \cdot s_j\text{-offset}]
\]

核心读取语句就是 `w[idx[self.dim] + offset]`，其中 `offset` 按 segment 累加。

#### 4.1.3 源码精读

`ShardSingleDim` 是个 dataclass，只有三个字段：函数名 `name`、切分维 `dim`、可选分段 `segs`。

[python/mlc_llm/support/tensor_parallel.py:11-34](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/tensor_parallel.py#L11-L34) —— 定义 `ShardSingleDim`，`segs=None` 表示整维一刀切；给了 `segs`（如 `[q, k, v]`）则按段切。

`gen_tir` 是切分算法的核心。它先按 `_compute_in_shape` 算出**输入**形状——把 `dim` 维扩大 `shards` 倍（即还原成「未切分的完整权重」），再对每段做 `reshape 成 (shards, sub_seg)` + `transpose` 把 `shards` 翻到 `dim` 轴，最后 `concatenate`：

[python/mlc_llm/support/tensor_parallel.py:36-83](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/tensor_parallel.py#L36-L83) —— `gen_tir`。注意第 61-66 行的核心读取 `w[idx[:dim], idx[dim] + offset, idx[dim+1:]]`，第 70-78 行的 `reshape → transpose`，第 81 行把各段 `concatenate` 回 `1+dim` 轴。

> 直觉理解：对没有 `segs` 的层（如 `o_proj`、`down_proj`），这等价于「沿 `dim` 连续块切 N 份」；对有 `segs` 的层（如 QKV、gate_up），它保证 Q/K/V（或 gate/up）**各自独立地被均分**，于是每张卡拿到的仍是「一组完整的 Q 头 + 对应的 K/V 头」，GQA 比例不会被打乱。

`gen_shard_info` 则产出一分**轻量配方**（不切，只描述怎么切），供编译期 preprocs 路径使用：

[python/mlc_llm/support/tensor_parallel.py:85-97](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/tensor_parallel.py#L85-L97) —— `gen_shard_info` 返回 `func_name / in_shape / out_shape / out_dtype`；`_compute_in_shape` 把 `dim` 维放大 `shards` 倍，正是「完整未切分」形状。

那么模型里怎么用？以 Llama 为例，`LlamaDecoderLayer._set_tp` 给四个权重打标签：

[python/mlc_llm/model/llama/llama_model.py:181-202](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L181-L202) —— `_set_tp`：`qkv_proj` 和 `gate_up_proj` 用 `dim=0`（输出特征维，附带 `segs`），`o_proj` 和 `down_proj` 用 `dim=1`（输入特征维）。

| 权重 | `dim` | `segs` | Megatron 类比 | 含义 |
| --- | --- | --- | --- | --- |
| `qkv_proj` | 0 | `[q,k,v]` | column-parallel | 切输出头，每卡持有一部分 Q/K/V 头 |
| `gate_up_proj` | 0 | `[i,i]` | column-parallel | 切 FFN 中间维，gate/up 各自均分 |
| `o_proj` | 1 | 无 | row-parallel | 切输入维（注意力输出投影），结果需 all-reduce |
| `down_proj` | 1 | 无 | row-parallel | 切输入维（FFN 输出投影），结果需 all-reduce |

`dim=0`（输出维）的层各卡独立算出自己那份输出，无需通信即可继续；`dim=1`（输入维）的层各卡只算出「部分和」，必须 all-reduce 才能得到最终结果——这个 all-reduce 由 disco 会话在初始化时挂上（见 4.3.3 的 `InitCCL`）。

此外，`shard_bias` 是个小工具：把 bias 沿张量并行的维除以 `shards`，避免重复计算时数值翻倍：

[python/mlc_llm/support/tensor_parallel.py:100-118](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/tensor_parallel.py#L100-L118) —— `shard_bias` 上下文管理器，`tensor_parallel_shards>1` 时临时把 `bias /= shards`，退出后还原。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**。

1. **实践目标**：理解 Llama 模型如何用 4 个 `ShardSingleDim` 标签完整描述 TP 切分。
2. **操作步骤**：
   - 打开 `python/mlc_llm/model/llama/llama_model.py`，定位 `_set_tp`（约 181-202 行）。
   - 再打开另一个模型，例如 `python/mlc_llm/model/gpt_neox/gpt_neox_model.py`（搜索 `ShardSingleDim`），对比它的切分方式。
3. **需要观察的现象**：注意 `qkv_proj` 用了 `segs=[q, k, v]` 而 `o_proj` 没有 `segs`；注意 GQA 模型里 `q = num_q_heads * head_dim` 远大于 `k = v = num_kv_heads * head_dim`，因此 `segs` 的三个段长度不等。
4. **预期结果**：你能用一句话说清「为什么 QKV 必须用 `segs` 三段切，而 `o_proj` 不需要」。
5. **待本地验证**：若你本地装好了 mlc_llm，可写一段示例代码（非项目原有，标注「示例代码」）打印某层权重的 `attrs`：

```python
# 示例代码：仅用于观察 shard_strategy 标签，不修改源码
from mlc_llm.model.llama.llama_model import LlamaForCausalLM, LlamaConfig
# 需要一份真实 config.json 才能实例化；此处仅示意
# cfg = LlamaConfig.from_file("path/to/config.json")
# model = LlamaForCausalLM(cfg)
# print(model.model.layers[0].self_attn.qkv_proj.weight.attrs.get("shard_strategy"))
```

#### 4.1.5 小练习与答案

**练习 1**：若 `tensor_parallel_shards=1`，`shard_strategy` 标签还有作用吗？

**答案**：没有。无论是 compile 期（`_apply_preproc` 的 `if shard_strategy is not None and model_config.tensor_parallel_shards > 1`）还是 convert_weight 的 preshard（受 `MLC_INTERNAL_PRESHARD_NUM` 控制），只有 shards>1 才会消费这个标签。标签本身只是个挂在 `attrs` 上的数据，单卡时被忽略。

**练习 2**：为什么 `o_proj` 用 `dim=1` 而不是 `dim=0`？

**答案**：`o_proj` 的权重形状是 `[hidden_size, intermediate)`（输出=hidden，输入=注意力头的拼接）。注意力计算时每张卡只有自己那部分头，因此要沿**输入维 dim=1** 切，让每张卡只接收自己那部分输入做矩阵乘，再把各卡的部分和 all-reduce。沿 dim=0 切会导致每张卡都需要完整的输入，违背 TP 初衷。

---

### 4.2 两条分片路径：编译期 preprocs 与转换期 preshard

#### 4.2.1 概念说明

`ShardSingleDim` 只是「描述怎么切」。真正「执行切」有**两条互斥的路径**，这正是 u4-l3 提到的「路 A / 路 B」的底层来源：

- **路 A：编译期 preprocs（运行期现场切，默认）**。磁盘上存的是**完整未切分**的权重；编译期把 `ShardSingleDim` 翻译成一份**配方 + TIR 函数**塞进 model lib 的 metadata 与 IRModule；运行期加载器 `LoadMultiGPU` 读完整权重，调用配方里的 TIR 现场切成 N 份。优点：权重产物与单卡通用、可换 shards；缺点：每次启动都要切一遍。
- **路 B：转换期 preshard（提前切好）**。在 `convert_weight` 阶段（由 `MLC_INTERNAL_PRESHARD_NUM=N` 触发）就把权重切成 N 份分别落盘（命名 `param_shard-0 … param_shard-{N-1}`）；运行期加载器 `LoadMultiGPUPresharded` 直接各取一份。优点：启动快、运行期零额外切分开销；缺点：产物绑定卡数 N。

两条路径**共享同一个 `ShardSingleDim`** 作为底座——这是 MLC 设计的关键一致性：`gen_shard_info`/`gen_tir` 同时服务 preprocs，`gen_tir` 也服务 preshard。

#### 4.2.2 核心流程

两条路径的差异可以用一张表概括（详见 u4-l3 的更细粒度版本）：

| 维度 | 路 A（preprocs / 现场切） | 路 B（preshard / 提前切） |
| --- | --- | --- |
| 触发开关 | `tensor_parallel_shards>1`（编译期自动） | `MLC_INTERNAL_PRESHARD_NUM=N` 环境变量 |
| 切分发生的阶段 | 运行期（每次启动引擎） | 转换期（`convert_weight` 一次性） |
| 磁盘产物 | 完整权重 `params_shard_*.bin` | 已切分 `param_shard-{i}` 多份 |
| 运行期加载器 | `mlc.multi_gpu.LoadMultiGPU` | `mlc.multi_gpu.LoadMultiGPUPresharded` |
| 切分函数来源 | `gen_shard_info`（配方）+ `gen_tir`（TIR）→ 进 model lib | `gen_tir` → 包成 Relax → 编译成 VM → 转换期调用 |
| 卡数可变性 | 可换（重新现场切） | 绑定 N |

> 关键约束：转换期与运行期必须读**同一个** `tensor_parallel_shards` / `MLC_INTERNAL_PRESHARD_NUM`，否则切数不匹配会直接报错。

#### 4.2.3 源码精读

**路 A 的编译期翻译**在 `compile.py` 的 `_apply_preproc_to_params_and_check_pipeline` 里：

[python/mlc_llm/interface/compile.py:62-95](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L62-L95) —— 遍历每个参数：若有 `shard_strategy` 且 `tensor_parallel_shards>1`，就用 `gen_shard_info` 往 `preprocs` 列表追加一份配方（行 71-76），用 `gen_tir` 往 `extra_tirs` 追加切分 TIR（行 77-81），最后把 `preprocs` 写回 `param.attrs`（行 82）。

这段 `preprocs` 最终会随 model lib 的 metadata 一起被 C++ 侧读到（见 4.3.3 的 `Param::Preproc`）。`gen_shard_info` 产出的 `out_shape[0] == num_shards` 就是「第一维是卡数」的契约，运行期 loader 据此 `ScatterFromWorker0`。

**路 B 的转换期切分**在 `support/preshard.py`。`apply_preshard` 遍历所有带 `shard_strategy` 的参数，为每个不重名的策略建一个 Relax 切分函数，编译成 VM，再把 VM 句柄回填：

[python/mlc_llm/support/preshard.py:70-123](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/preshard.py#L70-L123) —— `apply_preshard`：行 101-102 把一个参数名展开成 N 个 `param_shard-{i}`；行 104-107 用策略名去重；行 118-122 把 BlockBuilder 编出的 VM 句柄替换掉策略名字符串。

其中 `_create_shard_func` 把 `gen_tir` 包成一个 Relax 函数，流程是 `call_tir(切) → split(N) → squeeze(0)`，正好把 `gen_tir` 输出的 `[N, *per_shard]` 拆成 N 个独立张量：

[python/mlc_llm/support/preshard.py:20-51](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/preshard.py#L20-L51) —— `_create_shard_func`：行 32 把 `dim` 维放大 N 倍得到输入形状（还原完整权重）；行 37-43 `call_tir` 调 `gen_tir` 出来的 TIR；行 44-49 `split` + `squeeze` 产出 N 个切片。

`convert_weight.py` 读环境变量决定是否走 preshard：

[python/mlc_llm/interface/convert_weight.py:102-125](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L102-L125) —— 行 102 读 `MLC_INTERNAL_PRESHARD_NUM`；行 111-112 用它覆盖 `model_config.tensor_parallel_shards`（保证转换期与运行期切数一致）；行 122-123 调 `apply_preshard` 拿到 `preshard_funcs`，否则置 `None`。

随后 `preshard_funcs` 被传进 loader，让加载器在 `yield` 每个参数前调用对应的切分函数（见 u4-l3）：

[python/mlc_llm/interface/convert_weight.py:174](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L174) —— `loader.load(device=args.device, preshard_funcs=preshard_funcs)`，把切分句柄注入流式加载。

#### 4.2.4 代码实践

这是一个**源码阅读 + 配置观察型实践**。

1. **实践目标**：验证「两条路径共享 `ShardSingleDim.gen_tir`」这一结论。
2. **操作步骤**：
   - 在 `tensor_parallel.py` 里找到 `gen_tir`（36-83 行）。
   - 用 grep / 编辑器搜索 `gen_tir` 的全部调用点：应只在 `preshard.py:_create_shard_func`（行 23）与 `compile.py:_apply_preproc`（行 78）出现。
   - 再搜索 `gen_shard_info` 的调用点：应只在 `compile.py`（行 72）出现。
3. **需要观察的现象**：preshard（路 B）只用 `gen_tir`、不用 `gen_shard_info`；preprocs（路 A）两者都用。
4. **预期结果**：你能解释「为什么路 B 不需要 `gen_shard_info`」——因为路 B 在转换期就直接拿到切好的张量落盘，运行期无需配方；而路 A 要把「怎么切」写进 metadata，让运行期 loader 照配方切。
5. **待本地验证**：可在一个小模型上分别用两种方式 `convert_weight`，对比产出的 `tensor-cache.json` 里参数名的差异（路 A 是 `param`，路 B 是 `param_shard-0/1/...`）。

#### 4.2.5 小练习与答案

**练习 1**：如果转换期设了 `MLC_INTERNAL_PRESHARD_NUM=2`，但运行期不设这个环境变量，会发生什么？

**答案**：运行期 `function_table.cc` 里 `getenv("MLC_INTERNAL_PRESHARD_NUM") == nullptr` 为真，于是选 `LoadMultiGPU`（路 A，现场切）。但磁盘上的权重是 preshard 产物（`param_shard-{i}`），路 A 加载器按完整权重名找参数会找不到，报错。所以两端必须一致。

**练习 2**：`apply_preshard` 里为什么要用 `shard_func_names` 这个集合去重？

**答案**：一个策略名（如 `_shard_qkv`）会被同一层以及多层重复使用（每个 decoder layer 的 qkv 都叫 `_shard_qkv`）。TIR 函数只与「策略 + 形状」相关，按名字去重能避免为每个参数都生成并编译一份相同的切分函数，节省编译时间。

---

### 4.3 运行期多 GPU 加载与 disco 分布式会话

#### 4.3.1 概念说明

切分配方（路 A）或切好的片（路 B）最终都要在**运行期**落到各张卡上。这需要两个角色配合：

- **多 GPU 加载器**（`multi_gpu_loader.cc`）：在 disco 会话的每个 worker 进程里跑，负责「读磁盘 → 必要时切 → 用集合通信分发到本组各 worker」。
- **disco 会话**（Session）：把多个 worker 进程组织起来，提供 `BroadcastFromWorker0` / `ScatterFromWorker0` / all-reduce 等原语。MLC 在引擎初始化时按 `num_shards`（与可选的 pipeline stages）拉起对应数量的 worker 进程，并初始化 CCL（NCCL/RCCL）。

这里的「worker / group」概念来自 disco：一个 disco Session 有 `num_workers` 个 worker，被分成 `num_groups` 个组。对纯 TP（无流水线），`num_groups=1`、`group_size=num_shards`；引入流水线并行时 `num_groups=num_stages`（见 u8-l4 的 `PipelineParallelRewrite`）。

#### 4.3.2 核心流程

引擎启动时的多 GPU 装配顺序（在 `EngineImpl` 构造时）：

1. `CreateDiscoSession` 从 model lib 读出 `tensor_parallel_shards` 与 `pipeline_parallel_stages`，算出 `num_workers = num_shards * num_stages`；若 `num_workers>1`，按是否跨节点选 `ProcessSession`（单机多卡）或 `SocketSession`（多节点流水线），再 `InitCCL`。
2. `FunctionTable::Init` 收到该 session，置 `use_disco=true`，通过 session 把 model lib 加载成「disco 远程模块」`disco_mod`，所有函数调用都经 session 广播到各 worker。
3. `FunctionTable::LoadParams` 在加载权重时按是否 preshard 选 `LoadMultiGPU` 或 `LoadMultiGPUPresharded`，返回一个 `DRef`（disco 分布式引用），其每个 worker 持有自己那份切好的权重。
4. 每个 worker 进程其实是 `mlc_llm.cli.worker` 拉起的 Python 子进程，它进入 TVM 的 `runtime.disco.WorkerProcess` 主循环，等待 session 下发指令。

`LoadMultiGPU`（路 A）内部对**每个参数**走「broadcast 或 scatter」二选一：无 preproc（无需切分）的参数直接 broadcast；有 preproc（需切分）的参数由 worker 0 跑切分 TIR 得到 `[N, ...]`，再 `ScatterFromWorker0` 把第 i 片发给第 i 个 worker。

#### 4.3.3 源码精读

**加载器选路**在 `function_table.cc::LoadParams`，由环境变量决定：

[cpp/serve/function_table.cc:172-178](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L172-L178) —— 行 172-174 三元表达式选函数名：无 `MLC_INTERNAL_PRESHARD_NUM` → `mlc.multi_gpu.LoadMultiGPU`（现场切），否则 → `mlc.multi_gpu.LoadMultiGPUPresharded`（读已切好的）。行 176-177 把 `(model_path, disco_mod, model_config_json)` 传给它，返回 `DRef`。

注意上面的 `if (this->model_metadata_.params.empty())` 分支（行 158-170）是另一种「整体加载」路径（用 `runtime.disco.ShardLoader`），与按参数名加载的 `LoadMultiGPU*` 互补。

**`LoadMultiGPU` 的核心**是 worker 0 切分 + scatter，其余 worker 接收：

[cpp/multi_gpu/multi_gpu_loader.cc:110-127](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/multi_gpu/multi_gpu_loader.cc#L110-L127) —— `BroadcastOrShardAndScatter`：若该参数 `preprocs` 为空（无需切），`BroadcastFromWorker0`（行 114）；否则先 `preprocs.Apply` 跑切分 TIR 得到第一维为 `num_shards` 的张量（行 123），校验 `shape[0]==num_shards`（行 120-122），再 `ScatterFromWorker0` 把第 i 片发到 worker i（行 125）。

`PreprocessorPool::Apply` 就是按 `preproc.func_name` 从 model lib 取出切分 TIR 函数并逐个调用——这正是 4.2 里 `gen_shard_info`/`gen_tir` 在运行期的落点：

[cpp/multi_gpu/multi_gpu_loader.cc:61-92](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/multi_gpu/multi_gpu_loader.cc#L61-L92) —— `PreprocessorPool` 构造时（行 67-72）优先从 `vm_module` 取函数，取不到再查全局函数；`Apply`（行 81-92）按 `preprocs` 顺序逐个切分。

`LoadMultiGPU` 主流程分三步：Step 0 加载 metadata 并**校验切数一致**（行 166-171，这条 ICHECK 正是「转换期/编译期与运行期 shards 必须一致」的硬约束），Step 1 建 preproc 池与参数索引，Step 2 worker 0 读盘+切分+分发、其余 worker 接收，Step 3 按 metadata 顺序重排：

[cpp/multi_gpu/multi_gpu_loader.cc:166-171](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/multi_gpu/multi_gpu_loader.cc#L166-L171) —— 切数校验：model lib 编译时的 `tensor_parallel_shards` 必须等于当前 disco 的 `num_shards`，否则报错并提示去改 `mlc-chat-config.json`。

[cpp/multi_gpu/multi_gpu_loader.cc:242-249](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/multi_gpu/multi_gpu_loader.cc#L242-L249) —— Step 3：按 `model_metadata.params` 的顺序重排切好的参数，缺位填 `Optional<Tensor>()`（用于流水线并行下某 group 不持有该参数的情形）。

**`LoadMultiGPUPresharded`**（路 B）则简单得多——无需切分，每个 worker 直接按 `param_shard-{local_worker_id}` 名读自己那份：

[cpp/multi_gpu/multi_gpu_loader.cc:289-307](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/multi_gpu/multi_gpu_loader.cc#L289-L307) —— 行 289 判断 `needs_sharding`；行 291-294 需切分时拼出 `param_shard-{local_worker_id}` 名字（这正是 preshard.py 里 `_sharded_param_name` 的命名），否则用原名；行 302-307 复用文件流读盘。

**disco 会话的建立**在 `engine.cc::CreateDiscoSession`：按 `num_workers = num_shards * num_stages` 决定是否拉起会话，单机用 `ProcessSession`，跨节点流水线用 `SocketSession`，最后 `InitCCL`：

[cpp/serve/engine.cc:818-877](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L818-L877) —— 行 819 `num_workers = num_shards * max_num_stages`；行 828-835 按 device 选 `nccl`/`rccl`；行 874-875 单机建 `ProcessSession`，入口 `mlc_llm.cli.worker`；行 857-863 跨节点建 `SocketSession`；行 877 `InitCCL` 统一初始化集合通信（这正是 `dim=1` 权重 all-reduce 所依赖的底层）。

**Python 侧如何为 disco 提供 worker 进程**：`__init__.py` 注册了一个全局函数，给 disco 运行时一个「拉本地 worker」的回调：

[python/mlc_llm/__init__.py:13-21](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/__init__.py#L13-L21) —— `_create_socket_session_local_workers` 返回一个 `ProcessSession`，entrypoint 写死 `mlc_llm.cli.worker`，`num_groups=1`（即 SocketSession 里每个节点本地是一组）。它被 `@register_global_func("runtime.disco.create_socket_session_local_workers", override=True)` 注册成全局函数，供 C++/TVM 侧的 SocketSession 在初始化本地 worker 时回调。

而每个 worker 子进程的入口就是 `cli/worker.py`，它解析 5 个命令行参数（worker_id / num_workers / num_groups / 读 fd / 写 fd），然后进入 TVM 的 `WorkerProcess` 主循环：

[python/mlc_llm/cli/worker.py:29-51](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/worker.py#L29-L51) —— `main`：行 35-39 解析 5 个参数；行 49-50 取全局函数 `runtime.disco.WorkerProcess` 并调用，正式进入 disco worker 循环。注意行 23 `from .. import base  # noqa: F401` 与行 26 注册 calibrate——这保证 worker 进程也加载了 mlc_llm 的全部注册函数（包括上面那个 `create_socket_session_local_workers`），让边界回调在子进程里也可见。

> 收口：`CreateDiscoSession`（engine.cc）拉会话 → 会话按 entrypoint `mlc_llm.cli.worker` fork worker 进程 → worker 进程跑 `cli/worker.py:main` 进入 `WorkerProcess` 循环 → 主进程 `FunctionTable::Init` 经会话把 model lib 广播到各 worker → `LoadMultiGPU[Presharded]` 在每个 worker 里读盘+切分+分发。整条链路里，**切分知识**来自 `ShardSingleDim`，**切分执行**来自 preprocs/preshard，**跨卡分发**来自 disco。

#### 4.3.4 代码实践

这是一个**源码阅读型实践**。

1. **实践目标**：把「shards>1 时权重从磁盘到各卡」的完整路径在源码里走一遍。
2. **操作步骤**：
   - 从 `engine.cc:CreateDiscoSession`（772-883 行）读起，找到 `ProcessSession(..., "mlc_llm.cli.worker")`（874-875 行）。
   - 跳到 `cli/worker.py`，看 worker 进程入口如何进入 `WorkerProcess`（49-50 行）。
   - 回到 `function_table.cc::LoadParams`，看路 A/B 选路（172-178 行）。
   - 进 `multi_gpu_loader.cc::LoadMultiGPU`，看 worker 0 的「读盘+切分+scatter」（180-211 行）与其余 worker 的「接收」（216-239 行）。
3. **需要观察的现象**：worker 0 与非 0 worker 走的是 `if (worker_id == 0)` 的两个不同分支；非 0 worker 里又区分「组内第一个 worker」与「组内其余 worker」（行 226-237）。
4. **预期结果**：你能画出一张时序图，包含「主进程 CreateDiscoSession → fork N 个 worker → worker 0 读盘切分 → ScatterFromWorker0 → 各 worker 收到自己那份」。
5. **待本地验证**：若有多卡机器，可用 `mlc_llm compile ... --tensor-parallel-shards 2` 编一个模型，再 `mlc_llm chat --device cuda:0,cuda:1` 运行，观察日志里 `[Worker #0]` / `[Worker #1] Loading model to device` 的输出（对应 `multi_gpu_loader.cc` 行 160 / 261 的 `LOG(INFO)`）。

#### 4.3.5 小练习与答案

**练习 1**：`LoadMultiGPU` 里，为什么 worker 0 之外的 worker 还要分「组内第一个」和「组内其余」两种？

**答案**：这是为**流水线并行**留的口子。当 `num_stages>1` 时， disco 有多个 group，每个 group 对应一个 pipeline stage。组内第一个 worker（`worker_id % group_size == 0`）需要从「全局 worker 0」接收完整参数再在本组内分发；组内其余 worker 只需从本组第一个 worker 接收。纯 TP（`num_stages==1`）时只有一个组，逻辑退化为普通的 scatter。

**练习 2**：`FunctionTable::Init` 在 `use_disco=true` 时，为何不直接 `LoadFromFile` 拿本地 VM？

**答案**：因为多卡下要保证**每张卡都加载同一份 model lib 并执行同一份代码**。disco 的做法是把模块加载包成 session 级远程调用 `runtime.disco.load_vm_module`（见 `function_table.cc:78-79`），由 session 广播到所有 worker；之后每次取函数（`mod_get_func`，行 80-88）也走 session 的 `ModuleGetFunction`，返回的 `DRef` 在各 worker 上并行执行。直接本地加载只会让主进程有模块、worker 没有。

---

## 5. 综合实践

**任务：跟踪一个 QKV 权重在 2 卡 TP 下的一生。**

把本讲三个模块串起来，回答下面这条链上的每个问题（纯源码阅读，不需运行）：

1. **声明**：在 `llama_model.py` 的 `_set_tp` 里，`qkv_proj` 的 `ShardSingleDim("_shard_qkv", segs=[q,k,v], dim=0)` 是何时被挂到 `weight.attrs` 上的？`q/k/v` 三个值各由哪些 config 字段算出？
2. **路径分支**：假设你用路 A（不设 `MLC_INTERNAL_PRESHARD_NUM`）。
   - `compile.py:_apply_preproc` 会为它生成什么 `preprocs` 条目（`func_name / in_shape / out_shape`）？写下来。
   - `out_shape[0]` 为什么必须等于 2？
3. **运行期切分**：在 `LoadMultiGPU` 里，`PreprocessorPool::Apply` 调用的切分函数体，对应 `tensor_parallel.py:gen_tir` 里的哪几行？切完后 `ScatterFromWorker0` 把哪一片发给 worker 1？
4. **路径对比**：若改走路 B（设 `MLC_INTERNAL_PRESHARD_NUM=2`），`convert_weight` 阶段 `apply_preshard` 会把这一个参数展开成哪两个名字落盘？运行期 `LoadMultiGPUPresharded` 又如何按 `local_worker_id` 各取一份？
5. **会话保障**：这一切能跨卡协同，依赖 `engine.cc:CreateDiscoSession` 做了哪两件关键事（拉会话 + `InitCCL`）？`InitCCL` 用的 `ccl` 字符串在 CUDA 下是什么？

把答案整理成一张「参数生命周期表」，列含：阶段、触发开关、调用的关键函数（带源码行号）、产物。完成后，你应当能用一句话向别人解释「MLC 的张量并行为什么不写一行分布式代码就能跑多卡」——因为切分知识集中在 `ShardSingleDim`、执行被编译期/加载器吸收、跨卡协同交给 disco。

## 6. 本讲小结

- **声明即分片**：模型代码只在权重上挂一个 `ShardSingleDim(name, dim, segs)` 标签，单卡/多卡同一份代码；`dim=0` 切输出维（如 qkv/gate_up），`dim=1` 切输入维（如 o_proj/down_proj，需 all-reduce）。
- **`gen_tir` 用 reshape+transpose 实现按段切分**：`segs` 让 QKV、gate_up 的各段被独立均分，保证每卡拿到一组完整的头，不破坏 GQA 比例。
- **两条互斥路径共享 `ShardSingleDim`**：路 A（preprocs，默认）把 `gen_shard_info`+`gen_tir` 写进 model lib、运行期现场切；路 B（preshard，`MLC_INTERNAL_PRESHARD_NUM`）在转换期用 `gen_tir` 切好落盘。两端切数必须一致。
- **运行期加载按环境变量选路**：`function_table.cc::LoadParams` 据有无 `MLC_INTERNAL_PRESHARD_NUM` 选 `LoadMultiGPU`（worker 0 切 + `ScatterFromWorker0`）或 `LoadMultiGPUPresharded`（各 worker 直读 `param_shard-{i}`）。
- **跨卡协同靠 disco**：`CreateDiscoSession` 按 `num_workers=num_shards*num_stages` 拉起 `ProcessSession`/`SocketSession` 并 `InitCCL`；worker 进程入口统一是 `mlc_llm.cli.worker`，由 `__init__.py` 注册的 `create_socket_session_local_workers` 为 SocketSession 提供本地 worker 池。
- **编译期↔运行期靠 metadata 字符串契约**：`gen_shard_info` 的 `func_name` / `out_shape[0]==num_shards` 是 loader 与编译产物之间的约定，`multi_gpu_loader.cc:166-171` 的 ICHECK 守护这条一致性。

## 7. 下一步学习建议

- **u12-l3 分离式推理（Disaggregation）**：本讲的 disco 会话是 prefill/decode 跨实例传输 KV 的基础，读完本讲再看 disagg 动作会很自然。
- **u8-l4 流水线并行**：本讲多次提到 `num_stages` / `pipeline_stages` / `pipeline_parallel_stages`，其编译期改写机制在 `pipeline_parallel_rewrite.py`，可与本讲的加载器 `group` 概念对照阅读。
- **继续读源码**：想深入 disco 本身，可读 TVM 的 `runtime/disco/`（在 `3rdparty/tvm` 下）；想加一个新模型的 TP 支持，可仿照 `llama_model.py:_set_tp` 给目标模型的四个投影层挂 `ShardSingleDim` 标签即可。
