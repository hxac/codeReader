# 量化权重格式与 sidecar

## 1. 本讲目标

学完本讲后，你应当能够：

1. 说出 `int4_awq`、`int4_gptq`、`int4_awq_modelopt`、`nvfp4` 四种量化检查点在 safetensors 里的**张量布局**各是什么样子（dtype、shape、打包方向）。
2. 描述 `repacking.py` 的 `apply_all_repacking` 用怎样的**固定顺序**把这些检查点布局「翻译」成 C++ 插件期望的 swizzled 布局，并解释为什么顺序不能乱。
3. 理解 FP8 embedding sidecar（`embedding.safetensors`）的作用、它的 per-row 分块量化原理，以及为什么 TTS 的 talker / code_predictor 不支持它。
4. 说清楚 EAGLE3 与 DFlash 两种投机解码 draft 模型为什么要有**独立于** transformers 的校准实现，以及它们各自把哪些权重从 base 模型「借」过来。

本讲是 u3（量化）单元的收尾篇，承接 u2-l4（检查点加载与权重重排）里已经讲过的 `load_weights → apply_all_repacking` 主线，把镜头拉近到「重排」与「sidecar」这两个最贴近位运算的细节上。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**为什么要「重排」（repack）。** 量化后的权重在磁盘上是**为通用框架设计的紧凑布局**（例如 8 个 4-bit 权重塞进一个 int32），而 C++ 运行时里的 TensorRT 插件（int4 GEMM、NVFP4 MoE）为了喂给特定的 tensor core，要求权重按**自己的 swizzle 顺序**排列。这两套布局的「数据」相同、「排布」不同。所以 EdgeLLM 在加载检查点之后、导出 ONNX 之前，必须做一次纯 CPU 的位运算翻译。`repacking.py` 的开头注释把这件事讲得很清楚：

> [tensorrt_edgellm/checkpoint/repacking.py:15-23](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L15-L23) — 把检查点格式（AWQ 列打包 int32、GPTQ 行打包 int32、ModelOpt uint8、ModelOpt NVFP4）翻译成 per-plugin 布局，全部在 `module._buffers` 上原地操作，由 `loader.load_weights` 在所有权重赋值完成后调用。

**什么是 sidecar。** 它是「跟随在主 ONNX 图旁边的一份独立数据文件」。词嵌入表（embedding table）就是典型的 sidecar：C++ 运行时不在图里跑 embedding，而是直接从 `embedding.safetensors` 里按 token id 查表。`embedding_quantization.py` 负责把这张表从 FP16 进一步压成 **FP8 E4M3**，省一半显存。

**零点折叠（zero-point folding）。** AWQ/GPTQ 这类**非对称**量化会为每组权重存一个零点 `qzero`，反量化公式是 `(nibble - qzero) * scale`；而 EdgeLLM 的 int4 GEMM kernel 用的是对称约定 `(nibble - 8) * scale`。为了让两者对齐，重排时会把零点「烤」进权重本身。这个技巧会贯穿整篇讲义，先记住它的名字。

> 本讲需要读者已经读过 **u2-l4**（检查点加载与权重重排）了解 `load_weights` / `_set_tensor` / buffer 的概念，以及 **u3-l1 / u3-l2**（量化包设计与 CLI）了解 `fp8` / `int4_awq` / `nvfp4` 等量化常量的含义。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用到的关键符号 |
|------|------|-------------------|
| `tensorrt_edgellm/checkpoint/repacking.py` | 量化权重的位运算翻译中枢 | `apply_all_repacking`、`repack_awq_to_plugin`、`repack_gptq_to_plugin`、`_cast_modelopt_awq_prepacked`、`decode_modelopt_nvfp4`、`repack_nvfp4_qwen3_moe_experts` |
| `tensorrt_edgellm/checkpoint/embedding_quantization.py` | FP8 词嵌入量化 | `quantize_embedding_to_fp8` |
| `tensorrt_edgellm/checkpoint/checkpoint_utils.py` | sidecar 落盘编排 | `write_runtime_sidecars` 里的 embedding 分支、`_runtime_embedding_scale` |
| `tensorrt_edgellm/checkpoint/loader.py` | 调用 repacking 的上层入口 | `load_weights`（调用 `apply_all_repacking` 的位置） |
| `tensorrt_edgellm/scripts/export.py` | FP8 embedding 开关与 TTS 排除 | `--fp8-embedding` 参数处理、thinker 限制警告 |
| `tensorrt_edgellm/quantization/models/eagle3_draft.py` | EAGLE3 draft 独立校准模型 | `Eagle3DraftModel`、`quantize_and_export_draft`、`_remap_keys`、`_fill_embedding` |
| `tensorrt_edgellm/quantization/models/dflash_draft.py` | DFlash draft 独立校准模型 | `DFlashCalibDraftModel`、`quantize_and_export_dflash_draft`、`_disable_dflash_fc_quantization` |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：4.1 int4（AWQ/GPTQ/ModelOpt-AWQ）检查点布局与重排主流程；4.2 NVFP4 权重格式与 MoE 专家重排；4.3 FP8 embedding sidecar；4.4 量化 draft 模型。

### 4.1 int4 量化权重的检查点布局与重排主流程

#### 4.1.1 概念说明

`int4`（每个权重用 4 bit 表示）是边缘设备最常用的省显存方案。但「int4」只是一个位宽，**不同量化工具存出来的张量形态完全不同**。EdgeLLM 至少要吃三种 int4 检查点：

