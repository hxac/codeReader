# 词表裁剪、FP8 KV 缓存与 chat 模板

## 1. 本讲目标

本讲把三项看似零散、实则同源的「导出期优化 / 格式化特性」一次性讲透。学完后你应当能够：

- 说清**词表裁剪（vocabulary reduction）**解决什么问题：为什么边缘设备的 `lm_head` 矩阵是显存与算力的浪费大户，以及 EdgeLLM 如何用一个 `vocab_map` 把它「瘦身」。
- 手工跑通 `tensorrt-edgellm-reduce-vocab` → `tensorrt-edgellm-export --reduced-vocab-dir` 的完整链路，并能解释 `vocab_map.safetensors` 在导出期与运行时分别被谁、如何消费。
- 说清 **FP8 KV 缓存**为何是「检查点元数据驱动、导出期自动启用」的，并能算出它相对 FP16 的显存收益。
- 理解 `processed_chat_template.json` 这一 sidecar 如何把 HuggingFace 的 Jinja 模板「编译」成 C++ 运行时分词器可直接消费的「前缀/后缀」结构，并知道哪些模型必须走硬编码模板。

这三项特性的共同点是：**它们都产出一个落盘的 sidecar 文件（`vocab_map.safetensors`、`config.json` 里的 `kv_cache_dtype`、`processed_chat_template.json`），由 Python 导出端写、由 C++ 运行时读**，是上一讲 u2-l6 强调的「sidecar 契约」的具体实例。

## 2. 前置知识

在进入正题前，先用三句话回顾本讲依赖的两个前置认知（详见 u2-l6 与 u5-l5）：

1. **sidecar 契约**：EdgeLLM 的 Python 导出端不只产出 ONNX 图，还会往同一个输出目录写一堆「辅助文件」（`config.json`、`embedding.safetensors`、tokenizer 文件、`processed_chat_template.json` 等）。这些 sidecar 是 C++ 运行时启动时必须读取的配置。
2. **lm_head 与 KV 缓存是显存大头**：一个 `lm_head` 是 `[hidden_size, vocab_size]` 的矩阵，`vocab_size` 通常 15 万以上；KV 缓存随序列长度线性增长。两者是边缘设备上「省显存」最能见效的地方。
3. **`config.quant` 是量化的唯一真相来源**：u2-l1 讲过的 `QuantConfig` 里，`kv_cache_quant` 字段决定 KV 缓存用什么精度，它在量化阶段（u3-l2 的 `--kv_cache_quantization`）被烤进检查点的 `hf_quant_config.json`，导出期只读不写。

如果你对 `safetensors`、`nn.Module` 的 `buffer/parameter`、TensorRT 的 `DataType` 还陌生，建议先回看 u2-l4（权重加载）与 u5-l6（张量抽象）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tensorrt_edgellm/vocab_reduction/selection.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/vocab_reduction/selection.py) | 从校准数据「选出」要保留的 token id 集合，生成 `vocab_map` 张量 |
| [tensorrt_edgellm/vocab_reduction/onnx_export.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/vocab_reduction/onnx_export.py) | 把 `vocab_map`「应用」到模型的 `lm_head`（按量化类型就地裁剪行），并把 sidecar 拷给运行时 |
| [tensorrt_edgellm/scripts/reduce_vocab.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/reduce_vocab.py) | `tensorrt-edgellm-reduce-vocab` 命令行入口 |
| [tensorrt_edgellm/chat_template.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/chat_template.py) | 把 HF Jinja 模板「编译」成 `processed_chat_template.json` |
| [tensorrt_edgellm/chat_templates/__init__.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/chat_templates/__init__.py) | 访问内置硬编码模板的薄封装 |
| [tensorrt_edgellm/vocab_reduction/constants.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/vocab_reduction/constants.py) | sidecar 文件名常量 |
| [tensorrt_edgellm/models/default/modeling_default.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py) | 默认 Attention：`enable_fp8_kv_cache` 开关与 `q/k/v_scale` 缓冲 |
| [tensorrt_edgellm/checkpoint/checkpoint_utils.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/checkpoint_utils.py) | `write_runtime_artifacts`：把 `reduced_vocab_size`、`kv_cache_dtype`、chat 模板一起写进运行时目录 |
| [cpp/runtime/kvCacheManager.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/kvCacheManager.cpp) | 运行时 KV 缓存分配：按 `kvCacheType`（kHALF/kFP8）决定每个元素字节数 |

---

## 4. 核心概念与源码讲解

### 4.1 词表裁剪（vocabulary reduction）

#### 4.1.1 概念说明

一个典型的大模型词表有 15 万～25 万个 token，但任何一个具体任务（比如某汽车的座舱问答、某机器人的指令理解）真正会用到的 token 往往只有几千到几万。`lm_head` 是一个形状为 \([H,\, V]\) 的矩阵，其中 \(H\) 是隐藏维度、\(V\) 是词表大小。模型每一步都要用它把隐状态投影到 \(V\) 维 logits 再采样——**这 \(V\) 列里有绝大多数在当前任务里永远不会被采样出来**。

