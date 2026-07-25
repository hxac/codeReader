# 检查点加载与权重重排

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `load_weights` 把 safetensors 权重搬进 `nn.Module` 的完整主流程：分片映射（shard map）、前缀剥离（VL 多模态前缀）、key remap、张量赋值、pre-repack 钩子、repack、tie weights。
- 解释为什么 EdgeLLM **不用 `module.load_state_dict()`**，而是用 `_set_tensor` 直接写 buffer/parameter——以及这一选择如何保住 `fp8`、`uint8`、`bfloat16` 等量化和特殊 dtype。
- 描述 AWQ / GPTQ / NVFP4 三类量化权重在加载后如何被 `apply_all_repacking` 重排成 C++ 插件期望的物理布局，并理解其中的零点折叠数学。
- 说明 embedding 表为什么不走线性层的 repack 路径，而是单独导出成 FP8 的 sidecar 文件。
- 了解张量并行（`tp_size` / `tp_rank`）下，每个 rank 如何在赋值时就地切分权重。

## 2. 前置知识

本讲是 Python 导出前端（u2 单元）的核心环节，承接 [u2-l1（配置解析）](u2-l1-checkpoint-config-parsing.md) 与 [u2-l2（AutoModel 分发）](u2-l2-automodel-dispatch-and-registry.md)。开始前请确认你了解以下概念：

- **safetensors**：HuggingFace 生态常用的权重存储格式，按「张量名 → 字节段」记录在一个或多个 `.safetensors` 文件里；它的索引文件是 `model.safetensors.index.json`，里面的 `weight_map` 记录每个 key 落在哪个分片文件。
- **buffer 与 parameter**：PyTorch `nn.Module` 有两种持久化张量。`parameter`（`nn.Parameter`）会被优化器跟踪（带梯度），`buffer`（`module.register_buffer`）不会。推理场景下权重无需梯度，二者都只是「模块里的一块张量」。
- **量化（quantization）**：把 FP16 权重压成低比特整数（int4/int8）或低精度浮点（FP8/FP4），以省显存、提带宽。不同量化方案（AWQ、GPTQ、NVFP4）有不同的**物理打包布局**（nibble 的排列、scale 的形状）。详见 [u2-l1](u2-l1-checkpoint-config-parsing.md) 中的 `QuantConfig`。
- **sidecar（伴生文件）**：EdgeLLM 在 ONNX 子图目录里，除了 `.onnx` 外还会写出 `embedding.safetensors`、`config.json`、tokenizer 等配套文件，统称 sidecar。
- **张量并行（TP）**：把一个大权重按某个维度切成 `tp_size` 份，每份放在一张卡（rank）上。详见 [u1-l2](u2-l1-checkpoint-config-parsing.md) 流水线概念。

一句话定位：u2-l2 讲了「**建一个空的 `nn.Module`**」，本讲讲「**把检查点里的真实权重灌进这个空壳**，并在灌的过程中把量化权重的字节布局调整成 C++ 插件能吃的格式」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tensorrt_edgellm/checkpoint/loader.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/loader.py) | 权重加载主线。`load_weights` 是公开入口；负责定位分片、剥离前缀、调用 key remap、把每个张量写进模块、再触发 repack。 |
| [tensorrt_edgellm/checkpoint/repacking.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py) | 加载后的量化权重格式重排。把 AWQ 列打包 int32、GPTQ 行打包 int32、ModelOpt uint8、NVFP4 等转换成 TensorRT 插件期望的布局。 |
| [tensorrt_edgellm/checkpoint/embedding_quantization.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/embedding_quantization.py) | 把 embedding 表量化成 FP8 E4M3（按 128 列分块带 per-block scale），用于导出阶段写出 `embedding.safetensors` sidecar。 |
| [tensorrt_edgellm/model.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py) | `AutoModel.from_pretrained`，是 `load_weights` 的唯一上层调用点；构造模块后把 `key_remap`、`key_prefix`、`pre_repack_hook`、`mapping` 透传进去。 |
| [tensorrt_edgellm/models/linear.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/linear.py) | 各类 Linear 子类（`FP16Linear`/`AWQLinear`/`GPTQLinear`/NVFP4 组合式等）定义 `tp_split_dim`，告诉 loader 该沿哪一维切分。 |

> 本讲聚焦 **loader / repacking / embedding_quantization** 三个最小模块。

---

## 4. 核心概念与源码讲解

### 4.1 loader：检查点权重的加载主线

#### 4.1.1 概念说明

一个「建好的 `nn.Module`」内部其实是一堆空的 buffer/parameter——形状对了，数值还没填。EdgeLLM 要做的就是把检查点里的张量按名字搬进这些空位。

这里有一个关键设计选择：**不用 PyTorch 自带的 `module.load_state_dict(state_dict)`，而是自己写 `_set_tensor` 逐个赋值**。原因写在 loader.py 顶部的模块文档串里：

> Rather than `module.load_state_dict()`, weights are assigned directly to module buffers/parameters via `_set_tensor`. This preserves the original tensor dtype (fp8, uint8, bfloat16, ...) without any silent cast that PyTorch's state-dict mechanism might introduce.

