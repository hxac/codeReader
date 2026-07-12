# LoRA 适配器机制

## 1. 本讲目标

LoRA（Low-Rank Adaptation）是大模型微调最常用的轻量化手段：冻结原始权重，只训练一对极小的低秩矩阵。本讲聚焦 **LMDeploy PyTorch 后端如何把多个 LoRA 适配器高效地挂到推理引擎上**，让一个部署实例同时服务多个「微调分身」，且同批次不同请求可走不同适配器。

读完本讲，你应当能够：

- 说清「适配器从用户配置到挂载进模型」的完整加载链路；
- 看懂 `nn/linear/lora.py` 中 `LoRA` 层如何把「base 权重输出 + adapter 低秩增量」叠加，以及多适配器如何打包进同一组矩阵；
- 理解 `AdapterManager` 如何为同批次每条请求分配 `adapter_id`，并在运行时按 id 路由到对应适配器。

本讲承接 [u5-l2 线性层与权重量化变体](./u5-l2-linear-quant-variants.md) 中 `LinearBase` 的「薄包装 + 委托」与张量并行（TP）切分约定，是它的直接延伸。

## 2. 前置知识

### 2.1 LoRA 的数学直觉

全量微调要更新巨大的权重矩阵 \(W\in\mathbb{R}^{out\times in}\)。LoRA 假设「有用的更新」是低秩的，于是把更新量分解为两个小矩阵的乘积：

\[
W' = W + \Delta W = W + s\cdot B A,\qquad A\in\mathbb{R}^{r\times in},\ B\in\mathbb{R}^{out\times r},\ s=\frac{\alpha}{r}
\]

其中秩 \(r\ll\min(in,out)\)（常取 8/16/64），缩放 \(s=\alpha/r\)。前向时：

\[
y = Wx + s\cdot B(Ax)
\]

训练只学 \(A,B\)，参数量从 \(out\times in\) 降到 \(r\cdot(in+out)\)。推理时既可把 \(\Delta W\) 预先加回 \(W\)（合并模式），也可保留 \(A,B\) 在线计算增量（**适配器模式**）。LMDeploy 走的是后者——这样能**在同一份 base 权重上同时挂多个适配器**，按请求切换。

### 2.2 关键术语回顾

- **LinearBase 与 `lora_adapters`**：[u5-l2](./u5-l2-linear-quant-variants.md) 讲过，所有线性层继承 `LinearBase`，它内部维护一个 `lora_adapters: nn.ModuleDict`，把若干 `LoRA` 子模块按名字挂上。本讲要回答「这些子模块怎么来、怎么算、怎么选」。
- **packed_modules_mapping**：把 HF 的 `q_proj`/`k_proj`/`v_proj` 融合成 `qkv_proj` 这类打包参数的契约（见 [u3-l4](./u3-l4-llama-model-rewrite.md)）。LoRA 同样要处理「适配器原本挂在 `q_proj`，但运行时它在 `qkv_proj` 里」。
- **OpType 与 backends 桥接**：[u5-l1](./u5-l1-nn-optimized-modules.md)/[u5-l4](./u5-l4-op-backend-dispatch.md) 讲过「薄包装 + 委托」，LoRA 层也是这套：`nn.LoRA` 经 `OpType.LoRA` 查到 `TritonLoRAImpl` 真身。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `lmdeploy/messages.py` | 用户面 `PytorchEngineConfig.adapters` 字段（`dict[name, path]`），引擎创建时传入 |
| `lmdeploy/pytorch/engine/engine.py` | `Engine.__init__` 下载适配器、调 `_build_adapter_manager` 建管理器、把 `adapters` 透传给 executor；`_on_add_message` 把每条请求的 `adapter_name` 挂到序列上 |
| `lmdeploy/pytorch/engine/model_agent/agent.py` | 在 patched 模型建好后调 `add_adapters(patched_model, adapters, ...)` 真正挂载 |
| `lmdeploy/pytorch/models/patch.py` | `add_adapters()`：解析每个适配器的 PEFT 配置 → 为每个目标层建 `LoRA` 子模块 → 灌权重 |
| `lmdeploy/pytorch/adapter/adapter.py` | 适配器辅助：`get_ranks_and_scalings`、`find_all_target`、`load_lora_weights`、`AdapterManager` |
| `lmdeploy/pytorch/nn/linear/lora.py` | `LoRA(nn.Module)`：单个目标层的多适配器打包层，含两个 `weight_loader` 处理 TP 切分 |
| `lmdeploy/pytorch/nn/linear/base.py` | `LinearBase` 持有 `lora_adapters`，前向时 `base 输出 + Σ adapter` |
| `lmdeploy/pytorch/engine/inputs_maker.py` | `_set_adapter_ids`：每步把每条序列的 `adapter_name` 翻成 `adapter_id` 写入 `ModelInputs` |
| `lmdeploy/pytorch/backends/lora.py` | `AdapterInfo` 数据类、`LoRAImpl`/`LoRABuilder` 抽象接口 |
| `lmdeploy/pytorch/backends/cuda/lora.py` | `TritonLoRAImpl`：调 fused kernel 算增量并叠加到 base 输出 |
| `lmdeploy/pytorch/kernels/cuda/fused_lora.py` | Triton `_fused_lora_kernel`：按序列 `adapter_id` 路由，两段 matmul 算 \(s\cdot B(Ax)\) |

## 4. 核心概念与源码讲解

本讲按「**加载 → 计算 → 选择**」三个最小模块组织：先看适配器如何从磁盘长成模型里的 `LoRA` 子模块，再看单个 `LoRA` 子模块的前向数学，最后看运行时如何为同批次不同请求选不同适配器。