词表裁剪的核心想法是：在导出前，先用一份**校准数据**统计「这个任务实际会用到哪些 token」，只保留这些 token 对应的 `lm_head` 输出行，把 \(V\) 砍到一个远小的 \(V'\)。带来的好处是三重的：

- `lm_head` 权重矩阵更小 → 省权显存、省 GEMM 算力；
- 每步 logits 张量从 \([V]\) 变 \([V']\) → 省 activation 显存；
- 采样在更小词表上做 → 采样 kernel 更快（见 u5-l7）。

关键约束：裁剪后采样得到的 token id 是「压缩后空间」里的 id（范围 \(0..V'-1\)），必须能映射回「原始全词表」里的真实 id，模型才能输出正确文字。这个映射就是 `vocab_map`——一个长度为 \(V'\) 的 `int32` 张量，`vocab_map[i]` 表示「压缩后的第 i 个 token 在原词表里的真实 id」。

> 术语：**vocab_map** = 「压缩 id → 全词表 id」的查表；**reduced_vocab_size** = \(V'\)；**lm_head 裁剪** = 只保留 `vocab_map` 列出的那些输出行。

#### 4.1.2 核心流程

词表裁剪在 EdgeLLM 里横跨三个阶段，是一个典型的「离线生成 → 导出应用 → 运行时还原」流程：

```text
[阶段 A：离线选词]  reduce_vocab CLI
   校准数据(CNN/DailyMail) + tokenizer + config
        │  reduce_vocab_size()
        ▼
   vocab_map.safetensors  +  reduced_vocab.json   (两个 sidecar)

[阶段 B：导出期应用]  export --reduced-vocab-dir <A 的输出>
   load_reduced_vocab_map() 校验并加载 vocab_map
        │  apply_reduced_vocab() → _reduce_lm_head_in_place()
        ▼
   lm_head 就地裁掉 V' 行；config.reduced_vocab_size = V'
   export_onnx() 照常导出（lm_head 已经是小的了）
        │  copy_reduced_vocab_artifacts()
        ▼
   运行时目录里多一份 vocab_map.safetensors；config.json 多一个 reduced_vocab_size

[阶段 C：运行时还原]  C++ sampler
   每个 batch 采样出 [V'] 空间的 token id
        │  mapReducedVocabToFullVocab()  原地查表
        ▼
   id = vocab_map[id]   → 全词表 id → 解码成文字
```

选词算法有两种，由 `--method` 选择：

- **frequency（频率法）**：在校准文本里统计 token 频次，取出现最多的若干个。
- **input_aware（输入感知法，默认）**：针对摘要类任务设计，优先保留「输出摘要里出现且输入文档里也出现」的任务相关 token，再加一层「容差预算」吸收相邻 token。

无论哪种方法，都有一批「必须保留」的 token：特殊 token（EOS/BOS/PAD/UNK，否则无法停止/填充）和（若给 EAGLE 提供了 `d2t` 草稿词表映射）`d2t` 引用到的 base token。这部分先扣减，剩下的名额才分给算法选择。

#### 4.1.3 源码精读

**① 选词内核 `reduce_vocab_size()`** —— 这是「阶段 A」真正干活的函数，返回排好序的 `int32` 张量：

[tensorrt_edgellm/vocab_reduction/selection.py:207-258](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/vocab_reduction/selection.py#L207-L258) — 先用 `get_special_tokens()` 收集必须保留的 token，可选地把 `d2t` 引用的 base token 并入 `required`；剩下的名额 `remaining_slots = reduced_vocab_size - len(required)` 交给 `frequency` 或 `input_aware` 滤子；最后 `required | additional` 合并，强制断言总数恰等于目标，返回 `torch.tensor(sorted(final_tokens), dtype=torch.int32)`。

其中特殊 token 的收集逻辑值得单独看，它体现了「EOS 必须在、且会兜底到 PAD」的设计：

[tensorrt_edgellm/vocab_reduction/selection.py:53-72](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/vocab_reduction/selection.py#L53-L72) — EOS 缺失时退而求其次用 PAD，两者都没有才报错；BOS/PAD/UNK 仅在非 None 时纳入。

对于投机解码场景，`d2t`（draft-to-target，见 u7-l2）会把压缩后的草稿词表映射到 base 词表，这些 base token 必须留在裁剪后的词表里，否则草稿模型采样出的 token 在 base 词表里「查无此人」：

[tensorrt_edgellm/vocab_reduction/selection.py:37-50](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/vocab_reduction/selection.py#L37-L50) — 对每个 reduced id，`base_token_id = reduced_token_id + offset`，落在合法区间则纳入 required。

两种选词滤子的差异在于「从哪取、按什么序取」：

[tensorrt_edgellm/vocab_reduction/selection.py:75-105](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/vocab_reduction/selection.py#L75-L105) — frequency 法直接对 `sample["article"]` 字段做 `Counter`，按 `most_common()` 取够名额。

[tensorrt_edgellm/vocab_reduction/selection.py:108-204](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/vocab_reduction/selection.py#L108-L204) — input_aware 法四步走：先从 `highlights`（摘要）建输出词频、从 `article`（文档）建输入词频；再做「输出且输入」的 input-aware 过滤；然后从输出高频里挑「核心词表」（占 90% 名额）外加「容差预算」（10% 名额，给每个核心 token 的 ±5 邻居 token，`tolerance_k=5`），最后用各类词频兜底填满。

> 注意一个数据集硬编码：两个滤子分别读 `sample["article"]` 与 `sample["highlights"]`，这正是 CNN/DailyMail 数据集的字段。`reduce_vocab.py` 的 CLI 也固定加载 `cnn_dailymail`——所以这套参考实现目前是「摘要任务专用」的样板，换任务需要换数据集与字段。

**② 命令行入口 `main()`** —— 串起「加载 tokenizer/config → 加载数据集 → 调 `reduce_vocab_size` → 落盘」：

[tensorrt_edgellm/scripts/reduce_vocab.py:33-138](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/reduce_vocab.py#L33-L138) — 关键参数：`--model_dir`（读 tokenizer 与 config）、`--output_dir`、`--reduced_vocab_size`（目标 \(V'\)，必须小于原 \(V\)）、`--method`（默认 `input_aware`）、`--max_samples`（默认 50000）、`--d2t_path`（可选）。产出两个文件，文件名由常量定义。

[tensorrt_edgellm/vocab_reduction/constants.py:17-22](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/vocab_reduction/constants.py#L17-L22) — `VOCAB_MAP_NAME = "vocab_map.safetensors"`、`VOCAB_INFO_NAME = "reduced_vocab.json"`；DFlash 草稿用独立命名 `draft_vocab_map.safetensors` 以免与 base 冲突。

**③ 导出期应用 `apply_reduced_vocab()`** —— 这是「阶段 B」的核心：加载 vocab_map、就地裁剪 lm_head、并把信息缓存给后续 sidecar 拷贝：

[tensorrt_edgellm/vocab_reduction/onnx_export.py:85-105](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/vocab_reduction/onnx_export.py#L85-L105) — 做三件事：① 校验不是 EAGLE3 draft（EAGLE3 不支持词表裁剪，会抛错）；② `config.reduced_vocab_size = V'`（让导出与 config.json 都知道）；③ 把 vocab_map 缓存到 `model._reduced_vocab_map_for_runtime`（后面 sidecar 拷贝要用）；④ 调 `_reduce_lm_head_in_place(lm_head, vocab_map)` 真正动刀。

`_reduce_lm_head_in_place()` 的分发逻辑是本模块的精髓——**裁剪方式必须匹配 lm_head 的量化类型**，因为不同量化方案的权重布局完全不同（见 u3-l3）：

[tensorrt_edgellm/vocab_reduction/onnx_export.py:351-388](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba233c9315b89d8e47d6/tensorrt_edgellm/vocab_reduction/onnx_export.py#L351-L388) — 按 `isinstance` 分发：
- **FP16 / FP8 / MXFP8 / INT8-SQ / NVFP4**：走 `_reduce_row_sliced_lm_head`，直接 `index_select` 输出维（第 0 维）的若干行——因为这些权重的输出维就是连续的行。
- **AWQ / GPTQ / ModelOpt 预打包 INT4**：必须走「repack 之前」的特殊路径（见下），因为这些权重是 int4 打包进 int32/uint8 的，不能简单按行切。

**④ 为什么 INT4 lm_head 要在 repack 之前裁剪？** 这是一个关键的时序约束。u2-l4 讲过，权重加载后会跑 `apply_all_repacking` 把检查点布局翻译成 C++ 插件期望的 swizzle 布局。一旦 repack 完成，输出维就被打散进打包格式里，再想按「逻辑 token id」选行就极其困难。所以正确做法是在 repack **之前**、权重还是检查点原始布局时就裁：

[tensorrt_edgellm/vocab_reduction/onnx_export.py:141-146](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/vocab_reduction/onnx_export.py#L141-L146) — `should_apply_reduced_vocab_before_repacking()` 仅当 lm_head 是 `AWQLinear`/`GPTQLinear`/`ModelOptAWQPrepackedLinear` 时返回 True。

INT4 裁剪还额外要求 \(V'\) 是 128 的倍数、`group_size==128`，否则打包后的列无法对齐：

[tensorrt_edgellm/vocab_reduction/onnx_export.py:173-192](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/vocab_reduction/onnx_export.py#L173-L192) — AWQ 是「列打包」（8 个 int4 塞进一个 int32），裁剪要按 `(0,4,1,5,2,6,3,7)` 的 `channel_to_bit` 重排 nibble；GPTQ 是行打包；ModelOpt 是 pair 打包（2 个 int4 塞进 uint8）。三种打包各有一个 `_reduce_*_before_repacking` 专用函数处理。

**⑤ 这套时序如何接入权重加载主流程？** u2-l4 的 `load_weights` 支持 `pre_repack_hook` 回调，词表裁剪正是靠它在正确时机插入：

[tensorrt_edgellm/model.py:361-408](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/model.py#L361-L408) — `from_pretrained` 里：若 `should_apply_reduced_vocab_before_repacking(model)` 为真，把 `apply_reduced_vocab` 包进 `pre_repack_hook`（在 repack 前跑）；否则置 `apply_reduced_vocab_after_load=True`，在 `load_weights` 之后才裁（适用于 FP16/FP8 等可直接按行切的情形）。

**⑥ sidecar 拷贝** —— 裁完后，把 vocab_map 原样（或转 int32）写进运行时目录：

[tensorrt_edgellm/vocab_reduction/onnx_export.py:108-138](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/vocab_reduction/onnx_export.py#L108-L138) — 若 `config.reduced_vocab_size` 为空则直接返回（未启用）；否则把 vocab_map 以 int32 存为 `vocab_map.safetensors`，并顺带拷贝 `reduced_vocab.json`。

**⑦ 运行时如何消费？** `reduced_vocab_size` 会被写进 `config.json`，C++ 运行时据此决定是否加载 `vocab_map.safetensors`；采样后用查表把压缩 id 还原成全词表 id：

[tensorrt_edgellm/checkpoint/checkpoint_utils.py:629-630](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/checkpoint_utils.py#L629-L630) — `build_runtime_llm_config_dict` 把 `reduced_vocab_size` 写进 config.json。

[cpp/sampler/sampling.h:250-265](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/sampler/sampling.h#L250-L265) — `mapReducedVocabToFullVocab` 原地查表：`vocabIds[i] = vocabMappingTable[vocabIds[i]]`，把压缩空间 id 映射回全词表。

[cpp/runtime/llmInferenceRuntime.cpp:340-359](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L340-L359) — 运行时构造期从 `engineDir/vocab_map.safetensors` 加载映射表，强校验「1D、长度等于 reduced_vocab_size、INT32」，存为 `mBaseVocabMappingTable`。

> CLI 侧两个入口（u2-l6 / u4-l3 的 export 命令）：base 模型用 `--reduced-vocab-dir`，DFlash 草稿用 `--draft-reduced-vocab-dir`（产出 `draft_vocab_map.safetensors`）。见 [tensorrt_edgellm/scripts/export.py:2446-2463](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/export.py#L2446-L2463)。

#### 4.1.4 代码实践

**实践目标**：手工组装一条 `reduce-vocab` 命令，理解它产出的两个 sidecar，并追踪 `vocab_map.safetensors` 在后续 `export` 中被谁消费。

**操作步骤**：

1. 先生成词表裁剪产物（需要能联网下载 `cnn_dailymail` 数据集与一个 HF 模型，如 `Qwen/Qwen3-0.6B`）：

   ```bash
   tensorrt-edgellm-reduce-vocab \
     --model_dir Qwen/Qwen3-0.6B \
     --output_dir /tmp/reduced_vocab \
     --reduced_vocab_size 32000 \
     --method input_aware \
     --max_samples 2000
   ```

2. 检查产物目录，应当看到：
   - `vocab_map.safetensors`：内含一个 `vocab_map` 张量，形状 `[32000]`，dtype 为 int32。
   - `reduced_vocab.json`：记录原始词表大小、裁剪后大小、所用方法与数据集。

3. 用下面的最小 Python 片段（**示例代码**，非项目原有）读取并校验 `vocab_map`，对照 `load_reduced_vocab_map` 的断言理解它做了哪些合法性检查：

   ```python
   # 示例代码：手动加载并校验 vocab_map，复现 onnx_export.load_reduced_vocab_map 的校验逻辑
   from safetensors import safe_open
   import torch

   with safe_open("/tmp/reduced_vocab/vocab_map.safetensors", framework="pt", device="cpu") as f:
       vm = f.get_tensor("vocab_map")

   assert vm.dim() == 1, "必须 1D"
   assert not torch.is_floating_point(vm), "必须是整数 token id"
   assert torch.unique(vm).numel() == vm.numel(), "不能有重复 id"
   assert int(vm.max()) < 151936, "id 不能超过 Qwen3-0.6B 的 vocab_size"
   print("vocab_map 长度:", vm.numel(), "最小 id:", int(vm.min()), "最大 id:", int(vm.max()))
   ```

4. 再组装导出命令，把裁剪产物喂给 export（**待本地验证**：需要 GPU 与 TensorRT 环境）：

   ```bash
   tensorrt-edgellm-export \
     Qwen/Qwen3-0.6B \
     /tmp/qwen3_onnx_reduced \
     --reduced-vocab-dir /tmp/reduced_vocab
   ```

**需要观察的现象**：

- 步骤 1 终端会打印「Final vocabulary composition」与「Required tokens (d2t + special): N / Method-selected tokens: M」——核对 N 是否等于你模型特殊 token 的数量（Qwen3 通常 5 个左右）。
- 步骤 3 的断言应全部通过，且最大 id 小于原始 `vocab_size`。
- 步骤 4 的导出日志里应出现 `Reduced lm_head output dimension to 32000`，导出目录的 `config.json` 里应有 `"reduced_vocab_size": 32000`，且目录下多出一份 `vocab_map.safetensors`。

**预期结果**：导出的 ONNX 里 `lm_head` 的输出维从 ~151936 缩到 32000；运行时采样后经 `mapReducedVocabToFullVocab` 还原，最终输出的文字与未裁剪版本一致（在所选校准任务覆盖的 token 范围内）。**若校准数据与你的真实任务分布差异大，可能出现个别稀有 token 无法输出——这是词表裁剪的固有代价。**

#### 4.1.5 小练习与答案

**练习 1**：为什么 EAGLE3 draft 模型不支持词表裁剪（`apply_reduced_vocab` 会主动抛错），而 DFlash draft 却支持？

**参考答案**：EAGLE3 draft 的词表是一个独立的「草稿词表」，它靠 `d2t`（draft-to-target）映射把草稿 id 翻译成 base 词表 id（见 u7-l2）；base 模型若再做词表裁剪，`d2t` 指向的 base token 可能已被裁掉，映射链断裂。而 DFlash draft 走的是另一条路（它复用 base 的 `lm_head`，见 u7-l2 的 `_inherit_dflash_lm_head_quant`），对它裁剪 lm_head 等价于同步缩小草稿输出空间，可独立用 `draft_vocab_map` 管理，故支持。

**练习 2**：假设你的 lm_head 是 AWQ 量化（int4 列打包），你想把词表裁到 30000。`_validate_int4_checkpoint_reduction` 会因为哪一条拒绝你？

**参考答案**：因为 30000 不是 128 的倍数（见 [onnx_export.py:176-180](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/vocab_reduction/onnx_export.py#L176-L180)）。AWQ 把 8 个 int4 权重打包进一个 int32，且 group_size 固定 128，裁剪后的输出维必须是 128 的倍数才能保持打包列对齐。你应该改成最近的 128 倍数，如 30016。

**练习 3**：`vocab_map` 在运行时的方向是「压缩 id → 全词表 id」。为什么导出期裁 lm_head 时是「按 `vocab_map` 选行」，这两件事的「方向」一致吗？

**参考答案**：一致。`vocab_map[i] = full_id`，意思是「裁剪后词表的第 i 个位置对应全词表的第 `full_id` 行」。裁 lm_head 时，保留的就是这些 `full_id` 行、并按 `vocab_map` 的顺序重排成新的第 0..V'-1 行，于是「新 lm_head 的第 i 行」恰好对应「压缩 id i」。运行时采样出压缩 id i 后，用 `vocab_map[i]` 还原成全词表 id 再解码，方向自洽。

---

### 4.2 FP8 KV 缓存

#### 4.2.1 概念说明

u5-l5 讲过，KV 缓存是显存大头：每个 attention 层都要存历史 token 的 Key/Value，形状为 \([B, 2, N_{kv}, L_{max}, D]\)，随序列长度 \(L\) 线性增长。默认它是 FP16（每个元素 2 字节）。

**FP8 KV 缓存**把这个存储精度从 FP16 换成 NVIDIA FP8 E4M3（每个元素 1 字节）。直接的收益是 KV 缓存显存**减半**；在 Blackwell（SM100+）这类原生支持 FP8 的硬件上，由于 CuTe DSL 的 FP8 注意力 kernel 能直接读 FP8 的 Q/KV 张量，还能额外带来 9%~17% 的 context attention 提速（长上下文场景下数据搬运减少的红利更明显）。

FP8 E4M3 的格式是：1 位符号、4 位指数、3 位尾数，可表示的最大值是 \(448.0\)。量化一个张量到 FP8 用「per-tensor 缩放」：

\[
\text{scale} = \frac{\text{amax}}{448.0}, \qquad q = \text{round}\!\left(\frac{x}{\text{scale}}\right)\ \text{clip 到 FP8 范围}
\]

其中 amax 是该张量在校准前向里统计到的最大绝对值。EdgeLLM 对 Q/K/V 各算一个独立的 per-tensor scale，记作 `q_scale`/`k_scale`/`v_scale`。

> 关键认知：**FP8 KV 缓存是「检查点元数据驱动」的，不是导出期 flag**。量化阶段（u3-l2 的 `--kv_cache_quantization fp8`）把 `kv_cache_quant: fp8` 烤进 `hf_quant_config.json`；导出期 `ModelConfig` 读到这个字段后自动启用，没有专门的导出开关。运行时再从导出的 `config.json` 里读 `kv_cache_dtype` 决定 KV 缓存用什么 dtype 分配。

#### 4.2.2 核心流程

```text
[量化阶段]  tensorrt-edgellm-quantize llm --kv_cache_quantization fp8
   ModelOpt 校准 → 每层 K/V 的 amax → q_scale/k_scale/v_scale
   烤进检查点 hf_quant_config.json: kv_cache_quant="fp8"
        │
[导出阶段]  自动启用（config.quant.kv_cache_quant == "fp8"）
   ① 每个 q/k/v_proj 模块 register_buffer("q_scale"/"k_scale"/"v_scale")
      （从检查点权重 ...{q,k,v}_proj.{q,k,v}_scale 加载）
   ② attention_plugin 算子拿到 enable_fp8_kv_cache=True 与 qkv_scales
   ③ ONNX 导出时把 fp8 KV cache 标志与 scale 落进图
   ④ config.json 写入 "kv_cache_dtype": "fp8"
        │
[构建阶段]  引擎构建器从 ONNX 自动探测 FP8 KV（无需 flag）
        │
[运行阶段]  KVCacheManager 用 nvinfer1::DataType::kFP8 分配缓存
   每元素 1 字节（FP16 是 2 字节）→ 显存减半
   attention kernel 读 FP8 Q/KV + per-tensor scale，输出仍是 FP16
```

需要特别注意的两条**限制**：

1. **EAGLE3 / DFlash 草稿模型强制关闭 FP8 KV**：草稿模型自带的 KV 缓存（`mDraftCacheManager`）不量化，`enable_fp8_kv_cache=False` 硬编码（见 [modeling_eagle3_draft.py:155](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/eagle3/modeling_eagle3_draft.py#L155)、[modeling_dflash_draft.py:197](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/dflash/modeling_dflash_draft.py#L197)）。base 模型仍可用 FP8 KV。
2. **FP8 系统提示 KV 缓存尚未实现**：u9-l2 讲的系统提示 KV 缓存的 capture/restore 目前只支持 FP16，FP8 会显式报错（见下文源码）。

#### 4.2.3 源码精读

**① 启用开关与 scale 缓冲** —— 默认 Attention 在构造时读取量化配置，决定是否启用，并在启用时给每个投影层挂三个 scale 缓冲：

[tensorrt_edgellm/models/default/modeling_default.py:217](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L217) — `self.enable_fp8_kv_cache = config.quant.kv_cache_quant == "fp8"` 是唯一的启用判据。

[tensorrt_edgellm/models/default/modeling_default.py:239-245](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L239-L245) — 启用时，给 `q_proj`/`k_proj`/`v_proj` 各 `register_buffer` 一个 `q_scale`/`k_scale`/`v_scale`（初值 1.0）。注释特别强调：这些是 **KV 缓存专用的 per-tensor scale**，挂在投影模块上、对应检查点 key `...{q,k,v}_proj.{q,k,v}_scale`，**不是** FP8Linear 那种权重/输入 per-tensor scale，两者不要混淆。

**② 透传给 attention 算子** —— 启用标志与 scale 作为参数喂给 `attention_plugin` 自定义算子（u2-l3 / u8-l1）：

[tensorrt_edgellm/models/default/modeling_default.py:290-298](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L290-L298) — kwargs 里带 `enable_fp8_kv_cache` 与（在下方拼接的）`qkv_scales`。

[tensorrt_edgellm/models/ops.py:90-157](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/ops.py#L90-L157) — `attention_plugin` 的函数签名与 docstring 给出一张特性矩阵：`enable_fp8_kv_cache` 与 `enable_tree_attention`（EAGLE 树形）可独立组合。注释点明一个 dynamo 导出的坑：`enable_fp8_kv_cache` 等 kwarg **不能用默认值**，否则 `torch.export` 会把它从 FX 图里剥掉、破坏 ONNX 翻译；`qkv_scales` 也必须显式传 `[1.0, 1.0, 1.0]` 让图里有一个合法的 FLOATS 值。

**③ 烤进 config.json** —— 导出期把 dtype 字符串写进运行时配置，C++ 严格从此读取：

[tensorrt_edgellm/checkpoint/checkpoint_utils.py:632-636](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/checkpoint_utils.py#L632-L636) — `"kv_cache_dtype": "fp8" if config.quant.kv_cache_quant == "fp8" else "fp16"`。注释说明这是导出期「烤死」的，运行时不再做引擎内省回填。

**④ 运行时分配** —— KVCacheManager 用 `kvCacheType` 决定每个元素几字节：

[cpp/runtime/kvCacheManager.h:55](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/kvCacheManager.h#L55) — `Config::kvCacheType` 只允许 `kHALF` 或 `kFP8`。

[cpp/runtime/kvCacheManager.cpp:29-85](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/kvCacheManager.cpp#L29-L85) — 构造函数先断言 `kvCacheType` 只能是 kHALF/kFP8（[L32](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/kvCacheManager.cpp#L32)），再用 `elemSize = getTypeSize(kvCacheType)` 算每元素字节数（[L48](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/kvCacheManager.cpp#L48)），按 `[maxBatchSize, 2, numKVHeads, maxSequenceLength, headDim]` 给每层分配一个 GPU 张量（[L77-79](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/kvCacheManager.cpp#L77-L79)）。FP8 时 elemSize=1、FP16 时 elemSize=2，故总分配字节数线性减半。

**⑤ FP8 系统提示缓存未实现** —— 这是 u9-l2 系统提示缓存与 FP8 KV 结合时的已知盲区：

[cpp/runtime/hybridCacheManager.cpp:367-371](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/hybridCacheManager.cpp#L367-L371)（[及 L415-419 同款断言](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/hybridCacheManager.cpp#L415-L419)）— `captureKVCache`/`restoreKVCache` 目前只实例化了 `half` 模板，遇到 `kFP8` 会显式抛错「FP8 system-prompt cache is not implemented」。所以**同时开 FP8 KV 与系统提示缓存会运行时报错**。

#### 4.2.4 代码实践

**实践目标**：用一张纸算清 FP8 KV 缓存相比 FP16 在你目标边缘设备上的显存收益。

**操作步骤**：

1. 选一个具体模型与部署配置，例如 Qwen3-8B：`num_hidden_layers=36`、`num_key_value_heads=8`、`head_dim=128`、`maxBatchSize=8`、`maxSequenceLength=4096`。
2. 套用 KVCacheManager 的分配公式，算 FP16 与 FP8 两版的总字节数：

   单层元素数 = \(B \times 2 \times N_{kv} \times L_{max} \times D\)。代入：

   \[
   8 \times 2 \times 8 \times 4096 \times 128 = 33{,}554{,}432 \ \text{元素}
   \]

   - FP16（2 字节）：\(33{,}554{,}432 \times 2 = 64\,\text{MiB}\) 每层，36 层共 **2304 MiB ≈ 2.25 GiB**。
   - FP8（1 字节）：\(33{,}554{,}432 \times 1 = 32\,\text{MiB}\) 每层，36 层共 **1152 MiB ≈ 1.125 GiB**。

3. 对照官方基准核对：长上下文 + 大 batch 时 KV 缓存主导显存，减半的收益最为显著。可参考 [docs/source/user_guide/features/FP8KV.md](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/user_guide/features/FP8KV.md) 里「d=128 模型 9~17% context attention 提速、~50% KV 显存」的结论。

**需要观察的现象 / 预期结果**：FP8 相比 FP16，KV 缓存显存恰好减半（这是 dtype 字节数直接决定的，与模型无关）。在 Blackwell 上还能观察到 context attention 的额外提速；在较老平台（如 Jetson Orin，SM8.7）则主要享受显存收益、速度收益较小。**精度层面**：绝大多数模型 FP8 KV 几乎无损，但官方文档点名 Qwen2.5-7B 系列（末层 KV 数值过大导致量化损失）有显著精度退化——**生产部署前务必在你的模型上验证输出质量**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 EdgeLLM 不提供一个 `--fp8-kv-cache` 的导出 flag，而是从检查点元数据驱动？

**参考答案**：因为 FP8 KV 缓存依赖量化阶段算出的 per-tensor scale（`q_scale`/`k_scale`/`v_scale`），这些 scale 必须由 ModelOpt 在校准前向里统计 amax 后写出，导出期无法凭空生成。所以正确的设计是：量化阶段决定「要不要 FP8 KV」并把决定与 scale 一起烤进检查点；导出期只读不算。这也保证了 config.json 里的 `kv_cache_dtype` 与图里实际用的 scale 不会漂移。

**练习 2**：把 `enable_fp8_kv_cache` 从 `attention_plugin` 的必传 kwarg 改成带默认值的可选 kwarg，会发生什么？

**参考答案**：会破坏 ONNX 导出。[ops.py:119-123](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/ops.py#L119-L123) 的注释明确指出：`torch.export` 会把「值等于默认值」的 kwarg 从 FX 图里剥掉，导致 dynamo 翻译阶段拿不到这个标志、无法正确生成 AttentionPlugin 节点。这就是为什么这些标志被刻意设成「必传、无默认值」。

**练习 3**：如果你的部署既想用 FP8 KV 缓存省显存，又想用系统提示 KV 缓存（u9-l2）降首 token 延迟，当前代码能同时满足吗？

**参考答案**：不能。[hybridCacheManager.cpp:367-371](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/hybridCacheManager.cpp#L367-L371) 的 capture/restoreKVCache 只支持 kHALF，遇到 FP8 会抛错。当前必须二选一：要么用 FP16 KV 换取系统提示缓存能力，要么用 FP8 KV 但放弃系统提示缓存，等待后续给 batched save/restore kernel 补上 FP8 模板实例化。

---

### 4.3 chat 模板

#### 4.3.1 概念说明

HuggingFace 的对话模板是一段 **Jinja2** 字符串（存在 `tokenizer.chat_template` 或 `chat_template.json` 里），把「多轮对话消息列表」渲染成模型期望的纯文本 prompt。例如 ChatML 风格会把 `{role: user, content: "你好"}` 渲染成 `<|im_start|>user\n你好<|im_end|>\n<|im_start|>assistant\n`。

问题在于：**C++ 运行时的分词器不内嵌 Jinja 引擎**。EdgeLLM 的解法是：在导出期（Python 端，有 transformers 库可用）把 Jinja 模板「编译」成一个**结构化的 JSON**——把每个角色（system/user/assistant）的「前缀」「后缀」、「生成提示」（generation_prompt）、多模态内容的占位格式等，全部预先提取成字符串字段。C++ 运行时只需按这些字段做字符串拼接，就能复现与 HF 一致的 prompt 格式。

这个编译产物就是 `processed_chat_template.json`，由 [tensorrt_edgellm/chat_template.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/chat_template.py) 的 `process_chat_template()` 生成，由 C++ 分词器在 [cpp/tokenizer/tokenizer.cpp:125](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/tokenizer/tokenizer.cpp#L125) 处加载、解析成 `ChatTemplateConfig` 结构（[tokenizer.h:61](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/tokenizer/tokenizer.h#L61)）。

> 三种生成路径，按优先级：① 硬编码模板（`_HARDCODED_TEMPLATE_MAP`）→ ② Jinja 自动提取 → ③ 兜底最小模板。

#### 4.3.2 核心流程

```text
process_chat_template(model_dir, output_dir)
   │
   ├─[1] _try_write_hardcoded_template(): 查 _HARDCODED_TEMPLATE_MAP[model_type]
   │       命中 → 直接拷内置 JSON（改 model_path）→ return        ★ 最高优先级
   │
   ├─[2] 加载 tokenizer/processor（VLM 先 AutoProcessor 再 AutoTokenizer）
   │      用占位符消息调 apply_chat_template，提取各 role 的 prefix/suffix
   │      提取 generation_prompt（add_generation_prompt=True）
   │      提取 content_types（image/audio/video 的占位格式）
   │      处理 BOS、trim_content、thinking 变体等边角
   │      → 写 processed_chat_template.json                            ★ Jinja 提取
   │
   └─[3] 提取失败 → 再试硬编码 → 仍无 → write_fallback_processed_chat_template()
                                                                          ★ 兜底
```

`processed_chat_template.json` 的典型结构（以内置 Gemma4 模板为例）：

```json
{
  "roles": {
    "system":    {"prefix": "<bos><|turn>system\n", "suffix": "<turn|>\n"},
    "user":      {"prefix": "<bos><|turn>user\n",   "suffix": "<turn|>\n"},
    "assistant": {"prefix": "<|turn>model\n",       "suffix": "<turn|>\n"}
  },
  "content_types": {"image": {"format": "<|image|>"}, ...},
  "generation_prompt": "<|turn>model\n",
  "generation_prompt_thinking": "<|turn>model\n<|channel>thought\n",
  "default_system_prompt": ""
}
```

见 [tensorrt_edgellm/chat_templates/gemma4.json:1-34](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/chat_templates/gemma4.json#L1-L34)。C++ 运行时拿这个 JSON，把用户消息按 `roles[role].prefix + content + suffix` 拼接，最后追加 `generation_prompt`，就得到与 HF `apply_chat_template` 一致的 prompt 文本。

#### 4.3.3 源码精读

**① 总入口 `process_chat_template()`** —— 三级优先级的核心：

[tensorrt_edgellm/chat_template.py:352-364](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/chat_template.py#L352-L364) — 第一步先 `_try_write_hardcoded_template`，命中即返回；否则进入 Jinja 提取主流程；任意异常都兜底回硬编码。

**② 硬编码模板表** —— 哪些模型必须绕过 Jinja 提取、直接用内置 JSON：

[tensorrt_edgellm/chat_template.py:42-66](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/chat_template.py#L42-L66) — `_HARDCODED_TEMPLATE_MAP` 把 `model_type` 映射到 `chat_templates/` 下的 JSON 文件。需要硬编码的原因各异：phi4mm/qwen3_asr/qwen3_tts 的 Jinja 用了负数下标等 SandboxedEnvironment 不兼容的写法；qwen3_omni 家族是因为独立导出的检查点里 `chat_template.json` 不总能被 `AutoTokenizer` 重新加载，提取不可靠；gemma4 是因为独立检查点缺 `tokenizer.json` 导致提取失败、且 Unified 变体的 Jinja 会无条件吐出 thought 通道。注释把这些「为什么硬编码」逐条记录，是排错时的第一手线索。

**③ 内置模板的访问封装** —— 另一个独立的 `chat_templates/__init__.py` 提供按名查模板的能力：

[tensorrt_edgellm/chat_templates/__init__.py:26-31](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/chat_templates/__init__.py#L26-L31) — `get_template_path(model_identifier)` 返回 `<model_identifier>.json` 的路径（若存在）。注意它与 `chat_template.py` 里的 `_HARDCODED_TEMPLATE_MAP` 是两套入口：前者是「按任意标识符查文件」的通用工具，后者是「按 model_type 选定文件名」的导出期映射。

**④ Jinja 提取的「占位符差分」技巧** —— 这是理解长长提取主流程的钥匙。核心想法是：构造带可识别占位符（如 `<placeholder_system_prompt>`）的消息，让 tokenizer 渲染，再在渲染结果里**定位占位符**，它前面就是 prefix、后面就是 suffix：

[tensorrt_edgellm/chat_template.py:227-231](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/chat_template.py#L227-L231) — `_extract_prefix_suffix`：在渲染文本里 `find(placeholder)`，切片得到前后两段。

主流程用这套技巧分别提取 system/user/assistant 的 prefix/suffix、generation_prompt（[L474-492](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/chat_template.py#L474-L492) 处理 Phi-4MM 那种「替换而非追加末尾 EOS」的边角）、以及多模态 content_types（[L533-616](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/chat_template.py#L533-L616) 对 phi4mm/nemotron-omni/qwen3-omni 各有专门分支）。最终把所有字段汇总成 JSON 写盘（[L671-702](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/chat_template.py#L671-L702)）。

**⑤ Qwen3-Omni 的 thinking 覆盖** —— 这段较长的注释解释了一个「为何要主动改写提取结果」的非显然决策，值得细读：

[tensorrt_edgellm/chat_template.py:510-531](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/chat_template.py#L510-L531) — Qwen3-Omni 经 RLHF 不会吐 `<think>`，但其模板的 `enable_thinking=False` 分支会前置 `<think>\n\n</think>`，这会顶偏 Talker 硬编码的切片位置（`[:, :3]` 等），导致 prefill 错位。解法是强制把 `generation_prompt` 设成「不注入」变体、并清空 `generation_prompt_thinking`，使 C++ 运行时无论 `enableThinking` 标志如何都走同一路径。

**⑥ 兜底模板** —— 当一个模型既无硬编码、Jinja 提取又失败时，写一个最小的 `User: `/`Assistant: ` 模板保证运行时不至于无格式可用：

[tensorrt_edgellm/chat_template.py:271-300](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/chat_template.py#L271-L300) — `write_fallback_processed_chat_template`。

**⑦ 接入 sidecar 写出主流程** —— `write_runtime_artifacts` 在写完 config/embedding/tokenizer 后，按「先提取、再兜底」的顺序产出 chat 模板：

[tensorrt_edgellm/checkpoint/checkpoint_utils.py:921-925](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/checkpoint/checkpoint_utils.py#L921-L925) — 若 `processed_chat_template.json` 尚不存在且有 `model_dir`，调 `process_chat_template`；若仍不存在，调 `write_fallback_processed_chat_template`。两道关卡保证运行时目录里一定有这个文件。

#### 4.3.4 代码实践

**实践目标**：观察 `process_chat_template` 对一个真实模型产出的 JSON，并理解它的每个字段如何被 C++ 还原成 prompt。

**操作步骤**：

1. 准备一个本地 HF 模型目录（如 `Qwen/Qwen3-0.6B`，或任何带 `chat_template` 的模型）。
2. 用下面的最小 Python 片段（**示例代码**）直接调用导出端的提取函数，把结果打印出来：

   ```python
   # 示例代码：单独跑 chat 模板提取，观察产出的 JSON 结构
   from tensorrt_edgellm.chat_template import process_chat_template
   import json, os

   out_dir = "/tmp/chat_tpl_out"
   os.makedirs(out_dir, exist_ok=True)
   process_chat_template("Qwen/Qwen3-0.6B", out_dir)   # model_dir 可换成本地路径

   with open(os.path.join(out_dir, "processed_chat_template.json")) as f:
       tpl = json.load(f)
   print(json.dumps(tpl, indent=2, ensure_ascii=False))
   print("roles:", list(tpl["roles"].keys()))
   print("generation_prompt:", repr(tpl["generation_prompt"]))
   ```

3. 手工对照验证：用 transformers 在 Python 里渲染同一条消息，看 prefix/suffix 是否与产物一致：

   ```python
   # 示例代码：用 HF 渲染，对照 processed_chat_template 的字段
   from transformers import AutoTokenizer
   tok = AutoTokenizer.from_pretrained("Qwen/Qwen3-0.6B")
   rendered = tok.apply_chat_template(
       [{"role": "user", "content": "你好"}],
       tokenize=False, add_generation_prompt=True)
   print(rendered)
   # 期望：rendered == tpl["roles"]["user"]["prefix"] + "你好" + tpl["roles"]["user"]["suffix"] + tpl["generation_prompt"]
   ```

**需要观察的现象**：

- 步骤 2 打印的 JSON 里，`roles.system/user/assistant` 各有 `prefix`/`suffix`，Qwen3 这类 ChatML 模型的 prefix 会含 `<|im_start|>`、suffix 含 `<|im_end|>\n`。
- 步骤 3 中，手工按 `prefix + content + suffix + generation_prompt` 拼出的字符串应当与 `apply_chat_template` 的输出**逐字符一致**——这就是 C++ 运行时复现 prompt 的依据。

**预期结果**：两者一致即说明提取正确。若不一致（常见于带 thinking 通道或特殊 BOS 处理的模型），说明该模型可能需要走硬编码路径或额外边角处理——这正是 `_HARDCODED_TEMPLATE_MAP` 存在的原因。**若你在自定义模型上发现不一致，大概率需要仿照 `gemma4.json` 给 `chat_templates/` 加一个内置模板并在 `_HARDCODED_TEMPLATE_MAP` 注册。**

#### 4.3.5 小练习与答案

**练习 1**：为什么 phi4mm、qwen3_omni、gemma4 这些模型要绕过 Jinja 自动提取、直接用硬编码模板？

**参考答案**：因为它们的 Jinja 模板或检查点布局会让自动提取不可靠或出错——phi4mm 的模板用了 Jinja2 SandboxedEnvironment 不支持的负数下标；qwen3_omni 独立导出检查点的 `chat_template.json` 不总能被 `AutoTokenizer` 重新加载；gemma4 独立检查点缺 `tokenizer.json` 导致提取失败，且 Unified 变体的 Jinja 会无条件吐出 thought 通道。硬编码模板绕开这些坑，保证导出产物确定且正确。详见 [chat_template.py:42-66](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/chat_template.py#L42-L66) 的逐条注释。

**练习 2**：`_extract_prefix_suffix` 用「占位符差分」来定位 prefix/suffix。如果某个模型的模板把占位符 `<placeholder_user_text>` 渲染成了别的东西（比如做了 trim 或转义），这个技巧还能用吗？

**参考答案**：不一定能用。这就是为什么主流程里有大量兜底与边角处理：比如 `trim_content` 检测（[L660-669](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/chat_template.py#L660-L669)）专门判断模板是否对内容做了 `| trim`，若是则在 JSON 里置 `trim_content: true` 让 C++ 渲染器也跟着 trim，否则空白差异会导致分词结果与 HF 不一致。占位符差分是「主策略」，trim/BOS/thinking 等检测是「补丁」。

**练习 3**：`chat_templates/__init__.py` 的 `get_template_path()` 和 `chat_template.py` 的 `_HARDCODED_TEMPLATE_MAP` 都能定位内置模板，它们是什么关系？

**参考答案**：`get_template_path(identifier)` 是一个通用的「按文件名查 `chat_templates/<identifier>.json` 是否存在」的工具函数（[chat_templates/__init__.py:26-31](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/chat_templates/__init__.py#L26-L31)），对外暴露给任意调用方；而 `_HARDCODED_TEMPLATE_MAP` 是导出期 `process_chat_template` 内部专用的「`model_type` → 文件名」映射，决定哪些模型在导出时绕过 Jinja 提取。前者是「机制」（查文件），后者是「策略」（哪些模型用它）。两者读的是同一个 `chat_templates/` 目录。

---

## 5. 综合实践

把三项特性串起来，设计一个「在边缘上又快又省、且多轮对话格式正确」的部署任务。以一个中等规模 LLM（如 Qwen3-8B）为例：

1. **量化（含 FP8 KV）**：用 `tensorrt-edgellm-quantize llm --quantization nvfp4 --kv_cache_quantization fp8` 产出带 FP8 KV 元数据的检查点。
2. **词表裁剪**：用 `tensorrt-edgellm-reduce-vocab --reduced_vocab_size 64000`（注意：若 lm_head 是 INT4 量化，需取 128 的倍数）产出 `vocab_map.safetensors`。
3. **导出**：用 `tensorrt-edgellm-export <量化后检查点> <onnx目录> --reduced-vocab-dir <词表裁剪目录>`。导出完成后，检查 `<onnx目录>` 是否同时包含：`config.json`（里有 `reduced_vocab_size` 与 `kv_cache_dtype: "fp8"`）、`vocab_map.safetensors`、`processed_chat_template.json`。
4. **构建与推理**：用 `llm_build` 与 `llm_inference` 跑通，输入一个多轮对话 JSON（含 system + user），验证：
   - 输出文字正确（词表裁剪未破坏可输出性）；
   - KV 缓存显存相比 FP16 减半（可用 `nvidia-smi` 或运行时日志 `KVCacheManager(dtype=kFP8, ...)` 观察，见 [kvCacheManager.cpp:82-84](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/kvCacheManager.cpp#L82-L84)）；
   - 多轮对话的 prompt 拼接正确（chat 模板生效）。

**思考题**：如果在这个部署里再叠加系统提示 KV 缓存（u9-l2），会与第 1 步的 FP8 KV 冲突吗？为什么？（答：会，因为 capture/restoreKVCache 尚未实现 FP8 模板——这是把本讲三个特性「排列组合」时必须知道的兼容性边界。）

> 本综合实践需要 GPU、TensorRT 与联网下载数据集/模型；若环境不具备，至少把四条命令组装完整、并逐一解释每个参数与每一步产出的 sidecar，作为「源码阅读型实践」。

## 6. 本讲小结

- **词表裁剪**用一个 `vocab_map`（压缩 id → 全词表 id 的查表）把 `lm_head` 从 \(V\) 行砍到 \(V'\) 行，省权显存/算力/activation；分「离线选词（`reduce_vocab_size`，特殊 token 与 d2t 必留）→ 导出应用（按 lm_head 量化类型分发，INT4 必须在 repack 之前裁且要 128 倍数）→ 运行时还原（`mapReducedVocabToFullVocab`）」三阶段。
- **FP8 KV 缓存**是检查点元数据驱动的：量化阶段 `--kv_cache_quantization fp8` 烤进 `hf_quant_config.json`，导出期读 `config.quant.kv_cache_quant=="fp8"` 自动启用，给 q/k/v_proj 挂 per-tensor scale，运行时 KVCacheManager 用 `kFP8`（1 字节）替代 `kHALF`（2 字节）分配，**KV 显存直接减半**，Blackwell 上还有额外 attention 提速。
- **chat 模板**把 HF 的 Jinja 模板在导出期「编译」成 `processed_chat_template.json`（各 role 的 prefix/suffix + generation_prompt + content_types），供无 Jinja 引擎的 C++ 分词器拼接复现；按「硬编码模板 > Jinja 提取 > 兜底」三级优先级产出。
- 三者的共同身份是**导出期写出、运行期读入的 sidecar 契约**：`vocab_map.safetensors`、`config.json` 里的 `reduced_vocab_size`/`kv_cache_dtype`、`processed_chat_template.json`。
- 已知边界：EAGLE3 draft 不支持词表裁剪；EAGLE3/DFlash draft 强制关闭 FP8 KV；FP8 KV 与系统提示 KV 缓存目前互斥（capture/restore 未实现 FP8）。
- 生产前必验：词表裁剪要确认校准数据覆盖目标任务的 token；FP8 KV 要确认目标模型无精度退化（Qwen2.5-7B 系列已知有问题）；chat 模板在自定义模型上要对照 HF 渲染逐字符核对。

## 7. 下一步学习建议

- 想深入「INT4 权重为何不能按行切、repack 布局长什么样」：回看 u3-l3（量化权重格式与 sidecar）与本讲的 `_select_column_packed_int4`/`_select_pair_packed_int4_rows` 对照阅读。
- 想理解运行时 KV 缓存的完整生命周期（FP8 与 FP16 如何被 attention kernel 消费）：回看 u5-l5（KV 缓存与混合缓存管理）与本讲的 `KVCacheManager` 构造函数。
- 想做「新增一个 SM 架构的 FP8 FMHA」或「给系统提示缓存补 FP8 支持」：阅读 u8-l2（自定义 CUDA 算子）与 `cpp/kernels/contextAttentionKernels/` 下的 CuTe DSL FMHA runner，以及 `hybridCacheManager.cpp` 的 capture/restore 模板实例化点。
- 想接入一个全新模型（含其特有的 chat 模板）：先读 u9-l4（接入新模型架构），再结合本讲的 `_HARDCODED_TEMPLATE_MAP` 机制决定是否需要给它加内置模板。
- 建议动手的源码追踪任务：从 `tensorrt-edgellm-reduce-vocab` 的 `main` 出发，一路追到 C++ `mapReducedVocabToFullVocab`，画出「一个压缩 id 如何变成全词表 id」的完整跨语言调用链。