`load_state_dict` 内部会做 dtype 对齐与严格匹配（多了少了 key 都报错），这对**量化权重**是致命的：一个 AWQ 的 `qweight` 是 `int32`、NVFP4 的 `weight` 是 `uint8`、ModelOpt 的预打包权重也是 `uint8`——这些都不该被悄悄转成 fp16。`_set_tensor` 直接写 `module._buffers[attr] = tensor`，**原样保留 dtype**。

此外，`_set_tensor` 还做了两件 `load_state_dict` 不会做的事：

1. **`bfloat16 → float16` 就地转换**：EdgeLLM 导出流水线假设 FP16 激活，C++ 运行时也只认 FP16（或 FP8）权重文件。在加载时顺手转好，省去后续再扫一遍。
2. **找不到的 key 只记 debug 日志、不报错**：检查点里可能有多余的 key（如被丢弃的层、EAGLE3 的 `t2d`），用警告而非硬错误跳过，更鲁棒。

#### 4.1.2 核心流程

`load_weights` 的主流程是一趟「按分片分组 → 遍历 key → 赋值 → 收尾」的循环，最后才触发 repack：

```text
load_weights(model, model_dir, ...)
 │
 ├─ 1. mapping = config.mapping (含 tp_size/tp_rank)，默认 Mapping() 即 tp_size=1
 ├─ 2. shard_map = _build_shard_map(model_dir)        # key -> 分片文件绝对路径
 ├─ 3. 按「文件路径」把 key 分组 → path_to_keys        # 保证每个分片只 open 一次
 ├─ 4. 确定 strip_prefix / insert_prefix
 │      ├─ 若显式给了 key_prefix：strip = key_prefix, insert = ""
 │      └─ 否则：_detect_key_prefix(all_keys) 自动嗅探 VL/ASR/TTS 外层前缀
 ├─ 5. 对每个 (shard_path, keys)：
 │      for key in keys:
 │        tensor = 从分片读出 key 对应张量(.bin 用 torch.load / .safetensors 用 safe_open)
 │        mapped_key = _apply_prefix(key)              # 剥前缀
 │        mapped_key = key_remap(mapped_key) if key_remap else mapped_key
 │        if _set_tensor(model, mapped_key, tensor, mapping):  loaded++
 │        elif _try_split_fused_tensor(...):            loaded++   # 融合权重拆分兜底
 │        else: skipped++                                # 找不到对应模块就跳过
 ├─ 6. pre_repack_hook(model)  if 提供了钩子           # 例：reduce-vocab、DFlash lm_head
 ├─ 7. apply_all_repacking(model)                       # 量化权重重排（见 4.2）
 └─ 8. 若 tie_word_embeddings 且 lm_head 是 FP16Linear：tie_weights()
```

其中 step 5 是核心：每个 key 走「前缀剥离 → key remap → 赋值（或融合拆分）」三连。

**前缀剥离** 解决的是多模态检查点的命名嵌套问题。例如 InternVL3 把 LLM 权重藏在 `language_model.model.*` 下，而 EdgeLLM 的 `CausalLM` 参数树是 `model.*`，所以要剥掉 `language_model.` 这一层。`_detect_key_prefix` 用一组启发式规则按模型族判定 `(strip_prefix, insert_prefix)` 二元组。

**key remap** 解决的是 draft 模型（EAGLE3/MTP/DFlash）检查点张量名与目标模块名不一致的问题。例如 EAGLE3 draft 把单层藏在 `midlayer.` 下，要重映射成 `layers.0.`；同时跳过 `t2d`、`target_model.*` 这些训练侧产物。这些 remap 函数定义在 `model.py` 里（`_eagle3_key_remap` 等），通过 `key_remap` 参数透传。

**融合权重拆分**（`_try_split_fused_tensor`）是 step 5 的兜底分支：当 `_set_tensor` 失败时，检查这个 key 是不是某种「融合」权重，例如：
- PEFT/LoRA 检查点把基础权重嵌在 `module.base_layer.weight` 下——剥掉 `.base_layer.` 再试一次；
- `self_attn.qkv_proj.weight` 是把 q/k/v 三个投影拼成一个大矩阵——按 `num_attention_heads`/`num_key_value_heads` 切回三份；
- `mlp.gate_up_proj.weight` 是把 gate/up 拼起来——沿第 0 维对半切。

#### 4.1.3 源码精读

**公开入口 `load_weights`** 的签名与主循环：