- **AWQ（AutoAWQ）**：权重张量 `qweight` 形状是 `[in_features, out_features // 8]`、`dtype=int32`，沿**输出轴**把 8 个 nibble 打包进一个 int32；零点 `qzeros` 形状 `[in // group, out // 8]` int32。
- **GPTQ**：权重 `qweight` 形状是 `[in_features // 8, out_features]`、`dtype=int32`，沿**输入轴（K 轴）**把 8 个 nibble 打包进一个 int32；可能还带一个 `g_idx`（`desc_act` 时的分组索引）。
- **ModelOpt 预打包 AWQ（`int4_awq_modelopt`）**：权重已经是 `uint8`、形状 `[out // 2, in]`（每字节 2 个 nibble），是 NVIDIA ModelOpt **已经打包好**的形态，EdgeLLM 不需要重新解包零点，只需再做一次 swizzle。

C++ 运行时的 int4 GEMM 插件（`Int4GroupwiseGemm`）期望的是另一种 swizzled 布局：`[out // 2, in]` 的 `int8`，且内部按 K 块、even/odd、行交错的方式重排，好喂给 tensor core。所以无论哪种来源，最终都要落到这一套**插件布局**。

#### 4.1.2 核心流程

整条重排主线由一个总函数 `apply_all_repacking` 编排，它定义了**固定六趟扫描的顺序**：

```
apply_all_repacking(model):
  1. _stack_moe_experts(model)        # MoE 专家堆叠，必须在 GPTQ repack 之前
  2. _repack_awq_weights(model)       # AWQ int32 → swizzled int8
  3. _repack_gptq_weights(model)      # GPTQ int32 → swizzled int8
  4. _cast_modelopt_awq_prepacked(model)  # ModelOpt uint8 → swizzled int8
  5. _cast_fp8_linear_scales(model)   # FP8 线性层 scale 转 fp16
  6. _cast_nvfp4_weights(model)       # NVFP4 uint8 → int8 view-cast
```

顺序里有几个关键约束：

- **MoE 专家堆叠必须最先跑**。因为 GPTQ 的 MoE 路径需要**原始的 int32 打包权重**，一旦第 3 步把它转成了 swizzled int8，专家堆叠就拿不到原始数据了。堆叠完后，每个专家的 `qweight` 被设成 `None`，这样第 3 步的常规 GPTQ repack 会自动跳过它们。
- 每趟扫描都用 `isinstance(module, XXXLinear)` 做**门控**，所以对非量化的 FP16 子编码器跑这套函数是安全的（每个 fixup 都会被门挡掉）。

而 `apply_all_repacking` 本身是被 `load_weights` 在所有权重赋值完成之后调用的：