### 4.1 适配器的加载与构建全链路

#### 4.1.1 概念说明

「适配器」在磁盘上就是一个 PEFT（HuggingFace `peft` 库）训练产物目录，里面有 `adapter_config.json`（记录 `r`、`lora_alpha`、`target_modules` 等）和 `adapter_model.safetensors`（记录 `lora_A`/`lora_B` 权重）。

LMDeploy 的设计目标是**一个引擎实例挂多个适配器**，并且**每个目标线性层把所有适配器的低秩矩阵打包进同一组大矩阵**，而非每个适配器各建一份。这样：

- 显存开销只随「适配器数量 × rank」线性增长，而不重复存 base 权重；
- 同一 batch 内不同 token 可走不同适配器，由一个 kernel 一次算完。

因此「加载」不是简单地 `model.load_state_dict`，而是一条多阶段链路：**解析 PEFT 配置 → 为每个目标层建打包的 `LoRA` 子模块 → 把每个适配器的 `lora_A`/`lora_B` 写进对应的 rank 切片**。

#### 4.1.2 核心流程

从用户敲下 `PytorchEngineConfig(adapters={...})` 到 `LoRA` 子模块就位，链路如下：

```text
PytorchEngineConfig.adapters (dict[name, path])
        │
        ▼  Engine.__init__
_download_adapters()          # 本地路径直用，否则从 HF 下载
        │
        ├─► adapters 透传 build_executor → ModelAgent._build_model
        │       │
        │       ▼  建好 patched 模型、灌完 base 权重后
        │   add_adapters(patched_model, adapters)        # patch.py
        │       │
        │       ├─ PeftConfig.from_pretrained 读每个适配器的 r/alpha/target_modules
        │       ├─ 在适配器列表前插一个 "空适配器"(r=0) 作为 id=0
        │       ├─ 对每个 target_modules 中的目标层名：
        │       │     ├─ get_ranks_and_scalings  算各适配器 rank 与 scaling
        │       │     ├─ find_all_target         找到模型里所有同名层（含打包层 pack_idx）
        │       │     ├─ 分配 sum_rank 大小的 lora_a / lora_b 缓冲
        │       │     ├─ new LoRA(...)           挂到 mod.lora_adapters[name]
        │       │     └─ （略）weight_loader 在此时已绑定
        │       └─ 逐适配器读 adapter_model.safetensors → load_lora_weights(model, ..., adapter_id)
        │
        ▼  Engine.__init__
_build_adapter_manager(adapters)   # AdapterManager：name→id 映射，供运行时选适配器
```

关键约定：**适配器 id=0 恒为「无适配器」**（base 模型本身），其余按名字排序编号。这条约定在 `add_adapters` 与 `AdapterManager` 两处**各自独立地**实现，必须保持一致。

#### 4.1.3 源码精读