[loader.py:59-91 — load_weights 的签名与参数说明](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/loader.py#L59-L91)

注意 `mapping` 参数——它驱动后续的张量并行切分；`pre_repack_hook` 是在「原始张量已就位、但还没 repack」这个时间窗口里执行的回调，用于需要看到原始量化布局的操作（如词表裁剪）。

主循环的关键段落（以 safetensors 分支为例）：

[loader.py:152-176 — 遍历分片读张量并赋值](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/loader.py#L152-L176)

最后是收尾三步——pre_repack_hook、apply_all_repacking、tie_weights：

[loader.py:178-194 — 触发 repack 与 tied embedding 处理](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/loader.py#L178-L194)

> tie_weights 的细节：当 `config.tie_word_embeddings=True` 时，HF 检查点不存 `lm_head.weight`（它复用 `embed_tokens.weight`）。但只有 `lm_head` 是 `FP16Linear` 时才能安全 tie——量化 lm_head 不能直接共享 embedding 表，所以这里做了类型守卫。

**分片映射 `_build_shard_map`**：

[loader.py:247-318 — 把检查点目录解析成 key→文件路径表](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/loader.py#L247-L318)

它支持四种布局，按优先级：单文件 safetensors > 多分片 safetensors（读 `weight_map`）> 单文件 `.bin`（PyTorch pickle）> 多分片 `.bin`。其中有一处值得注意的健壮性处理：如果索引指向的分片文件缺失、但单文件 `model.safetensors` 还在，就**忽略过时的索引**并回退到单文件，同时打 warning。

**逐张量赋值 `_set_tensor`**：

[loader.py:402-441 — 把张量写进指定 buffer/parameter](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/loader.py#L402-L441)

四步：(1) `_navigate` 按 `.` 分割的路径走模块树（整数段当 `ModuleList` 下标）；(2) `bfloat16 → float16`；(3) `_shard_for_module` 按 TP 切分；(4) 依次尝试 `_buffers` → `_parameters` → 普通 `setattr`。

**张量并行切分 `_shard_for_module`**：

[loader.py:376-399 — 按模块声明的 split_dim 切分张量](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/loader.py#L376-L399)

这里的设计很优雅：**loader 本身不知道每种 Linear 该怎么切**，它只是问模块 `module.tp_split_dim(attr)`——返回要切的维度（0 表示列切、1 表示行切）或 `None`（表示复制）。每个 Linear 子类自己拥有 TP 规则，loader 保持通用。例如 `ColumnParallelLinear.tp_split_dim` 返回 `0`（输出维切），`RowParallelLinear` 返回 `1`（输入维切），`ReplicatedLinear` 返回 `None`（全量复制）。底层切片原语是 `load_weight_shard`：

[loader.py:340-373 — load_weight_shard：按 tp_rank 取出本 rank 的切片](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/loader.py#L340-L373)

它还能利用 safetensors 的懒加载切片（`get_shape` 路径），只从磁盘读本 rank 那一段，而不是把整张读进内存再切。

**融合权重拆分 `_try_split_fused_tensor`**（节选 QKV 部分）：

[loader.py:444-541 — 剥离 base_layer、拆分 qkv_proj / gate_up_proj](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/loader.py#L444-L541)

QKV 拆分里有个细节：当 `tp_size>1` 时，`config.num_attention_heads` 已经是 per-rank 的头数，拆分时要乘回 `world` 才能对齐完整的检查点张量，随后每个子切片再在 `_set_tensor` 内部被重新切到本 rank。

#### 4.1.4 代码实践

**实践目标**：跟踪一个普通 FP16 权重从 safetensors 进入 `nn.Module` 的完整路径，写出步骤清单。

**操作步骤**：

1. 打开 [loader.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/loader.py)，定位 `load_weights`（L59）与 `_build_shard_map`（L247）。
2. 假设检查点目录里只有一个 `model.safetensors`（单文件），里面有一个 key `model.layers.0.self_attn.q_proj.weight`，dtype 为 `torch.bfloat16`，且 `tp_size=1`、无 `key_remap`、无 VL 前缀。
3. 按 `load_weights` 的执行顺序，写出这个张量被处理到最终写入 `module._parameters["weight"]` 的每一步。

**预期结果**（你需要复现的步骤清单）：

| 步骤 | 发生的事 | 关键函数 |
|------|---------|---------|
| 1 | `_build_shard_map` 发现 `model.safetensors` 存在、无 index，用 `safe_open().keys()` 读出所有 key，全部指向同一文件 | L282-287 |
| 2 | `_detect_key_prefix` 没匹配到任何 VL 前缀，返回 `("", "")` | L202-244 |
| 3 | 进入 safetensors 分支，`f.get_tensor(key)` 读出 `bfloat16` 张量 | L152-154 |
| 4 | `_apply_prefix` 不改 key（strip 为空） | L155 |
| 5 | 无 `key_remap`，跳过 | L159 |
| 6 | `_set_tensor`：`_navigate` 走到 `model.layers.0.self_attn.q_proj`，attr=`weight` | L417-419 |
| 7 | `tensor.dtype == bfloat16` → 转 `float16` | L423-424 |
| 8 | `_shard_for_module`：`tp_size==1` 直接返回原张量 | L386-388 |
| 9 | `attr in module._parameters` → 写入 `nn.Parameter(tensor, requires_grad=False)` | L431-433 |

**需要观察的现象**：步骤 7 的 dtype 转换是静默发生的——这也是为什么 EdgeLLM 选 `_set_tensor` 而非 `load_state_dict`（后者对 `q_proj` 这种 FP16 路径碰巧也能工作，但对量化路径会失败）。

> 若你手头有 GPU 和一个小的 Qwen3 检查点，可在 Python 里实际调用 `AutoModel.from_pretrained(model_dir)` 后 `print(dtype_summary(model))`（来自 [model.py:424](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L424-L430)）确认权重全部是 float16。若无环境，则以上为「待本地验证」的纯阅读型实践。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `load_weights` 在遍历 key 前要做 `path_to_keys` 分组（L95-97），而不是对每个 key 单独 open 一次分片？

**答案**：性能。一个分片文件里通常有几百个 key，反复 `safe_open`/`torch.load` 同一个文件会重复解析头部与磁盘 IO。按文件路径分组后每个分片只 open 一次，在循环内连续读出它名下的所有 key。

**练习 2**：如果一个 key 既不在任何 buffer/parameter 里、也不是融合权重，`load_weights` 会怎样？

**答案**：`_set_tensor` 返回 `False`，`_try_split_fused_tensor` 也返回 `False`，于是走到 `else` 分支记一条 `logger.debug("Key not found in model: %s", key)`，`skipped += 1`。不会报错——这是有意的鲁棒设计，让多余 key（如被 `num_decoder_layers` 截断的层、训练侧产物）被静默跳过。

**练习 3**：`_detect_key_prefix` 处理 Qwen3-VL-2B 时返回 `("model.language_model.", "model.")`。请解释为什么是「剥掉一段再插回一段」，而不是只剥掉。

**答案**：Qwen3-VL-2B 的检查点 key 形如 `model.language_model.embed_tokens.weight`，剥掉 `model.language_model.` 后剩下 `embed_tokens.weight`，但 EdgeLLM 的 `CausalLM` 参数树期望的是 `model.embed_tokens.weight`（顶层有个 `model` 子模块）。所以剥掉外层后还要插回一个 `model.`，才能让 key 对上模块树。

---

### 4.2 repacking：量化权重的格式重排

#### 4.2.1 概念说明

量化的核心矛盾是：**检查点作者打包权重的方式**，和 **GPU kernel 期望读取的布局**，通常不一样。

举个最直观的例子——AWQ：

- AWQ 把权重按 **输出通道** 方向打包：每个 `int32` 装 8 个 int4 nibble，分别对应 8 个连续的输出通道。布局记作 `[in, out//8] int32`。
- 但 EdgeLLM 的 int4 GEMM 插件（受 Marlin/CUTLASS 风格影响）期望的是另一套「swizzle」过的 `[out//2, in] int8` 布局——为了匹配 tensor core 的访存模式，需要对 K 轴分块置换、对 N 轴做偶奇交错、再把 4 个 nibble 塞进一个 int16。

所以加载完后，必须把 AWQ 的 `[in, out//8]` 解包成裸 nibble，**调整零点**，再重新打包成插件布局。这就是 repack。

同理，GPTQ 是**按输入通道**打包（行打包），NVFP4 用两半 nibble + FP8 block scale，ModelOpt 的 W4A16 预打包用 `uint8`——每种都要自己的重排规则。

还有一个贯穿所有 int4 方案的**零点折叠**数学，下面单独讲。

#### 4.2.2 核心流程

整个重排由 `apply_all_repacking(model)` 一次性驱动，它在 `load_weights` 收尾时被调用（[loader.py:181](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/loader.py#L181)）。它内部按固定顺序跑 6 趟扫描，每趟 `for module in model.modules()` 找特定类型的子模块就地改写其 buffer：

```text
apply_all_repacking(model)
 │
 ├─ 1. _stack_moe_experts(model)          # 必须最先：MoE 专家堆叠需要原始 GPTQ int32
 ├─ 2. _repack_awq_weights(model)         # AWQ: qweight 列打包 int32 -> 插件 int8
 ├─ 3. _repack_gptq_weights(model)        # GPTQ: qweight 行打包 int32 -> 插件 int8（+ 产出 int4_act_perm）
 ├─ 4. _cast_modelopt_awq_prepacked(model)# ModelOpt W4A16: uint8 预打包 -> 插件 int8 + scale 转置
 ├─ 5. _cast_fp8_linear_scales(model)     # FP8: 把 weight_scale/input_scale 统一转 float16
 └─ 6. _cast_nvfp4_weights(model)         # NVFP4: uint8 weight -> int8 view（位模式不变）
```

顺序很重要：`_stack_moe_experts` 必须在 GPTQ repack **之前**，因为 MoE 专家堆叠（Marlin/NVFP4 MMA）需要原始的 GPTQ `int32` 打包权重；它把每个专家的 qweight 提取出来重新打包后，会把 per-expert 的 `qweight` 置 `None`，这样后续 `_repack_gptq_weights` 遇到 `None` 就自动跳过。

每趟扫描都用 `isinstance(module, XxxLinear)` 做门控，所以对非量化模型（全是 FP16Linear）这 6 趟基本是空转，开销极小。

#### 4.2.3 源码精读

**调度入口 `apply_all_repacking`**：

[repacking.py:255-269 — 六趟扫描的固定顺序](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L255-L269)

**零点折叠的数学**（AWQ / GPTQ 共用同一思想）：

不同量化方案的「反量化公式」对零点的处理不同，而 int4 GEMM kernel 的反量化是固定的 `(nibble - 8) * scale`（即隐含零点为 8）。要在重排时把方案的零点「烤进」权重 nibble 里。

以 AWQ 为例（AWQ 反量化是 `(nibble - qzero) * scale`），要让两者等价：

\[
(\text{adjusted} - 8) \cdot s = (\text{nibble} - q_{\text{zero}}) \cdot s
\]

解得：

\[
\text{adjusted} = \text{nibble} - q_{\text{zero}} + 8
\]

GPTQ 同理，只是它还要处理「不同检查点存的是 `zero` 还是 `zero-1`」的歧义，多一个 `zero_point_offset`（默认 1）：

\[
\text{repacked} = \text{nibble} - (\text{stored\_zero} + \text{offset}) + 8
\]

这两段数学就分别落在 `repack_awq_to_plugin` 与 `repack_gptq_to_plugin` 里。

**AWQ 重排 `repack_awq_to_plugin`**：

[repacking.py:47-105 — AWQ qweight 解包、零点折叠、重打包](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L47-L105)

注意 L77 的 `_AWQ_BIT_TO_CH = [0, 2, 4, 6, 1, 3, 5, 7]`——AutoAWQ 打包时 8 个 nibble 的输出通道顺序是非顺序的（源自其 `AWQ_REVERSE_ORDER`），解包时必须按这个逆置换读出，否则输出通道会被打乱。零点折叠在 L96：`nibbles = (nibbles - zeros_expanded + 8).clamp(0, 15)`。

**GPTQ 重排 `repack_gptq_to_plugin`**：

[repacking.py:165-247 — GPTQ 行打包解包、g_idx 重排、零点折叠](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L165-L247)

GPTQ 比 AWQ 多两件事：(1) 它支持对称量化——有些检查点（如 Qwen3.5 int4）干脆不存零点（`qzeros` 是空的 `[num_groups, 0]`），此时隐含零点就是中点 8，折叠公式退化（L191-198）；(2) `desc_act` 模式下要按 `g_idx` 对输入通道（K 行）重排，并返回一个 `int4_act_perm` 索引，运行时在做 GEMM 前用它对激活做相同的 `index_select`（L236-247）。

**通用的 int4 打包核 `_pack_intweights`**：

[repacking.py:108-137 — nibble 到插件 swizzle 布局的四步置换](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L108-L137)

AWQ 和 GPTQ 在「裸 nibble `[N,K]`」之后的打包逻辑是共享的——四步：K 块内置换、组内偶奇重排、每 4 行跨 64 宽 K 条交错、4 个 nibble 装进一个 int16。这保证了两种方案最终落到同一套插件布局。

**ModelOpt W4A16 预打包 `_cast_modelopt_awq_prepacked`**：

[repacking.py:271-323 — uint8 解包到 nibble、转插件 int8、scale 转置到 fp16](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L271-L323)

ModelOpt 已经预打包成 `[N//2, K] uint8`（每字节两个 nibble），且用的是二补码掩码（`s & 0xF`，即 `-8→8, 0→0, 7→7`），所以要先把每字节拆成两个 nibble、做 `(u + 8) % 16` 转成插件约定的 `[0,15]`，再走同一个 `_pack_intweights`。

**NVFP4 视图转换 `_cast_nvfp4_weights`**（最简单的一趟）：

[repacking.py:339-351 — uint8 → int8 视图转换](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L339-L351)

NVFP4 的 FP4 nibble 位模式在 uint8 和 int8 下完全一样，这里只是 `view(torch.int8)`——因为「某些 ONNX 导入器对 UINT8 权重做 block DQ 时会处理出错，int8 没问题」。这是「绕过下游 bug」式的格式修正。

**NVFP4 MoE 专家堆叠**（进阶）：对于 MoE 模型，所有专家的权重还要进一步堆叠成 CuTeDSL 6D MMA 布局。这是 `repack_nvfp4_qwen3_moe_experts`（[L802](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L802)）和 `repack_nvfp4_nemotron_moe_experts`（[L879](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L879)）的职责，它们会先把 NVFP4 解码成稠密 fp32（`decode_modelopt_nvfp4`）、再重新量化打包。这部分与具体 GPU 架构（SM100/101/110 vs SM12x）强相关，本讲只点到为止，深度对接留到 [u8（插件与算子）](u8-l1-tensorrt-plugin-architecture.md)。

#### 4.2.4 代码实践

**实践目标**：对比 AWQ 与 NVFP4 在 repack 阶段的工作量差异。

**操作步骤**：

1. 打开 `repacking.py`，对比两个函数的行数与操作复杂度：
   - `_cast_nvfp4_weights`（L339-351）：只有 `view(torch.int8)` 一行实质操作。
   - `repack_awq_to_plugin`（L47-105）：解包 8 个 nibble、零点折叠、转置、`_pack_intweights` 四步置换。
2. 解释为什么两者差距这么大。

**预期结果**：

- **NVFP4 的 repack 极轻**：因为 NVFP4 的 FP4 nibble + FP8 block scale 的物理布局，ModelOpt 产出时就已经和插件期望的「权重方向」一致，repack 只需把 dtype 从 uint8 view 成 int8（绕过 ONNX 导入器的 uint8 bug），scale 的 6D MMA swizzle 留给 MoE 专家堆叠阶段做。
- **AWQ 的 repak 重**：因为 AWQ 的 `[in, out//8] int32` 列打包与插件的 swizzle 布局差异巨大，必须完整解包成裸 nibble、做零点折叠、再重新打包；而且零点折叠还要处理 AutoAWQ 的非顺序通道排列（`_AWQ_BIT_TO_CH`）。

**需要观察的现象**：这正是 EdgeLLM「量化与导出解耦」设计的一个体现——量化阶段只产出标准 HF 风格检查点（保留各家原始打包），所有「翻译成插件布局」的脏活都集中在加载后的 `apply_all_repacking` 这一个地方，按 `isinstance` 分发。新增一种量化格式，只要加一趟扫描函数即可，不动 loader 主线。

> 这是「待本地验证」的纯阅读型实践；若要动手，可在加载一个 AWQ 模型前后分别 dump 某 `AWQLinear.qweight` 的 `dtype` 和 `shape`，观察从 `int32 [in, out//8]` 变成 `int8 [out//2, in]`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `apply_all_repacking` 里 `_stack_moe_experts` 必须在 `_repack_gptq_weights` 之前？

**答案**：MoE 专家堆叠（Marlin / NVFP4 MMA 打包）需要读取**原始的 GPTQ `int32` 打包权重**才能正确解码每个专家的 nibble。如果先跑了 `_repack_gptq_weights`，per-expert 的 qweight 已经被转成 swizzle 过的插件 int8 布局，专家堆叠就拿不到正确的原始数据了。所以堆叠在前，且堆叠完会把 per-expert qweight 置 `None`，让后续 GPTQ repack 自动跳过。

**练习 2**：GPTQ 的 `repack_gptq_to_plugin` 返回了两个值 `(qw_out, perm)`，这个 `perm`（`int4_act_perm`）是干什么用的？

**答案**：GPTQ 的 `desc_act`（激活感知重排）模式下，权重的 K 行（输入通道）按 `g_idx` 分组重排过，反量化时激活也要做完全相同的重排才能对齐。`perm` 就是这个重排索引，它被存进 `module._buffers["int4_act_perm"]`，运行时在做 int4 GEMM 之前用 `x.index_select(-1, perm)` 对激活施加同样的置换。当 `g_idx` 是顺序的（即没重排），`perm` 是恒等置换，开销可忽略。

**练习 3**：`_cast_nvfp4_weights` 用 `w.view(torch.int8)` 而不是 `w.to(torch.int8)`，为什么？

**答案**：`view` 不复制数据、只重新解释同一段内存的字节；`to` 会做数值转换。NVFP4 的 FP4 nibble 在 uint8 和 int8 下位模式完全相同，要的是「换一种 dtype 标签」而非「换数值」，所以用 `view` 零拷贝完成。`to` 既慢又会改变某些 nibble 的解释值。

---

### 4.3 embedding_quantization：FP8 embedding sidecar

#### 4.3.1 概念说明

embedding 表（词嵌入矩阵）是个特殊的权重：它的形状是 `[vocab_size, hidden_size]`，vocab 通常几万到几十万，是模型里最大的几块权重之一。在边缘设备上，把它从 FP16 压成 FP8 能省一半显存。

但 embedding 表**不走 4.2 的线性层 repack 路径**——它不是 Linear，没有 `qweight`/`qzeros` 那一套，也不喂给 int4/FP4 GEMM 插件。它是在**导出阶段**被单独量化、单独写成一个 `embedding.safetensors` sidecar 文件，由 C++ 运行时在推理时按需反量化查表。

这就是 `embedding_quantization.py` 的职责：一个独立的、最小化的 FP8 E4M3 量化器，按 128 列分块带 per-block scale。

#### 4.3.2 核心流程

`quantize_embedding_to_fp8` 的流程很直白，对一个 `[V, H]` 的 embedding 表：

```text
1. 校验 H 能被 block_size(=128) 整除
2. reshape 成 [V, H/128, 128]
3. 对最后一维取 amax(绝对值最大) → 每个 [V, H/128] 块一个 amax
4. scale = amax / 448.0    （448 是 FP8 E4M3 的最大可表示值）
5. quantized = clamp(weight / scale, -448, 448)
6. 转成 torch.float8_e4m3fn，reshape 回 [V, H]
7. 返回 (embedding_fp8, scales [V, H/128])
```

其中 448 是 FP8 E4M3 格式的数值上界（`FP8_E4M3_MAX`），128 是分块大小（`FP8_EMBEDDING_BLOCK_SIZE`）。

数学上，对每个 128 列的块 \(b\)，设其元素为 \(w_i\)，则：

\[
a_{\max} = \max_i |w_i|, \qquad s_b = \frac{a_{\max}}{448}
\]

\[
\hat{w}_i = \mathrm{clamp}\!\left(\frac{w_i}{s_b},\ -448,\ 448\right)
\]

反量化时 \(w_i \approx \hat{w}_i \cdot s_b\)。分块越小、scale 越细，精度越高但额外存储越多；这里取 128 是精度与开销的折中（每 128 个 FP16 值压成 128 个 FP8 + 1 个 scale，约 2 倍压缩）。

#### 4.3.3 源码精读

**常量定义**：

[embedding_quantization.py:26-27 — FP8 上界与分块大小](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/embedding_quantization.py#L26-L27)

**量化函数主体**：

[embedding_quantization.py:30-57 — quantize_embedding_to_fp8 的分块量化](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/embedding_quantization.py#L30-L57)

几个细节值得注意：

- **`amax().clamp(min=1e-4)`**（L48）：防止全零块导致 scale=0、进而除零。给个极小下界。
- **全程用 fp32 计算**（L46 `weight.float()`）：量化是精度敏感操作，先升到 fp32 算 scale 和除法，最后才 `to(torch.float8_e4m3fn)`，避免中间用 fp16 累积误差。
- **scale 的形状是 `[V, num_groups]`**：和权重的 `[V, H]` 在 K 轴上对齐，反量化时按组广播。

**它在哪里被调用**：不在 loader 里，而在导出阶段写 sidecar 时。`checkpoint_utils.py` 在写出 `embedding.safetensors` 前，根据 `fp8_embedding` 开关决定走 FP8 还是 FP16：

[checkpoint_utils.py:843-857 — 导出阶段写出 FP8 embedding sidecar](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/checkpoint_utils.py#L843-L857)

写出的 sidecar 含两个张量：`embedding`（FP8）和 `embedding_scale`（per-block scale），C++ 运行时读这两个张量就能恢复近似原始 embedding。

**一个重要的例外**（承接大纲与 [u3-l3](u3-l3-quantized-weight-formats.md)）：draft 模型（EAGLE3 / MTP / Gemma4-MTP）**不写 embedding sidecar**，因为它们复用 base 模型的 embedding 表。这在 [checkpoint_utils.py:819-826](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/checkpoint_utils.py#L819-L826) 被显式跳过。

#### 4.3.4 代码实践

**实践目标**：理解 FP8 embedding 量化的精度损失来源。

**操作步骤**：

1. 读 [embedding_quantization.py:30-57](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/embedding_quantization.py#L30-L57)。
2. 写一小段示例代码（非项目原有，标注「示例代码」），手动模拟这个量化-反量化过程，观察量化误差：

```python
# 示例代码：手动验证 quantize_embedding_to_fp8 的精度
import torch

torch.manual_seed(0)  # 仅示例用；项目内禁用随机（见 AGENTS.md）
V, H = 4, 256          # 4 个词，hidden=256（能被 128 整除）
w = torch.randn(V, H)  # 模拟一段 embedding

# 按 block_size=128 分块量化
block = 128
groups = H // block
amax = w.view(V, groups, block).abs().amax(dim=-1).clamp(min=1e-4)
scale = amax / 448.0
q = (w.view(V, groups, block) / scale.unsqueeze(-1)).clamp(-448, 448)
q_fp8 = q.view(V, H).to(torch.float8_e4m3fn)

# 反量化
deq = q_fp8.view(V, groups, block).to(torch.float32) * scale.unsqueeze(-1)
deq = deq.view(V, H)

err = (deq - w).abs().mean()
print(f"mean abs error: {err.item():.4f}")   # 待本地验证具体数值
```

**需要观察的现象**：

- `scale` 的形状是 `[V, groups]`，即每个词的每 128 列各有自己的 scale——这就是「per-row block scale」。
- 反量化值 `deq` 与原始 `w` 有微小偏差，误差来自：(1) FP8 E4M3 只有 4 位指数+3 位尾数，离散等级稀疏；(2) clamp 截断超出 ±448 的值（极少触发）。
- 具体误差数值**待本地验证**（取决于数据分布；正态分布的 embedding 通常相对误差在 1% 量级）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 embedding 量化要在 fp32 下算 scale 和除法，而不是直接用 fp16？

**答案**：量化是精度敏感的——求 amax、做除法、再 clamp，每一步都可能引入舍入误差。fp16 只有约 3 位有效十进制精度，中间累积误差会让选出的 scale 偏差变大，进而放大反量化误差。先升到 fp32 计算、最后才转 fp8，是把数值精度损失集中到「不可避免的那一次 fp8 转换」上。

**练习 2**：FP8 embedding sidecar 和 4.2 里的线性层量化权重，在「谁来消费、何时量化」上有什么本质区别？

**答案**：
- **线性层量化权重**：在**加载阶段**（`load_weights` 之后）被 `apply_all_repacking` 重排，留在 `nn.Module` 的 buffer 里，随后随 ONNX 导出一起进入引擎构建，最终被 int4/FP4 GEMM 插件消费。
- **FP8 embedding**：在**导出阶段**（写 sidecar 时）才被 `quantize_embedding_to_fp8` 量化，单独写成 `embedding.safetensors`，不进入 ONNX 图，由 C++ 运行时在推理时单独加载、按需反量化查表。它不走任何 Linear 子类，也不经过 `apply_all_repacking`。

**练习 3**：`FP8_EMBEDDING_BLOCK_SIZE = 128`。如果把 block_size 调小（比如 32），精度和开销会怎么变？

**答案**：block 越小，每个 scale 覆盖的元素越少、scale 越贴合局部数值范围，**量化精度越高**；但代价是 scale 张量变大（`H/block` 增大），sidecar 文件变大、反量化时的 scale 查找开销也增加。128 是 EdgeLLM 选定的精度与开销折中点。

---

## 5. 综合实践

**任务**：为「一个混合精度 LLM 的权重加载全链路」画一张端到端的数据流图，并标注每一步的输入输出 dtype 与负责的函数。

假设模型配置为：backbone 是 NVFP4 量化，`lm_head` 是 FP8 量化，`tie_word_embeddings=False`，`tp_size=2`，且开启了 FP8 embedding sidecar。

**要求**：

1. 从 `AutoModel.from_pretrained(model_dir)` 开始，画出以下阶段：
   - (a) 构造空 `nn.Module`（来自 u2-l2/u2-l3）；
   - (b) `load_weights` 主循环（前缀剥离、TP 切分、`_set_tensor`）；
   - (c) `apply_all_repacking` 的六趟扫描；
   - (d) 导出阶段写 embedding sidecar。
2. 在图上标注三类权重的 dtype 演变：
   - backbone NVFP4 线性层：`uint8`（检查点）→ `_cast_nvfp4_weights` → `int8`（buffer）；
   - FP8 `lm_head`：`fp8` 权重 + scale → `_cast_fp8_linear_scales` → scale 转 `float16`；
   - embedding：`fp16` → `quantize_embedding_to_fp8` → `fp8` + `scale`（sidecar）。
3. 在 TP 维度上标注：backbone 的 `ColumnParallelLinear` 沿 dim 0 切（输出维），`RowParallelLinear` 沿 dim 1 切（输入维），每个 rank 只持有自己那一份。

**验证方式**：画完后，对照本讲三个模块的源码逐条检查——你的图里每个箭头都应该能在 loader.py 或 repacking.py 里找到对应的函数与行号。如果画不出某一步，回到对应小节重读。

> 这是纯设计型实践，不需要运行代码。它的目的是把 loader / repacking / embedding_quantization 三个模块在「dtype 演变」与「TP 切分」两条线索上串起来，为下一讲 [u2-l5（ONNX 导出）](u2-l5-onnx-export-custom-ops-dynamo.md) 做铺垫——因为导出阶段消费的正是这些被 repack 过、被切分过的 buffer。

## 6. 本讲小结

- `load_weights` 是权重加载主线：`_build_shard_map` 定位分片 → `_detect_key_prefix`/`key_prefix` 剥离多模态前缀 → `key_remap` 重命名 draft 检查点的 key → `_set_tensor` 赋值 → 收尾触发 repack 与 tie。
- 关键设计：**用 `_set_tensor` 直接写 buffer/parameter，而非 `load_state_dict`**，以原样保留 `fp8`/`uint8`/`bfloat16` 等量化与特殊 dtype，并顺手做 `bf16→fp16` 转换。
- `_try_split_fused_tensor` 兜底处理三类融合权重：PEFT `base_layer` 嵌套、`qkv_proj` 三合一、`gate_up_proj` 二合一。
- 张量并行在赋值时就地完成：loader 通用、问 `module.tp_split_dim(attr)` 拿到切分维度，每个 Linear 子类自管规则；还能利用 safetensors 懒加载只读本 rank 切片。
- `apply_all_repacking` 按固定顺序跑六趟扫描，把 AWQ/GPTQ/NVFP4/ModelOpt 的检查点打包布局翻译成 C++ 插件期望的 swizzle 布局；其中 AWQ/GPTQ 都用「零点折叠」数学（`nibble - zero + 8`）把方案零点烤进权重。
- embedding 表不走线性层 repack，而是在导出阶段被 `quantize_embedding_to_fp8` 单独量化成 FP8 E4M3（128 列分块带 per-block scale），写成 `embedding.safetensors` sidecar；draft 模型复用 base 的 embedding 故跳过。

## 7. 下一步学习建议

- **紧接着读 [u2-l5（ONNX 导出：自定义算子与 Dynamo 翻译）](u2-l5-onnx-export-custom-ops-dynamo.md)**：本讲产出的「被 repack 过、被 TP 切分过的 buffer」正是 ONNX 导出阶段 trace 的对象。你会看到 `trt::`/`trt_edgellm::` 自定义算子如何消费这些 buffer，以及为什么导出图必须依赖加载阶段的格式契约。
- **配合 [u3 单元（量化）](u3-l1-quantization-package-design.md)**：本讲只讲了「加载时如何 repack」，而量化检查点本身是怎么产出的（配方、AWQ vs NVFP4 的训练侧流程）在 u3 详述。读 u3-l3 能补全「量化权重格式与 sidecar」的全貌。
- **深入 MoE 专家堆叠**：本讲点到为止的 `_stack_moe_experts` / `repack_nvfp4_*_moe_experts` 涉及大量 SM 架构相关的 swizzle，留到 [u8-l1（TensorRT 插件架构）](u8-l1-tensorrt-plugin-architecture.md) 与 [u8-l2（自定义 CUDA 算子）](u8-l2-custom-cuda-kernels.md) 再展开。
- **想动手验证**：找一个小的 NVFP4 量化检查点，在 `load_weights` 前后各加一行日志打印某个线性层的 `qweight.dtype` 与 `shape`，亲眼看到 `apply_all_repacking` 前后的变化。