> [tensorrt_edgellm/checkpoint/loader.py:178-189](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/loader.py#L178-L189) — 先跑可选的 `pre_repack_hook`，再 `apply_all_repacking(model)`，最后做 `tie_weights()`。这就是「先灌权重，再重排」的总顺序。

#### 4.1.3 源码精读

**总编排函数**——记住这六行的顺序就等于记住了本模块的骨架：

> [tensorrt_edgellm/checkpoint/repacking.py:255-268](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L255-L268) — `apply_all_repacking`，注释里明确写了 MoE 专家堆叠必须 FIRST，因为它需要 GPTQ 原始 int32 权重。

**AWQ 的核心难点：零点折叠。** AWQ 反量化是 `(nibble - qzero) * scale`，kernel 是 `(nibble - 8) * scale`。要让它俩相等，只需令重排后的 `adjusted_nibble = nibble - qzero + 8`：

\[
\text{kernel 结果} = (\text{adjusted\_nibble} - 8)\cdot\text{scale} = (\text{nibble} - \text{qzero})\cdot\text{scale} = \text{AWQ 结果}
\]

源码里这一行就是折叠动作：

> [tensorrt_edgellm/checkpoint/repacking.py:94-96](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L94-L96) — `nibbles = (nibbles - zeros_expanded + 8).clamp(0, 15)`，把每组的 qzero 烤进权重，并把结果钳到 4-bit 范围。

AWQ 还有一个坑：AutoAWQ 往一个 int32 里塞 8 个 nibble 时，用的是**非顺序**的输出通道排列（`AWQ_REVERSE_ORDER = [0,4,1,5,2,6,3,7]`）。源码用一张逆置换表 `_AWQ_BIT_TO_CH = [0,2,4,6,1,3,5,7]` 把每个 bit 位置对应的真实输出通道还原出来：

> [tensorrt_edgellm/checkpoint/repacking.py:77-82](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L77-L82) — `_AWQ_BIT_TO_CH` 与 nibble 提取循环，注释说明若不做这一步，每 8 个一组内的输出通道会被打乱。

**swizzle 的真正细节** `_pack_intweights`：折叠完零点、转成 `[N, K]`（N=输出, K=输入）后，还要做四步 numpy 变换——K 块内 permute、8 宽 even/odd 重排、4 行跨 64 宽 K 条带交错、最后 4 个 nibble 压一个 int16：

> [tensorrt_edgellm/checkpoint/repacking.py:108-137](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L108-L137) — `_pack_intweights`，AWQ 与 GPTQ 共用这一步把 `[N,K]` nibble 压成插件布局 `[N//4, K]` int16（再 view 成 `[N//2, K]` int8）。

**GPTQ 的两个额外难点。** GPTQ 沿 K 轴打包（`nibbles[k::8, :] = (qw >> (4*k)) & 0xF`），并且：(1) 有些对称 GPTQ 检查点（如 Qwen3.5 int4）**完全不存零点**，`qzeros` 是空的 `[num_groups, 0]`，此时当作对称量化、隐式零点取 8；(2) `desc_act` 场景下 `g_idx` 决定 K 行的真实分组顺序，需要按 `_gather_rows_by_gidx_order` 把同组的通道聚到一起，并返回一个激活置换 `int4_act_perm` 让运行时在 GEMM 前先 `index_select` 激活：

> [tensorrt_edgellm/checkpoint/repacking.py:190-200](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L190-L200) — 对称 GPTQ 检查点的判别与 `num_groups` 推断。
>
> [tensorrt_edgellm/checkpoint/repacking.py:232-247](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L232-L247) — GPTQ 零点偏移调整与 `g_idx` 行重排，返回 `(qweight_out, int4_act_perm)`。

**ModelOpt 预打包 AWQ 走的是更短的路径。** 因为权重已经是 `uint8[N//2, K]`（两 nibble 一字节），且零点已经按补码约定处理过，所以这里只需「解 2 nibble/byte → 补码转插件约定 → `_pack_intweights` → swizzled int8」，再把 `weight_scale` 转置成 `[K//g, N]` fp16：

> [tensorrt_edgellm/checkpoint/repacking.py:293-306](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L293-L306) — ModelOpt uint8 权重的解包与 `nibbles = (nibbles + 8) % 16` 的补码→插件约定转换。

#### 4.1.4 代码实践

**实践目标：** 对照源码，写出 AWQ 检查点在导出阶段需要哪些 repack 步骤，并验证零点折叠的数学。

**操作步骤：**

1. 打开 `repacking.py`，找到 `_repack_awq_weights`（[第 354-370 行](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L354-L370)），它门控 `AWQLinear` 且要求 `qweight.dtype == int32`。
2. 顺着它进入 `repack_awq_to_plugin`（[第 47-105 行](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L47-L105)），按顺序列出：① 用 `_AWQ_BIT_TO_CH` 提取 nibble；② 提取并广播 qzero；③ `nibbles - zeros + 8` 折叠零点；④ 转置到 `[N,K]`；⑤ `_pack_intweights` swizzle；⑥ view 成 int8。
3. **数学验证（可在本地用纯 Python 做，无需 GPU）：** 假设某组 `qzero=9`，某个 `nibble=5`，`scale=0.01`。
   - AWQ 原始反量化：`(5 - 9) * 0.01 = -0.04`。
   - 折叠后 `adjusted = 5 - 9 + 8 = 4`；kernel 反量化：`(4 - 8) * 0.01 = -0.04`。两者相等即验证通过。

**需要观察的现象 / 预期结果：** 手算的两个反量化结果必须完全一致（都是 `-0.04`），否则说明你对零点折叠方向理解反了。AWQ 的 `qweight` 形状会从 `[in, out//8]` int32 变成 `[out//2, in]` int8。

> 若想真正跑一遍，可写一段示例代码（**非项目原有代码**）：构造一个极小的 `qweight`/`qzeros` int32 张量，调用 `repack_awq_to_plugin`，打印前后 shape 与 dtype。能否在缺 GPU 的机器上运行 = **待本地验证**（该函数纯 CPU/numpy，理论上可跑）。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `apply_all_repacking` 要把 `_stack_moe_experts` 放在 `_repack_gptq_weights` 之前？
**答案：** MoE 专家堆叠需要 GPTQ **原始的 int32 打包权重**才能正确抽取每个专家的权重；一旦常规 GPTQ repack 把 `qweight` 转成了 swizzled int8，原始打包信息就丢了。堆叠后会把每个专家的 `qweight` 置 `None`，让后续常规 repack 跳过它们。

**练习 2：** 一个对称 GPTQ 检查点（`qzeros` 为空）走 `repack_gptq_to_plugin` 时，零点折叠会发生什么？
**答案：** 源码把对称情形的隐式零点设为 `8 - zero_point_offset`，使 `nibbles - zeros_expanded - zero_point_offset + 8` 恰好抵消成恒等（no-op），相当于「对称量化本来就和 kernel 的 `(nibble-8)` 约定一致，无需重映射」。见 [第 210-215 行](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L210-L215)。

**练习 3：** AWQ 和 GPTQ 的 `qweight` 分别沿哪个轴打包 8 个 nibble？
**答案：** AWQ 沿**输出轴**打包（`[in, out//8]`），GPTQ 沿**输入轴（K 轴）**打包（`[in//8, out]`）。这正是两者提取 nibble 的循环写法不同的原因。

---

### 4.2 NVFP4 权重格式与 MoE 专家重排

#### 4.2.1 概念说明

**NVFP4（4-bit 浮点，E2M1）** 和 int4 是两种完全不同的「省位宽」思路：int4 是均匀定点（等距网格），NVFP4 是**浮点**（指数+尾数，网格随幅度变化）。它的正数表示级别是：

\[
\{0,\ 0.5,\ 1.0,\ 1.5,\ 2.0,\ 3.0,\ 4.0,\ 6.0\}
\]

加上 1 个符号位，正好 4 bit。NVFP4 检查点由 ModelOpt 产出，每个权重张量由三部分组成：

- `weight`：`uint8` / `int8`，形状 `[out, in // 2]`（每字节 2 个 FP4 nibble）。
- `weight_scale`：FP8 E4M3 的**分块 scale**，形状 `[out, in // group_size]`，`group_size=16`。
- `weight_scale_2`：`[1]` 的 fp32，是「scale 的 scale」（super-scale / alpha）。

真实反量化值 = `E2M1_level × fp8_block_scale × weight_scale_2`。这是一种**两级缩放**结构，精度比单纯 int4 高、对异常值更友好，是 Thor/Spark 等 FP8 平台的主力格式。

#### 4.2.2 核心流程

NVFP4 在 EdgeLLM 里有两种用法，对应两条 repack 路径：

1. **普通 NVFP4 线性层**（非 MoE）：路径很短——只是把 `weight` buffer 从 `uint8` view-cast 成 `int8`，因为「打包后的 FP4 nibble 在两种 dtype 下位模式相同」，而有些 ONNX 导入器对 `UINT8` 权重 initializer 在 block DQ 时处理有 bug，int8 更稳。
2. **NVFP4 MoE 专家**：路径很长——要先把每个专家的 gate/up/down 投影**反量化成 dense fp32**，再**重新量化打包**成 CuTeDSL 插件期望的 6D MMA 布局，还要处理 SwiGLU 的 up/gate 行交织、以及对齐 padding。

MoE 这条路必须「先 decode 再 encode」的原因是：检查点里每个专家是独立 NVFP4 线性层，而 MoE 插件要把所有专家**堆叠**成一个 `[E, ...]` 张量并用统一的 MMA 布局喂给 tensor core，两套布局无法直接对齐，只能经过 dense 中间态。

#### 4.2.3 源码精读

**普通线性层的最简处理：**

> [tensorrt_edgellm/checkpoint/repacking.py:339-351](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L339-L351) — `_cast_nvfp4_weights`，仅做 `w.view(torch.int8)`，注释解释了为何要从 uint8 改成 int8。

**反量化函数 `decode_modelopt_nvfp4`**（MoE 重排的第一步全靠它）：把每字节拆成高低 nibble，用 `_FP4_E2M1_POSITIVE_LEVELS` 查表得幅度、拼符号位，再乘以 FP8 block scale 和 super-scale：

> [tensorrt_edgellm/checkpoint/repacking.py:620-671](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L620-L671) — `decode_modelopt_nvfp4`，关键三行：`values = _FP4_E2M1_POSITIVE_LEVELS[magnitude]`（查表）、`dense = values_grouped * ws_fp32`（乘 block scale）、`dense *= ws2`（乘 super-scale）。

其中 FP4 的正数级别表与量化边界（用于 `searchsorted` 反查）：

> [tensorrt_edgellm/checkpoint/repacking.py:613-617](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L613-L617) — `_FP4_E2M1_POSITIVE_LEVELS` 与 `_E2M1_BOUNDS`。

**重新量化打包 `_pack_nvfp4_moe_weight`**（MoE 重排的第二步）：把 dense 权重过一遍 BF16 舍入（`_round_dense_to_bf16`），按 16 宽分块算 `block_scale = max(|block|) / 6.0`，用 `searchsorted` 反查每个值对应的 nibble，高低 nibble 合字节，再把 FP8 scale 字节做 6D MMA swizzle：

> [tensorrt_edgellm/checkpoint/repacking.py:703-740](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L703-L740) — `_pack_nvfp4_moe_weight`，注释点明返回 `qweights [M,K/2] int8` 与 `blocks_scale [m_tiles,k_tiles,32,4,4] int8`（CuTeDSL MMA 物理布局）。

6D MMA swizzle 把线性 FP8 scale 重排成 `[m_tiles, k_tiles, 32, 4, 4]`：

> [tensorrt_edgellm/checkpoint/repacking.py:680-700](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L680-L700) — `_swizzle_nvfp4_mma_scales`。

**两个真实模型的差异。** `repack_nvfp4_qwen3_moe_experts`（Qwen3-MoE，SwiGLU）会把 gate/up 在 FC1 的 M 轴上做 **64 行 up/gate 交织**（`fc1_layout="interleave"`，给 SM100/101/110 的 `Nvfp4MoePlugin`）或**整体 concat**（`fc1_layout="concat"`，给 SM12x 的 `NvFP4MoEPluginGeforce`）：

> [tensorrt_edgellm/checkpoint/repacking.py:802-838](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L802-L838) — `repack_nvfp4_qwen3_moe_experts`，`fc1_layout` 参数区分两类 GPU 的插件。

而 Nemotron-H 的路由专家是 ReLU2 MLP（无 gate 投影），且要求 FC1 的 N 轴补齐到 128 的倍数、SM12x 还要求 K 轴（hidden_size）补齐到 256 的倍数，靠 `relu2(0)=0` 让零填充行/列贡献为零：

> [tensorrt_edgellm/checkpoint/repacking.py:879-913](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L879-L913) — `repack_nvfp4_nemotron_moe_experts`，docstring 详细解释了对齐 padding 的数学依据。

#### 4.2.4 代码实践

**实践目标：** 对比 AWQ 与 NVFP4 在检查点中的张量布局，并写出两者在导出阶段各自需要的 repack 步骤。

**操作步骤：**

1. 在本讲的 4.1 与 4.2 里各挑一个代表性张量，填下表（**示例代码·待本地验证的形状**）：

   | 格式 | 代表 buffer | 检查点 dtype / shape | 关联 scale | 导出阶段 repack 步骤 |
   |------|------------|---------------------|-----------|---------------------|
   | int4_awq | `qweight` | int32 `[in, out//8]` | `scales` `[in//g, out]`、`qzeros` | ① 提取 nibble（`_AWQ_BIT_TO_CH`）② 折叠 qzero ③ `_pack_intweights` swizzle → `[out//2, in]` int8 |
   | nvfp4（普通 Linear） | `weight` | uint8 `[out, in//2]` | `weight_scale` FP8 `[out, in//16]`、`weight_scale_2` `[1]` | 仅 `view(int8)`（位模式不变） |
   | nvfp4（MoE 专家） | 每专家 `weight` | uint8 `[out, in//2]` | 同上 | ① `decode_modelopt_nvfp4` 反量化到 dense ② `_pack_nvfp4_moe_weight` 重量化+6D MMA swizzle ③ 按 FC1 layout 交织/concat 并 stack 成 `[E,...]` |

2. 阅读两处注释确认细节：普通路径 [第 339-351 行](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L339-L351)、MoE 路径 [第 802-876 行](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L802-L876)。

**需要观察的现象 / 预期结果：** 你应能总结出——AWQ 走的是「打包 int32 → swizzled int8 + 零点折叠」，是一次**纯整数位运算**；而 NVFP4 MoE 走的是「打包 FP4 → dense → 重新打包 FP4 + MMA swizzle」，因为要先解出浮点值才能正确堆叠专家。两者的复杂度差距很大。

> 是否需要在真实 GPU 上跑：本表只需阅读源码即可完成，**无需 GPU**。

#### 4.2.5 小练习与答案

**练习 1：** 为什么普通 NVFP4 线性层只需 `view(int8)`，而 NVFP4 MoE 专家却要 decode 再 encode？
**答案：** 普通线性层里权重以**单个张量**形式直接进 ONNX initializer，插件读它时位模式不变，所以 dtype view 即可；MoE 插件要把所有专家堆成 `[E, ...]` 且用统一的 CuTeDSL 6D MMA 布局，检查点的逐专家布局与之不兼容，必须经 dense 中间态重新打包（见 [第 703-740 行](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L703-L740)）。

**练习 2：** NVFP4 的「两级缩放」分别指什么？
**答案：** 第一级是 per-16-element 的 FP8 E4M3 block scale（`weight_scale`），第二级是 per-tensor 的 fp32 super-scale（`weight_scale_2`，又称 alpha）。最终值 = `E2M1_level × block_scale × super_scale`。

**练习 3：** Nemotron-H MoE 为什么要把 FC1 的 N 轴补齐到 128 的倍数？
**答案：** 两个 NVFP4 MoE kernel 都要求 FC1 N 轴是 128 的倍数才能对齐 MMA tile；靠 `relu2(0)=0` 使零填充行产生零中间激活、FC2 零填充 M 行产生零输出贡献，调用方再切掉，数学上等价（见 [第 896-912 行](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L896-L912)）。

---

### 4.3 FP8 embedding sidecar

#### 4.3.1 概念说明

词嵌入表 `embed_tokens.weight` 是 LLM 里最大的一块「非计算」权重（`[vocab_size, hidden_size]`，动辄几亿参数）。EdgeLLM 的 C++ 运行时不把 embedding 放进 ONNX 图，而是单独写成 `embedding.safetensors` 这个 **sidecar**，运行时按 token id 直接查表（见 u2-l4）。

在 FP8 平台上，可以把这张表再压成 **FP8 E4M3**（每元素 1 字节，比 FP16 省一半），代价是引入一小点量化误差。关键是 FP8 E4M3 的表示范围有限（最大 ±448），整行元素幅度差异大时，逐元素量化会溢出。所以 EdgeLLM 采用**沿 hidden 维分块的 per-row scale**：每 128 个 hidden 元素一组，各自算一个 scale，把任意幅度的权重都拉进 `[-448, 448]`。

#### 4.3.2 核心流程

量化公式（per-row block，块大小 `B=128`，FP8 E4M3 最大值 `448`）：

\[
\text{amax}_{v,g} = \max_{j\in[gB,(g+1)B)} |W_{v,j}|,\qquad
s_{v,g} = \frac{\text{amax}_{v,g}}{448}
\]

\[
W^{\text{fp8}}_{v,j} = \text{clamp}\!\left(\frac{W_{v,j}}{s_{v,\,\lfloor j/B\rfloor}},\ -448,\ 448\right)\ \text{（存成 float8\_e4m3fn）}
\]

每个词 `v` 有 `hidden_size / 128` 个 scale。sidecar 文件里同时存 `embedding`（FP8）和 `embedding_scale`（fp32），运行时反量化查表：`row = embedding[v] * embedding_scale[v]`。

落盘的决策点在 `write_runtime_sidecars`（`checkpoint_utils.py`）：

```
if 模型是 draft（eagle3 / mtp / gemma4_mtp）:
    跳过 embedding.safetensors（复用 base 的）
else if embed_tokens 存在:
    取 weight，乘 _runtime_embedding_scale（如有）
    cast 到 fp16（运行时只认 fp16 或 fp8）
    if fp8_embedding:
        quantize_embedding_to_fp8 → 存 {"embedding", "embedding_scale"}
    else:
        存 {"embedding"}（纯 fp16）
```

而 `fp8_embedding` 这个开关来自 CLI 的 `--fp8-embedding`，但它**只对 LLM thinker 生效**。

#### 4.3.3 源码精读

**量化函数本身**——分块、求 amax、除以 448、clamp、cast：

> [tensorrt_edgellm/checkpoint/embedding_quantization.py:30-57](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/embedding_quantization.py#L30-L57) — `quantize_embedding_to_fp8`，常量 `FP8_E4M3_MAX = 448.0`、`FP8_EMBEDDING_BLOCK_SIZE = 128` 见 [第 26-27 行](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/embedding_quantization.py#L26-L27)。

**落盘分支**——注意 draft 模型的跳过逻辑：

> [tensorrt_edgellm/checkpoint/checkpoint_utils.py:816-857](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/checkpoint_utils.py#L816-L857) — draft（eagle3/mtp/gemma4_mtp）跳过 embedding sidecar；其余模型按 `fp8_embedding` 决定写 FP8 还是 FP16。`_runtime_embedding_scale` 在 [第 749 行](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/checkpoint_utils.py#L749) 定义，把某些模型的 embedding scale 因子折进表里。

**TTS 的限制**——这是本模块的关键结论。`--fp8-embedding` 只对 LLM thinker 组件生效；没有 thinker 的模型（如 Qwen3-TTS 的 talker / code_predictor）即使传了该开关，也会被警告并**回退到 FP16**：

> [tensorrt_edgellm/scripts/export.py:2796-2802](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L2796-L2802) — 注释与警告明确写「`--fp8-embedding` 只对 LLM thinker 适用；Talker / CodePredictor 用 FP16」。

原因在于 `fp8_embedding` 是在 thinker 的导出回调里被传下去的（[export.py 第 896 行](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L896) 的 `export_onnx(..., fp8_embedding=fp8_embedding)`），而 talker / code_predictor 走的是各自的组件导出路径，没有这条参数透传；运行时的 TTS 路径（`qwen3OmniTTSRuntime`）也是按 `text_embedding.safetensors` 的 FP16 约定来读的（见 [qwen3OmniTTSRuntime.cpp:199-204](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/qwen3OmniTTSRuntime.cpp#L199-L204)）。

#### 4.3.4 代码实践

**实践目标：** 理解 FP8 embedding sidecar 的内存收益，并解释 TTS 为何不支持。

**操作步骤：**

1. 读 `quantize_embedding_to_fp8`，确认 `block_size=128` 且要求 `hidden_size % 128 == 0`（[第 40-43 行](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/embedding_quantization.py#L40-L43)）。
2. 手算内存收益（**示例代码·纯算术**）：假设 vocab=151936、hidden=4096。
   - FP16：`151936 × 4096 × 2 ≈ 1.24 GB`。
   - FP8 embedding：`151936 × 4096 × 1`（权重）+ `151936 × 32 × 4`（scale，`4096/128=32` 组）`≈ 0.62 GB + 19 MB ≈ 0.64 GB`。
   - 收益约 **省一半**（scale 开销可忽略）。
3. 读 export.py 的 TTS 警告（[第 2799-2802 行](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L2799-L2802)），回答下方问题。

**需要观察的现象 / 预期结果：** 你应能说出：FP8 sidecar 用 per-128 分块 scale 控制溢出，整体省一半显存；但 talker/code_predictor 因为不在 thinker 导出路径上、运行时按 FP16 读 `text_embedding.safetensors`，所以不支持。

> 真正导出一个 FP8 embedding 需要真实模型与 GPU，本实践以源码阅读 + 手算为主，运行部分**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1：** 为什么 FP8 embedding 用 per-row **分块** scale，而不是整行一个 scale？
**答案：** 整行（`hidden_size` 可能上千）内元素幅度差异大，单个 scale 会让小幅度元素被量化噪声淹没、大幅度元素逼近 448 上限。分块（128 一组）让每组有独立的动态范围，兼顾精度与不溢出。

**练习 2：** EAGLE3 draft 导出时为什么不写 `embedding.safetensors`？
**答案：** draft 模型复用 base 模型的 embedding 表，C++ 构建器对 draft 也会跳过拷贝；写出来是冗余。见 [checkpoint_utils.py:816-826](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/checkpoint_utils.py#L816-L826)。

**练习 3：** 给 Qwen3-TTS 加 `--fp8-embedding` 会发生什么？
**答案：** 它没有 thinker 组件，导出器会打一条 warning 并把 talker/code_predictor 的 embedding 以 FP16 写出，开关静默无效。见 [export.py:2799-2802](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L2799-L2802)。

---

### 4.4 量化 draft 模型（EAGLE3 与 DFlash）

#### 4.4.1 概念说明

投机解码（speculative decoding）用一个小的 **draft 模型**快速猜若干个 token，再由大的 **base 模型**一次性验证（详见 u7）。draft 模型要跑得快，自然也要量化。但 EAGLE3 与 DFlash 这两种 draft 架构**不在 HuggingFace transformers 里**——它们是研究型/自研结构，没有现成的 `AutoModelForCausalLM` 能加载。

于是 EdgeLLM 在 `quantization/models/` 下为它们各写了一个**完全自包含的 PyTorch 校准实现**：只用 `torch.nn` 基础模块拼出前向，专门用来给 ModelOpt 跑 PTQ 校准（统计激活 amax），不依赖 transformers 的模型类，也不含 TensorRT 自定义算子 stub。

这两个文件的关键设计：

- **EAGLE3 draft**（`eagle3_draft.py`）：输入是 `(input_ids, hidden_states, hidden_states_from_draft)`，注意力输入维度是 `2*hidden_size`（把 draft 自己的隐藏态和 base 的隐藏态拼接）。它有一个从 base 模型训练出来的 `fc` 投影（`target_hidden*3 → hidden`），并把 base 的 embedding 借过来当 `embed_tokens`。
- **DFlash draft**（`dflash_draft.py`）：输入是 `(proposal_embeds, target_hidden_concat)`，`fc` 把 `num_target_layers * hidden` 的拼接隐藏态投成 `h_delta`，draft 的 K/V 由 `h_delta` 投影产生（target path），self-attention 走 proposal path。它的 `fc` 投影器对精度敏感，量化时被**强制排除**。

两者的产出都是「带 `hf_quant_config.json` 的标准 HF 检查点」，再交给 u2 的导出前端去 repack/导出，与普通 LLM 量化完全合流。

#### 4.4.2 核心流程

两个文件的量化编排函数结构高度一致，都是经典的 PTQ 四步：

```
quantize_and_export_*_draft(base_dir, draft_dir, output_dir, quantization, ...):
  1. draft = XxxDraftModel.from_pretrained(draft_dir, base_dir)   # 加载 + 借 base 权重
  2. if not is_quantized(draft):
       base, tokenizer = 加载未量化 base 模型
       quant_cfg = build_quant_config(quantization, lm_head_q, kv_q)
       （DFlash 额外：_disable_dflash_fc_quantization(quant_cfg)）
       mtq.quantize(draft, quant_cfg, forward_loop=_calib)         # 跑校准前向统计 amax
  3. 对每个 quantized linear 调 _export_quantized_weight 导出量化权重
  4. postprocess_state_dict + save_file("model.safetensors")
     拷 config.json，写 hf_quant_config.json
```

校准前向 `_calib` 的核心是：**用 base 模型跑出真实的隐藏态，喂给 draft**，这样 draft 的激活分布才贴近真实推理。EAGLE3 取 base 第 2、中间、倒数第 4 层隐藏态拼接；DFlash 取 `target_layer_ids` 对应层（带 HF 的 +1 偏移）拼接，并构造 `[last_token, mask, mask, ...]` 的 proposal 输入。

#### 4.4.3 源码精读

**EAGLE3 校准模型的前向**——注意 `2*hidden_size` 的拼接输入与 base embedding 的复用：

> [tensorrt_edgellm/quantization/models/eagle3_draft.py:168-177](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/models/eagle3_draft.py#L168-L177) — `forward`：`inputs_embeds = embed_tokens(input_ids)`、`hidden_states = fc(hidden_states) + hidden_states_from_draft`，最后 `lm_head` 只取最后一个 token。
>
> [tensorrt_edgellm/quantization/models/eagle3_draft.py:305-321](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/models/eagle3_draft.py#L305-L321) — `_fill_embedding`：当 draft 检查点没有 `embed_tokens.weight` 时，从 base 模型的多个候选 key 名里借过来。

**EAGLE3 的权重 key 重映射**——训练产物里的名字和 EdgeLLM 期望的名字不一致，要翻译（丢弃 `t2d`，把 `midlayer` 重命名成 `layers.0`）：

> [tensorrt_edgellm/quantization/models/eagle3_draft.py:286-294](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/models/eagle3_draft.py#L286-L294) — `_remap_keys`。

**EAGLE3 校准**——用 base 的隐藏态驱动 draft：

> [tensorrt_edgellm/quantization/models/eagle3_draft.py:240-249](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/models/eagle3_draft.py#L240-L249) — 取 base 第 `[2, 中间, 倒数第4]` 层隐藏态拼接成 `cat_hs`，喂给 `dm(data, cat_hs, zeros)`。

**DFlash 的两个关键差异。** (1) 默认量化格式是 **nvfp4**（不是 fp8）；(2) **KV cache 量化尚未验证**，传了会直接报错；(3) **`fc` 投影器被强制留在 FP16/FP32**：

> [tensorrt_edgellm/quantization/models/dflash_draft.py:391](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/models/dflash_draft.py#L391) — 默认 `quantization: str = "nvfp4"`。
>
> [tensorrt_edgellm/quantization/models/dflash_draft.py:410-412](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/models/dflash_draft.py#L410-L412) — KV cache 量化未验证，raise ValueError。
>
> [tensorrt_edgellm/quantization/models/dflash_draft.py:547-559](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/models/dflash_draft.py#L547-L559) — `_disable_dflash_fc_quantization`：把 `fc` 的 input/weight/output_quantizer 全部 `enable=False`；导出后还在 `hf_quant_config.json` 的 `exclude_modules` 里追加 `"fc"`（[第 504-508 行](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/models/dflash_draft.py#L504-L508)）。

**DFlash 的 lm_head 借用**——draft 检查点可能不带 `lm_head.weight`，按 base 是否 `tie_word_embeddings` 决定从 `lm_head` 还是 embedding 借：

> [tensorrt_edgellm/quantization/models/dflash_draft.py:344-379](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/models/dflash_draft.py#L344-L379) — `_fill_lm_head_from_base`。

**两者为什么独立于 transformers。** 两个文件开头的 docstring 都强调「fully self-contained」「no dependency on transformers model classes」「only the calibration forward path is implemented」（完整带 KV cache 与 GatherND 的推理前向属于 ONNX 导出层）：

> [tensorrt_edgellm/quantization/models/eagle3_draft.py:15-23](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/models/eagle3_draft.py#L15-L23) 与 [tensorrt_edgellm/quantization/models/dflash_draft.py:15-37](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/models/dflash_draft.py#L15-L37)。

#### 4.4.4 代码实践

**实践目标：** 对比 EAGLE3 与 DFlash draft 在量化时的三处关键差异，并解释它们各自从 base「借」什么。

**操作步骤：**

1. 打开两个 `quantize_and_export_*` 函数签名，对比默认 `quantization`：EAGLE3 [第 201 行](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/models/eagle3_draft.py#L201) 是 `fp8`，DFlash [第 391 行](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/models/dflash_draft.py#L391) 是 `nvfp4`。
2. 确认 DFlash 对 `kv_cache_quantization` 与 `fc` 的特殊处理（[第 410-412 行](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/models/dflash_draft.py#L410-L412)、[第 547-559 行](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/models/dflash_draft.py#L547-L559)），EAGLE3 没有这两条限制。
3. 填下表（**示例代码·源码阅读型**）：

   | 维度 | EAGLE3 draft | DFlash draft |
   |------|-------------|-------------|
   | 默认量化 | fp8 | nvfp4 |
   | 从 base 借 | `embed_tokens.weight` | `lm_head.weight`（按 tie 决定） |
   | 强制不量化的模块 | 无特殊排除 | `fc`（精度敏感的 target-hidden 投影器） |
   | KV cache 量化 | 支持（透传 `kv_cache_quantization`） | 未验证，传了报错 |

**需要观察的现象 / 预期结果：** 你应能解释——DFlash 的 `fc` 把 base 多层隐藏态投成 `h_delta`，是 draft K/V 的来源，精度一掉 draft 就乱猜，所以必须排除量化；EAGLE3 的 `fc` 没有这条约束，故可整体量化。

> 实际跑校准需要 base+draft 检查点与 GPU，本实践以源码阅读为主，运行部分**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1：** 为什么 EAGLE3/DFlash draft 要在 `quantization/models/` 下自己写一套 PyTorch 实现，而不复用 transformers？
**答案：** 它们是非 transformers 原生的自研/研究型 draft 架构，`AutoModelForCausalLM` 加载不了；而 ModelOpt 的 PTQ 只需要一个能跑前向的 `nn.Module` 来统计激活 amax，所以写一个「只含校准前向、不含 TRT 自定义算子 stub」的最小自包含实现就够（见两个文件的 docstring）。

**练习 2：** DFlash draft 量化时为什么要把 `fc` 排除？
**答案：** `fc` 是把 base 多层隐藏态投成 `h_delta` 的精度敏感投影器，`h_delta` 直接决定 draft 的 K/V；它一旦被量化失真，draft 提议质量会显著下降。源码用 `_disable_dflash_fc_quantization` 关掉它的所有 quantizer，并在导出的 `hf_quant_config.json` 里把 `fc` 加进 `exclude_modules`（[第 504-508 行](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/models/dflash_draft.py#L504-L508)）。

**练习 3：** EAGLE3 draft 的 `from_pretrained` 里 `_remap_keys` 做了什么？
**答案：** 把训练产物的 key 名翻译成 EdgeLLM 期望的参数名：丢弃 `t2d` 相关 key（draft-to-target 映射不在权重里），把 `midlayer` 重命名为 `layers.0`（见 [第 286-294 行](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/models/eagle3_draft.py#L286-L294)）。

---

## 5. 综合实践

**任务：追踪一个 NVFP4 量化模型从检查点到运行时的完整「权重变形」旅程。**

假设你手上有一个 NVFP4 量化的 Qwen3-MoE 检查点（backbone=`nvfp4`）。请完成下列源码追踪并产出一张「权重变形表」：

1. **量化阶段（u3-l1/u3-l2）：** 该检查点由 `tensorrt-edgellm-quantize llm --backbone_quantization nvfp4` 产出。确认它的 `hf_quant_config.json` 里 backbone 算法，以及每个 Linear 的 `weight`/`weight_scale`/`weight_scale_2` 三个 buffer 的来源。
2. **加载阶段（u2-l4）：** `load_weights` 用 `_set_tensor` 把这些 buffer 原样写进 `nn.Module`（保留 uint8/fp8 dtype），不做反量化。
3. **重排阶段（本讲 4.1/4.2）：** `apply_all_repacking` 跑到第 6 步 `_cast_nvfp4_weights` 把普通 NVFP4 线性层 `weight` view 成 int8；而 MoE 专家块在 `_stack_moe_experts`（第 1 步）里经 `repack_nvfp4_qwen3_moe_experts` 做 decode→encode→stack。请在源码里定位这两段（[第 339-351 行](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L339-L351) 与 [第 802-876 行](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/repacking.py#L802-L876)）。
4. **sidecar 阶段（本讲 4.3）：** 若导出时加了 `--fp8-embedding`，`write_runtime_sidecars` 会把 `embed_tokens.weight` 经 `quantize_embedding_to_fp8` 写成 FP8 sidecar；否则写 FP16。
5. **交付物：** 一张表，列出 `qkv_proj`、`gate_up_proj`、MoE 专家、`embed_tokens` 这四类权重分别在「检查点 dtype/shape → 重排后 dtype/shape → 落盘位置（图内 initializer / sidecar）」三列的状态。

**预期结果：** 你应能清晰看到——同一个 NVFP4 格式，普通线性层只需 `view(int8)`，MoE 专家却要走完整的「反量化→重量化→MMA swizzle」，而 embedding 则根本不进图、以 FP8/FP16 sidecar 单独存在。这正是本讲三个模块（repacking、embedding_quantization、量化 draft）所要建立的全景。

> 本综合实践以源码追踪与制表为主，可全程离线完成；若要真正导出验证，需 GPU 与对应检查点，**待本地验证**。

## 6. 本讲小结

- EdgeLLM 要吃至少三种 int4 检查点（AWQ 列打包 int32、GPTQ 行打包 int32、ModelOpt 预打包 uint8），它们在 `apply_all_repacking` 里按**固定六趟顺序**翻译成 int4 GEMM 插件的 swizzled int8 布局；MoE 专家堆叠必须最先跑，因为它要 GPTQ 原始 int32 权重。
- **零点折叠**是把 AWQ/GPTQ 的非对称约定 `(nibble - qzero) * scale` 对齐到 kernel 对称约定 `(nibble - 8) * scale` 的核心技巧，公式 `adjusted = nibble - qzero + 8`。
- **NVFP4（E2M1）** 是浮点 4-bit，采用「per-16 block FP8 scale + per-tensor super-scale」两级缩放；普通线性层只需 `uint8→int8` view，MoE 专家要 decode→encode→6D MMA swizzle，并区分 SwiGLU FC1 的 interleave/concat 两种 layout。
- **FP8 embedding sidecar** 用 per-128 分块 scale 把词嵌入表压成 FP8 E4M3，省一半显存；但 `--fp8-embedding` 只对 LLM thinker 生效，TTS 的 talker/code_predictor 会回退 FP16。
- **EAGLE3 / DFlash draft** 因为不是 transformers 原生架构，在 `quantization/models/` 下各有独立自包含的校准实现；EAGLE3 默认 fp8、借 base 的 embedding，DFlash 默认 nvfp4、借 base 的 lm_head 且强制把精度敏感的 `fc` 投影器排除在量化之外。
- 所有这些重排与 sidecar 的产物最终都汇聚成「Python 导出端与 C++ 运行时之间的契约」：repack 后的 buffer 进 ONNX initializer，embedding 进 sidecar 文件，`hf_quant_config.json` 描述格式。

## 7. 下一步学习建议

- **向下游走（C++ 侧）：** 这些被 repack 过的权重最终如何被插件消费，见 **u8-l1（TensorRT 插件架构）** 和 **u8-l2（自定义 CUDA 算子）**，重点看 `Int4GroupwiseGemm` 与 `Nvfp4MoePlugin` 如何读取本讲产出的 swizzled 布局。
- **向纵深走（draft 全链路）：** 本讲只覆盖了 draft 的**量化**，draft 的**导出变体解析与 key remap** 见 **u7-l2（导出侧的投机解码）**，draft 的 **C++ 解码验证** 见 **u7-l1（投机解码策略）**。
- **想动手接新格式：** 若要支持一种新的量化检查点格式，参照 `repacking.py` 新增一个 `repack_xxx_to_plugin` + 在 `apply_all_repacking` 里按正确顺序插入一趟 `isinstance` 门控的扫描即可，顺序约束（MoE 先、GPTQ 后）务必遵守。
- **想验证精度：** 结合 **u9-l6（测试与 CI）** 的 `export→build→inference` 三步验证范式，对比量化前后的生成结果，确认本讲的位运算翻译没有引入数值错误。