**① 用户配置与下载**。用户在 `PytorchEngineConfig` 里传 `adapters` 字典，定义见 [messages.py:441](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L441)（字段含义说明在 [messages.py:367](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/messages.py#L367)）。`Engine.__init__` 读出后做本地化下载，再透传给 executor，并另行建一个 `AdapterManager`：

[engine.py:125-127](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L125-L127) 取出 `adapters` 并下载；[engine.py:158](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L158) 把 `adapters` 透传给 `build_executor`（最终抵达 model_agent）；[engine.py:178](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L178) 建运行时管理器。

下载逻辑很朴素——本地存在直用，否则交给 `get_model`（HF hub 下载）：

```python
# engine.py:260-272  (_download_adapters)
for name, path in adapters.items():
    if os.path.exists(path):
        new_adapters[name] = path      # 本地路径直用
        continue
    new_path = get_model(path, download_dir=download_dir, revision=revision)
    new_adapters[name] = new_path
```

`_build_adapter_manager` 只有一行：[engine.py:274-275](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L274-L275) `return AdapterManager(adapters)`。

**② 在 patched 模型上挂载**。真正「长出 `LoRA` 子模块」发生在 model_agent 建好 patched 模型、灌完 base 权重之后：

[agent.py:1127-1129](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/model_agent/agent.py#L1127-L1129) 判空后调用 `add_adapters(patched_model, adapters, dtype=..., device=...)`。

`add_adapters` 是整个加载链路的心脏，位于 [patch.py:229-327](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/patch.py#L229-L327)。先看它如何构造「id=0 = 无适配器」的约定，并为每个目标层算出各适配器的 rank/scaling：

```python
# patch.py:255-260
adapter_cfgs = [PeftConfig.from_pretrained(adapters[name]) for name in adapter_names]
adapter_cfgs = [LoraConfig(r=0, target_modules=[])] + adapter_cfgs   # 头部插空适配器 → id=0
adapter_names = [None] + adapter_names
adapter_id_map = dict(zip(adapter_names, range(len(adapter_names))))
```

接着遍历所有目标层名，为模型里**每一处**同名线性层建一个打包 `LoRA`。注意它用 `find_all_target` 同时处理「普通层」与「打包层」（如 `q_proj` 命中 `qkv_proj`，得到 `pack_idx` 指明是第几段）：

```python
# patch.py:272-312（节选）
ranks, scalings = get_ranks_and_scalings(target_name, adapter_cfgs, device=device)
target_name = target_name.split('.')[-1]
found_mods, pack_idx = find_all_target(model, target_name)
sum_rank = ranks.sum().item()
...
lora_a = torch.empty((sum_rank, in_features), dtype=dtype, device=device)
lora_b = torch.empty((sum_rank, out_features), dtype=dtype, device=device)
lora = LoRA(in_features, out_features, ranks=ranks, scalings=scalings,
            lora_a=lora_a, lora_b=lora_b, base_slice=base_slice,
            ctx_mgr=ctx_mgr, colwise=colwise, is_tp=mod.is_tp,
            lora_b_spliter=lora_b_spliter)
mod.lora_adapters[target_name] = lora        # 挂到 LinearBase 的 lora_adapters
```

几个要点：

- `lora_a` 形状 `(sum_rank, in_features)`、`lora_b` 形状 `(sum_rank, out_features)`——**所有适配器的低秩矩阵沿 rank 维拼接成两块大缓冲**，`sum_rank = ranks.sum()`。`ranks` 是长度等于「适配器数（含空）」的张量，第 0 个为 0。
- `base_slice` 针对打包层：若目标层是 `qkv_proj` 中的某段（`pack_idx` 非 None），`base_slice = slice(prev_feats, prev_feats+out_features)` 圈出该段在 base 输出里的位置，LoRA 增量只叠加到这一段。
- `find_all_target` 返回模型中所有以 `.<packed_name>` 结尾的模块，定义见 [adapter.py:26-45](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/adapter/adapter.py#L26-L45)。

**③ 灌权重**。子模块建好后，逐个适配器读 `adapter_model.safetensors`，按其 `adapter_id` 把权重写进对应 rank 切片：

```python
# patch.py:315-325
for name, path in adapters.items():
    adapter_id = adapter_id_map[name]
    checkpoint_path = f'{path}/adapter_model.bin'
    if not osp.exists(checkpoint_path):
        checkpoint_path = f'{path}/adapter_model.safetensors'
    state_dict = load_state_dict(checkpoint_path, map_location=device)
    if hasattr(model, 'load_lora_weights'):
        model.load_lora_weights(state_dict.items(), adapter_id=adapter_id)
    else:
        load_lora_weights(model, state_dict.items(), adapter_id=adapter_id)
```

模型可自定义 `load_lora_weights`（如 `internlm2.py` 在权重名上做改名后再委托通用函数，见 [internlm2.py:350-372](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/internlm2.py#L350-L372)），否则走通用版 [adapter.py:84-108](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/adapter/adapter.py#L84-L108)。通用版的核心是把 PEFT 的权重名（`base_model.model.<...>.lora_A.<mod>.weight`）映射到 `mod.lora_adapters[name].lora_A` 参数上，再调 `load_weight(param, loaded_weight, adapter_id=...)`——`adapter_id` 决定写进哪段 rank。

#### 4.1.4 代码实践

**实践目标**：理解 `add_adapters` 的「空适配器占位」与「rank 打包」两个关键设计。

**操作步骤**（源码阅读型，无需 GPU）：

1. 打开 `lmdeploy/pytorch/models/patch.py` 的 `add_adapters`（L229 起）。
2. 假设用户传入 `adapters = {"legal": p1, "code": p2}`（两个适配器，rank 分别为 8、16）。手动推演：
   - `adapter_cfgs` 长度是多少？`adapter_id_map` 长什么样？
   - 对某个 `target_modules` 命中的层，`ranks`、`scalings` 两个张量的值分别是什么？`sum_rank` 是多少？
3. 打开 `lmdeploy/pytorch/adapter/adapter.py` 的 `get_ranks_and_scalings`（L10-L23）核对你的推演。

**需要观察的现象 / 预期结果**：

- `adapter_cfgs` 应为 3 项（空 + legal + code），`adapter_id_map = {None:0, "legal":1, "code":2}`。
- 对某目标层，若 legal、code 都作用于它，则 `ranks = tensor([0, 8, 16])`、`scalings = tensor([1, alpha_legal/8, alpha_code/16])`（第 0 项因 `r=0` 走 `ranks.append(0); scalings.append(1)` 分支，见 [adapter.py:15-17](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/adapter/adapter.py#L15-L17)），`sum_rank = 24`。
- 这说明：**两个适配器共享同一对 `(24, in)` / `(24, out)` 缓冲**，legal 占 `[0:8]`，code 占 `[8:24]`，id=0 占空 `[0:0]`。

> 若你本地有 GPU 且已下载某 base 模型与一个 PEFT LoRA 适配器，可进一步：用 `PytorchEngineConfig(adapters={"my": path})` 创建 pipeline，对比挂与不挂适配器的输出差异。若不具备运行环境，以上源码推演即为完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `add_adapters` 要在适配器列表头部插一个 `LoraConfig(r=0, target_modules=[])`？去掉会怎样？

**答案**：这个空适配器对应「不使用任何 LoRA、纯 base 推理」的情形，占住 `adapter_id=0`。运行时 `adapter_name=None` 的请求会被映射到 id 0（见 4.3）。若去掉，则 `adapter_id_map` 与 `AdapterManager` 的编号会整体错位，且「无适配器」请求将没有合法 id 可用，导致 kernel 取到错误的 rank 切片。

**练习 2**：`lora_a` 的形状是 `(sum_rank, in_features)` 而非 `(num_adapters, r, in_features)`，这样设计有什么好处？

**答案**：把所有适配器沿 rank 维连续拼接成一块稠密矩阵，运行时只需用一个 `adapter_id → (rank_start, rank)` 的索引就能切出对应适配器的子矩阵，一个 Triton kernel 即可处理「同 batch 多适配器」，无需为每个适配器单独发射 kernel 或保存稀疏张量；同时 `lora_a @ x` 这类计算对连续内存更友好。

### 4.2 LoRA 线性层：base 与 adapter 的叠加

#### 4.2.1 概念说明

`LoRA`（`nn/linear/lora.py`）是挂在 `LinearBase.lora_adapters` 里的子模块，它代表**一个目标层名下、所有适配器共用**的打包低秩层。注意它**自己不算 base 权重**——base 权重仍由外层 `LinearBase` 持有并先算出 base 输出，`LoRA` 只负责在此基础上「叠加」增量。

它对外做两件事：

1. **前向**：接收输入 `x` 与 base 输出 `base_output`，把 \(\sum\) 适配器的 \(s\cdot B(Ax)\) 加到 `base_output` 上。
2. **权重加载**：提供两个 `weight_loader`，把 PEFT 的 `lora_A`/`lora_B` 权重按 `adapter_id` 写进正确的 rank 切片，并按 TP 切分。

#### 4.2.2 核心流程

前向数据流（委托给 backend impl，详见 4.2.3）：

```text
LinearBase.forward(x)
  ├─ out = base 线性层(x)            # 先算 base，得到 base_output
  └─ for each target_name, lora in lora_adapters.items():
        out = lora(x, base_output=out)   # 把增量叠加回 out
```

`LoRA.forward` 内部委托 `self.impl.forward(...)`（cuda 下是 `TritonLoRAImpl`），后者用一个 fused kernel 完成「按每条序列的 `adapter_id` 路由 → 算 \(s\cdot B(Ax)\) → 原子加回 base 切片」。

权重加载时，rank 切片与 TP 切分由 `AdapterInfo` 描述：

\[
\text{rank\_offsets}_i = \sum_{j<i} \text{ranks}_j,\qquad
\text{adapter }i\text{ 的 rank 区间} = [\text{rank\_offsets}_i,\ \text{rank\_offsets}_i+\text{ranks}_i)
\]

#### 4.2.3 源码精读

**① `LoRA` 的构造**。[lora.py:12-47](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/lora.py#L12-L47)：

```python
class LoRA(nn.Module):
    def __init__(self, in_features, out_features, ranks, scalings,
                 lora_a, lora_b, base_slice, ctx_mgr=None,
                 colwise=True, is_tp=True, lora_b_spliter=None):
        super().__init__()
        self.adapter_info = AdapterInfo(in_features, out_features, ranks,
                                        scalings, base_slice)   # 派生 rank_offsets/max_rank
        impl_builder = get_backend().get_layer_impl_builder(OpType.LoRA)
        self.impl = impl_builder.build()                        # 委托：cuda→TritonLoRAImpl
        lora_A = nn.Parameter(lora_a, requires_grad=False)
        lora_B = nn.Parameter(lora_b, requires_grad=False)
        lora_A.weight_loader = self.weight_loader_A             # 绑定权重加载器
        lora_B.weight_loader = self.weight_loader_B
```

要点：`ranks`/`scalings` 是张量而非标量——因为这里打包了**所有适配器**。`AdapterInfo`（[backends/lora.py:10-27](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/backends/lora.py#L10-L27)）在 `__post_init__` 里算出 `rank_offsets = ranks.cumsum(0) - ranks` 与 `max_rank`，供 kernel 切片。桥接 `OpType.LoRA → TritonLoRABuilder` 的查表在 [op_backend.py:37-39](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/backends/cuda/op_backend.py#L37-L39)。

**② 前向：base + adapter 的叠加**。[lora.py:49-58](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/lora.py#L49-L58) 把活全交给 impl；外层 `LinearBase` 在 [base.py:189-194](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/base.py#L189-L194) 的 `_forward_lora` 里循环调用 `lora(x, out)`，把 base 输出逐层叠加：

```python
# base.py:189-194  _forward_lora
def _forward_lora(self, x, tp_sizes=None):
    out = self._forward_default(x, False, tp_sizes)     # base 权重的 GEMM
    for lora_adapter in self.lora_adapters.values():
        out = lora_adapter(x, out)                       # 叠加 adapter 增量
    ...
```

且 `LinearBase.forward` 会先判断 `len(self.lora_adapters)==0` 走纯 base 快路径（[base.py:214-227](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/base.py#L214-L227)）——没挂适配器时零开销。

真正的「base + adapter」数学在 cuda impl 里。[backends/cuda/lora.py:41-81](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/backends/cuda/lora.py#L41-L81)：

```python
# backends/cuda/lora.py:53-81（节选）
base_slice = adapter_info.base_slice
sliced_base = base_output[..., base_slice]          # 打包层只取对应输出段
...
lora_out = fused_lora(lora_input.x, lora_A, lora_B,
                      scaling=adapter_info.scalings,
                      rank_start=adapter_info.rank_offsets,
                      ranks=adapter_info.ranks,
                      seq_start=..., seq_lens=...,
                      adapter_ids=lora_input.adapter_ids,   # 每条序列的适配器 id
                      max_rank=adapter_info.max_rank, ...)
if not base_output.is_contiguous():
    lora_out = lora_out.reshape(sliced_base.shape)
    sliced_base.add_(lora_out)                       # 增量加回 base
return base_output
```

`fused_lora` 是 Triton kernel（[fused_lora.py:142](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/cuda/fused_lora.py#L142) 起的封装）。它的两段 matmul 就是 LoRA 公式 \(s\cdot B(Ax)\)：

```python
# fused_lora.py:110-135（_fused_lora_kernel 节选）
accumulator = tl.zeros((BLOCK_SIZE_M, BLOCK_SIZE_R), dtype=tl.float32)
for k in range(0, tl.cdiv(K, BLOCK_SIZE_K)):
    a  = tl.load(a_ptrs, ...)     # x：形状 (M, K=in)
    la = tl.load(la_ptrs, ...)    # lora_A^T：(K, R)
    accumulator = tl.dot(a, la, acc=accumulator)   # 第一段：accumulator = x @ A^T = (A x)
ar = accumulator.to(...)
scaling = tl.load(scaling_ptr + adapter_id).to(ar.dtype)   # 按本序列 adapter_id 取 s
ar *= scaling                                                # 乘缩放
...
lb = tl.load(lb_ptrs, ...)       # lora_B：(R, N=out)
c = tl.dot(ar, lb)               # 第二段：c = (s·A x) @ B = s·B(Ax)，形状 (M, N)
...
_atomic_store(c_ptrs, c, mask=c_mask)   # 把 c 原子加到 base 输出（CUM 路径）
```

注意三个细节：① `scaling`/`rank_start` 都用 `adapter_id` 索引，**同 batch 不同序列走不同适配器**；② 当 `adapter_id` 指向 id=0（空适配器，rank=0）时，进入 [fused_lora.py:95-101](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/cuda/fused_lora.py#L95-L101) 的 `rank==0` 分支，把对应输出置零——即「不加任何增量」；③ 输出通过 `_atomic_store` 累加（[fused_lora.py:24-34](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/cuda/fused_lora.py#L24-L34)），因为多个 program 块可能写同一输出位置。

**③ 权重加载与 TP 切分**。[lora.py:60-93](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/lora.py#L60-L93)。两个 loader 都先用 `adapter_id` 定位 rank 切片 `[r_start:r_end]`，再按 `colwise` 决定 TP 切分方式：

```python
# lora.py:60-72  weight_loader_A（lora_A：(r, in)）
rank = self.adapter_info.ranks[adapter_id].item()
r_start = self.adapter_info.rank_offsets[adapter_id].item()
param_r = param.data[r_start:r_start + rank]
if self.is_tp and not colwise:               # 行并行层（如 o_proj/down_proj）：切输入维
    world_size, rank_tp = get_tp_world_rank()
    loaded_weight = loaded_weight.chunk(world_size, dim=1)[rank_tp]
param_r.copy_(loaded_weight)
```

```python
# lora.py:74-93  weight_loader_B（lora_B：(r, out)）
...
if self.is_tp and colwise:                   # 列并行层（如 qkv/gate_up）：切输出维
    world_size, rank_tp = get_tp_world_rank()
    ...loaded_weight = loaded_weight.chunk(world_size, dim=0)[rank_tp]
param_r.copy_(loaded_weight.t())             # 注意转置：PEFT 存 (out,r)，这里存成 (r,out)
```

这与 [u5-l2](./u5-l2-linear-quant-variants.md)/[u9-l4](./u9-l4-tensor-parallelism-distribution.md) 讲的 Megatron 式 TP 完全一致：**列并行切输出、行并行切输入**；LoRA 的 \(A\) 作用于输入侧（与 base 行并行层同向切 dim=1），\(B\) 作用于输出侧（与 base 列并行层同向切 dim=0）。末尾 `loaded_weight.t()` 把 PEFT 默认的 `(out, r)` 布局转成 kernel 期望的 `(r, out)` 布局。

#### 4.2.4 代码实践

**实践目标**：定位「base + adapter 叠加」的实现，并核对 LoRA 公式在 kernel 里的两段 matmul。

**操作步骤**（源码阅读型）：

1. 打开 `lmdeploy/pytorch/nn/linear/base.py`，找到 `_forward_lora`（L189），确认 `out = self._forward_default(...)` 之后有 `for lora_adapter in self.lora_adapters.values(): out = lora_adapter(x, out)`（L193-194）。
2. 打开 `lmdeploy/pytorch/kernels/cuda/fused_lora.py`，在 `_fused_lora_kernel`（L42）中找到：第一段 `tl.dot(a, la)`（L117）、`ar *= scaling`（L124）、第二段 `tl.dot(ar, lb)`（L130）、`_atomic_store`（L135）。
3. 对照本节公式 \(y = Wx + s\cdot B(Ax)\)，把上述四行一一对应到公式里的运算。

**需要观察的现象 / 预期结果**：

- `_forward_default` 算的是 \(Wx\)（base），`fused_lora` 算的是 \(s\cdot B(Ax)\)（adapter 增量），`_atomic_store`/`add_` 把两者相加。
- kernel 中 `a` 对应 \(x\)，`la` 对应 \(A\) 的转置（故 `dot(a,la)=Ax`），`lb` 对应 \(B\)，`scaling` 对应 \(s=\alpha/r\)。
- 你应能解释：为什么 `lora_A` 存 `(r, in)` 但 kernel 里以 `(in, r)` 的 `la` 参与 dot——因为 `tl.load` 时用 `(offs_k[:,None], offs_r[None,:])` 读了它的转置视图。

**待本地验证**：若你有 GPU，可对同一 prompt 分别用 `adapter_name=None` 与某真实适配器推理，对比输出 token；理论上两者差异完全来自 `fused_lora` 算出的增量。

#### 4.2.5 小练习与答案

**练习 1**：`LoRA.forward` 里没有出现 `lora_A @ x` 这样的算式，真正的矩阵乘在哪里？为什么要这样分？

**答案**：真正的两段 matmul 在 `TritonLoRAImpl.forward` 调用的 `fused_lora` Triton kernel 中（`fused_lora.py` 的 `tl.dot(a, la)` 与 `tl.dot(ar, lb)`）。这样分是为了：① 让「按 `adapter_id` 路由 + 两段 GEMM + 原子累加」融合进单个 kernel，减少中间张量与显存往返；② 与 [u5-l1/u5-l4](./u5-l4-op-backend-dispatch.md) 的「nn 薄包装 + backends 实现」桥接模式保持一致，换设备只改 impl、不改 `nn.LoRA`。

**练习 2**：`weight_loader_B` 末尾有 `.t()`，而 `weight_loader_A` 没有。为什么？

**答案**：PEFT 把 `lora_B` 存成 `(out, r)`，而 lmdeploy 的 kernel 按 `(r, out)` 布局读取 `lora_B`（见 `lb_ptrs` 用 `(offs_r, offs_n)` 加载），故加载时需转置成 `(r, out)` 再写入 `param.data[r_start:r_end]`。`lora_A` 在 PEFT 中本就是 `(r, in)`，与 kernel 读取方向一致（kernel 读其转置视图即可），故无需转置。

**练习 3**：某目标层是 `qkv_proj` 里的 `k_proj`（`pack_idx=1`），`base_slice` 起什么作用？

**答案**：`qkv_proj` 把 q/k/v 三段输出拼成一条，`k_proj` 的 LoRA 增量只应加到 k 那一段。`base_slice = slice(prev_feats, prev_feats+out_features)` 圈出 k 段在拼接输出里的范围，kernel 用 `sliced_base = base_output[..., base_slice]` 只取该段做累加（[backends/cuda/lora.py:53-54](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/backends/cuda/lora.py#L53-L54)），避免污染 q、v 段。

### 4.3 运行时适配器管理与切换

#### 4.3.1 概念说明

前两节解决了「适配器怎么挂」和「单层怎么算」。本节回答最后一个问题：**一个 batch 里同时有多条请求，它们各自指定了不同的 `adapter_name`，引擎如何让 fused kernel 知道每条请求该用哪个适配器？**

答案是三层协作：

1. **`AdapterManager`**：引擎启动时建好「名字 → id」映射，恒以 `None`（无适配器）为 id 0。
2. **每步 forward 前**：`InputsMaker._set_adapter_ids` 把本批每条序列的 `adapter_name` 翻成一个 `adapter_id` 张量，塞进 `ModelInputs.local_adapter_ids`。
3. **kernel 内**：fused kernel 按每条序列的 `adapter_id` 取对应的 `rank_start`/`ranks`/`scaling`，算各自的增量。

`adapter_name` 则由用户在**每次请求**时指定（默认 `None` = 纯 base），从用户面一路透传到序列对象。

#### 4.3.2 核心流程

```text
请求级:  generate(messages, adapter_name="legal")          # async_engine.py:491
          └─► engine._on_add_message
                 └─► sess.add_sequence(..., adapter_name="legal")   # engine.py:458
                        └─ SchedulerSequence 记住 adapter_name

每步 forward:
   InputsMaker._set_adapter_ids(model_inputs, messages)            # inputs_maker.py:465
      ├─ adapter_names = [msg.adapter_name for msg in messages]
      ├─ adapter_manager.get_adapter_ids(adapter_names)            # 名字 → id 列表
      └─ model_inputs.local_adapter_ids = tensor(ids)              # 每序列一个 id
          └─► 经 StepContext → cuda lora impl 的 PackedLoRAInput.adapter_ids
                 └─► fused_lora(..., adapter_ids=...) 按 id 路由
```

`AdapterManager` 还承担「短路」职责：当 `num_adapters()<=1`（即只有 id=0，没挂任何适配器）时，`_set_adapter_ids` 直接 return，不产生 `local_adapter_ids`，kernel 也根本不被触发——**没挂适配器时零开销**。

#### 4.3.3 源码精读

**① AdapterManager：名字到 id 的映射表**。[adapter.py:111-129](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/adapter/adapter.py#L111-L129)：

```python
class AdapterManager:
    def __init__(self, adapters: dict[str, str]):
        if adapters is None:
            adapters = dict()
        adapter_names = list(adapters.keys())
        adapter_names = sorted(adapter_names)            # 排序，保证与 add_adapters 一致
        adapter_names = [None] + adapter_names           # 头部插 None → id=0
        adapter_id_map = dict(zip(adapter_names, range(len(adapter_names))))
        self.adapter_id_map = adapter_id_map

    def get_adapter_ids(self, names: list[str]):
        return [self.adapter_id_map[name] for name in names]

    def num_adapters(self):
        return len(self.adapter_id_map)
```

注意这里的「排序 + 头插 None」与 `patch.add_adapters`（[patch.py:253-260](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/patch.py#L253-L260)）的约定**必须一致**——同一名字在两边得到同一 id，否则 kernel 会取错 rank 切片。`sorted()` 是为了让映射稳定不依赖字典插入顺序。

**② 每条请求绑定 adapter_name**。用户经 `AsyncEngine.generate(..., adapter_name=...)` 指定（[async_engine.py:491](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L491) 形参，[async_engine.py:547](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/core/async_engine.py#L547) 透传），最终在 `_on_add_message → _add_message` 里随建序列时写入：

[engine.py:456-458](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L456-L458)：
```python
sess.add_sequence(req.data['token_ids'],
                  sampling_param=sampling_param,
                  adapter_name=req.data['adapter_name'],
                  ...)
```

这样每条 `SchedulerSequence` 都记住自己用哪个适配器，后续调度、抢占、迁移都不丢失该信息。

**③ 每步把名字翻译成 id 张量**。[inputs_maker.py:465-472](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py#L465-L472)：

```python
def _set_adapter_ids(self, model_inputs: ModelInputs, messages: 'SeqList'):
    if self.adapter_manager.num_adapters() <= 1:         # 没挂适配器 → 短路，零开销
        return
    adapter_names = [msg.adapter_name for msg in messages]
    local_adapter_ids = self.adapter_manager.get_adapter_ids(adapter_names)
    local_adapter_ids = model_inputs.seq_length.new_tensor(local_adapter_ids)
    model_inputs.local_adapter_ids = local_adapter_ids
```

`local_adapter_ids` 是长度等于「序列数」的整型张量，第 i 个元素是第 i 条序列的 `adapter_id`。它在 prefill 与 decode 两条路径都会被设置（[inputs_maker.py:553](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py#L553) 与 [inputs_maker.py:635](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py#L635)），随后经 `StepContext.local_adapter_ids` 流入 [backends/cuda/lora.py:37](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/backends/cuda/lora.py#L37) 的 `PackedLoRAInput.adapter_ids`，最终成为 kernel 的 `adapter_ids_ptr`（[fused_lora.py:53](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/cuda/fused_lora.py#L53)）。kernel 里 `scaling = tl.load(scaling_ptr + adapter_id)`（[fused_lora.py:123](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/cuda/fused_lora.py#L123)）即按此 id 取该序列的缩放与 rank 段。

**④ 服务面如何选适配器**。在 OpenAI 兼容 API 中，适配器被对外暴露成额外的「模型名」：[responses/serving.py:39](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/responses/serving.py#L39) 把 `adapters` 的名字并入 `model_names` 列表；客户端在请求里把 `model` 字段填成某适配器名即选中它：

[responses/serving.py:71](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/serve/openai/responses/serving.py#L71)：
```python
adapter_name = None if model_name == self.server_context.async_engine.model_name else model_name
```

即「`model` 填 base 模型名 → 不用适配器；填适配器名 → 用该适配器」。CLI 侧，`lmdeploy chat` 用 `--adapters` 接收适配器路径（[cli/utils.py:530-541](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/utils.py#L530-L541)），经 `get_lora_adapters`（[cli/utils.py:38](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/utils.py#L38)）解析成 dict 后写入 `engine_config.adapters`（[cli/chat.py:33-36](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/chat.py#L33-L36)）。

> 备注：引擎内部配置 `SchedulerConfig.max_active_adapters`（[config.py:104](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L104)，默认 64）为同时活跃适配器数上限，是 LoRA 调度的一个容量旋钮。

#### 4.3.4 代码实践

**实践目标**：验证「同 batch 多适配器」的运行时路由通路，并理解 `AdapterManager` 的短路优化。

**操作步骤**（源码阅读型）：

1. 打开 `lmdeploy/pytorch/adapter/adapter.py`，列出 `AdapterManager` 的三个公开方法（`__init__`、`get_adapter_ids`、`num_adapters`），说明各自职责。
2. 打开 `lmdeploy/pytorch/engine/inputs_maker.py` 的 `_set_adapter_ids`（L465），回答：为什么第一行要先判 `num_adapters() <= 1` 再 return？如果不判会怎样？
3. 追踪 `local_adapter_ids` 的下游：从 `model_inputs.local_adapter_ids`（L472）→ `StepContext` → `PackedLoRAInput.adapter_ids`（[backends/cuda/lora.py:37](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/backends/cuda/lora.py#L37)）→ kernel 内 `tl.load(scaling_ptr + adapter_id)`（[fused_lora.py:123](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/cuda/fused_lora.py#L123)）。

**需要观察的现象 / 预期结果**：

- `AdapterManager` 三方法：`__init__` 建映射、`get_adapter_ids` 批量查名→id、`num_adapters` 返回总数（含 None）。
- 若不判 `num_adapters()<=1`：即便没挂适配器，也会每步多生成一个全 0 张量、并触发 LoRA 分支，造成无谓开销。短路保证**未使用 LoRA 时与普通引擎完全等价**。
- `local_adapter_ids` 是「每序列一个 id」的张量，正是它让同 batch 不同请求走不同适配器成为可能。

**待本地验证**：若你有 GPU 与两个适配器，可启动 `lmdeploy serve api_server <model> --adapters legal=<p1> code=<p2>`，连续发两个请求（`model` 分别填 `legal` 与 `code`），用 `LMDEPLOY_LOG_LEVEL=DEBUG` 观察二者是否在同一 batch 内被处理。

#### 4.3.5 小练习与答案

**练习 1**：`AdapterManager` 与 `add_adapters` 都各自做了「排序 + 头插 None」。如果 `AdapterManager` 漏了排序，会发生什么？

**答案**：`adapter_id_map` 的 id 分配将依赖 `adapters` 字典的插入顺序（Python 3.7+ 保序），而 `add_adapters` 那边显式 `sorted()`。一旦用户传入字典的迭代顺序与排序结果不同，两边的「同名 → id」映射就会错位：运行时某请求按 `AdapterManager` 查到 id=1，但 `add_adapters` 把它的权重写进了 id=2 的 rank 切片，导致该请求用了错误适配器的权重。排序保证两侧稳定一致。

**练习 2**：客户端在 OpenAI 请求里把 `model` 字段填成一个**未注册**的适配器名，会在哪一步报错？

**答案**：在 `_set_adapter_ids` 调用 `get_adapter_ids` → `self.adapter_id_map[name]` 时抛 `KeyError`（`AdapterManager` 未登记该名字）。因为只有写在 `PytorchEngineConfig.adapters` 里的适配器才会进入 `adapter_id_map`，未注册的名字查不到映射。

**练习 3**：为什么说「没挂适配器时 LoRA 零开销」？给出两处证据。

**答案**：① `LinearBase.forward` 先判 `len(self.lora_adapters)==0` 走纯 base 快路径（[base.py:214-227](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/nn/linear/base.py#L214-L227)），根本不进 `_forward_lora`；② `InputsMaker._set_adapter_ids` 在 `num_adapters()<=1` 时直接 return（[inputs_maker.py:467-468](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py#L467-L468)），不产生 `local_adapter_ids`。两道短路共同保证未启用 LoRA 时与普通引擎无差异。

## 5. 综合实践

**任务**：把本讲三个最小模块串起来，画出 LoRA 适配器从「磁盘文件」到「kernel 内一次累加」的端到端数据流，并标注每个阶段负责的文件与关键函数。

**要求**：

1. 画一张流程图（文字版即可），包含以下节点并连线：
   - `PytorchEngineConfig.adapters`（messages.py）
   - `Engine._download_adapters` / `_build_adapter_manager`（engine.py）
   - `ModelAgent._build_model → add_adapters`（agent.py / patch.py）
   - `LoRA` 子模块构造 + `weight_loader_A/B`（lora.py）
   - `AdapterManager`（adapter.py）
   - 请求级 `adapter_name` → `_on_add_message`（engine.py）
   - 每步 `_set_adapter_ids`（inputs_maker.py）
   - `TritonLoRAImpl.forward → fused_lora`（backends/cuda/lora.py、kernels/cuda/fused_lora.py）
2. 在图上用三种颜色（或标记）区分三类数据：**配置/权重（启动期）**、**请求名字（请求期）**、**adapter_id 张量（每步 forward）**。
3. 在每个节点旁注明一条关键源码行号（用本讲给出的永久链接）。
4. 用一段话解释：为什么「同 batch 多适配器」能在不增加 base 权重显存、且只用一个 kernel 的前提下实现。

**预期结果**：你应当得到一条清晰的「启动期构建（写 rank 切片）→ 请求期绑定名字 → 每步翻译 id → kernel 按 id 路由」的主线，并能说清「多适配器共享打包缓冲 + 按 id 切 rank 段 + 单 kernel 原子累加」是同时服务多适配器的关键。

> 若本地有 GPU 与 PEFT 适配器，可在画图后实测：`lmdeploy serve api_server <model> --adapters a=<pa> b=<pb>`，并发发送 `model=a` 与 `model=b` 两个请求，验证二者可同时被处理。

## 6. 本讲小结

- **加载链路**：用户在 `PytorchEngineConfig.adapters` 传入 `{name: path}`，经 `Engine` 下载、透传给 `ModelAgent`，在 patched 模型建好后由 `patch.add_adapters` 为每个目标层建一个打包 `LoRA` 子模块，再按 `adapter_id` 把各适配器权重写进对应 rank 切片。
- **id=0 约定**：空适配器恒占 id=0（`patch.add_adapters` 与 `AdapterManager` 各自实现），代表「纯 base 推理」。
- **rank 打包**：同一目标层的所有适配器的 `lora_A`/`lora_B` 沿 rank 维拼接成 `(sum_rank, *)` 缓冲，由 `AdapterInfo.rank_offsets` 描述每个适配器的 rank 区间，使多适配器共享一组矩阵。
- **base + adapter 叠加**：`LinearBase` 先算 base 输出，再循环调 `lora(x, out)`；真正的 \(s\cdot B(Ax)\) 由 `TritonLoRAImpl` 的 `fused_lora` kernel 完成，两段 `tl.dot` 后原子累加回 base 输出。
- **多适配器 TP**：`weight_loader_A/B` 按 Megatron 约定切分——列并行层切输出维、行并行层切输入维，与 base 线性层 TP 方向一致；`lora_B` 加载时转置以匹配 kernel 布局。
- **运行时路由**：`AdapterManager` 提供 name→id 映射，`InputsMaker._set_adapter_ids` 每步把每条序列的 `adapter_name` 翻成 `local_adapter_ids` 张量，kernel 据此按序列取 scaling/rank；`num_adapters()<=1` 与 `len(lora_adapters)==0` 两道短路保证未用 LoRA 时零开销。

## 7. 下一步学习建议

- **[u10-l3 测试体系](./u10-l3-testing-framework.md)**：阅读 `tests/` 下与 LoRA 相关的用例，验证你对加载链路的理解；尝试为某模型写一个最小的 LoRA 接入测试。
- **深入 kernel**：精读 `lmdeploy/pytorch/kernels/cuda/fused_lora.py` 的 `_atomic_store` 与 grid 设计，理解多 program 块写同一输出时的并发安全；对比 [u5-l5](./u5-l5-triton-cuda-kernels.md) 的 w8a8 kernel，体会 LoRA 两段 GEMM 的分块策略。
- **新增模型的 LoRA 适配**：对照 [u10-l1](./u10-l1-add-new-pytorch-model.md) 与本讲，思考当一个新模型的权重命名与 PEFT 约定不一致时，为何需要重写 `load_lora_weights`（参考 `models/internlm2.py:350`），尝试为一个简单模型写一个最小改名映射。
- **调度与容量**：阅读 `SchedulerConfig.max_active_adapters` 的实际用途，结合 [u4-l4 调度器](./u4-l4-scheduler-prefill-decode.md) 思考「活跃适配器数」如何影响显存与调度决策。
