# u2-l1 · index 解析与 KimiK2WeightManifest

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `model.safetensors.index.json` 的 `weight_map` 长什么样，以及 crate 如何把它解析成一棵「张量目录树」`KimiK2WeightManifest`。
2. 独立推导 Kimi-K2.6 checkpoint 里全部 `language_model.*` 张量的名字：顶层 3 个 + 61 层（第 0 层 dense、其余 60 层 MoE），并用公式算出总张量数。
3. 理解 `validate` 的两层合同：结构校验（61 层、60 个 MoE 层）与并行整除校验（专家对 EP、头与词表对 TP），以及 `with_parallel_shape` 为什么会在换拓扑时重新校验。
4. 写出调用 `KimiK2WeightManifest::from_index_json` 的单元测试，验证「名字齐全则通过、缺一个名字则报出那个名字」。

本讲是单元 2（权重加载）的第一讲：只处理**名字与结构**，不打开任何 shard 的字节——mmap 与 GPU 上载留给 u2-l4，rank 切片推导留给 u2-l2。

## 2. 前置知识

### 2.1 safetensors 与 index.json

大模型的权重通常切成几十个 `*.safetensors` 分片（shard）文件。safetensors 是一种「头部 JSON 元数据 + 尾部裸张量字节」的格式，每个分片头部记录了本文件内每个张量的名字、dtype 和形状。

问题是：推理引擎拿到张量名 `language_model.model.layers.7.mlp.experts.123.gate_proj.weight_packed` 时，怎么知道它躺在哪个分片文件里？答案就是索引文件 `model.safetensors.index.json`，它的核心字段 `weight_map` 是一张「张量名 → 分片文件名」的表：

```json
{
  "metadata": { "total_size": ... },
  "weight_map": {
    "language_model.model.embed_tokens.weight": "model-00001-of-00064.safetensors",
    "language_model.model.layers.0.input_layernorm.weight": "model-00001-of-00064.safetensors",
    ...
  }
}
```

### 2.2 什么是 manifest

**manifest（清单）**：把扁平的 `weight_map` 字典重新组织成与模型结构同构的类型化树——「顶层 3 个张量 + 61 层，每层有 attention 部分，attention 之外是 dense MLP 或 MoE」。它是 checkpoint 与后续 rank 切分（u2-l2）、显存装载（u2-l4）之间的中间表示。名字本身就是结构：`layers.0` 是第 0 层，`mlp.experts.123` 是第 123 号专家。

### 2.3 前讲回顾（u1-l2 / u1-l3）

- 几何常量全部写死在 [src/config.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L13-L41)：61 层（[src/config.rs:15-17](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L15-L17)）、384 个路由专家（[src/config.rs:30](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L30)）。
- `KimiK2ParallelShape`（[src/config.rs:287-295](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L287-L295)）由 `tp_world × dp_world = ep_world` 推出每 rank 本地切片，两种合法拓扑 `tp8_ep8()` / `tp1_dp8()` 的 `local_experts` 都是 48。
- crate 采用扁平模块布局（`weights.rs` 模块根 + `weights/` 目录，无 `mod.rs`），`weights` 子树整体挂在 `kimi-k2` feature 之后（[src/lib.rs:33-34](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/lib.rs#L33-L34)）。

### 2.4 一个直觉比喻

把 `weight_map` 想成一本没有目录的厚字典：8 万多个词条按字母序堆着。`KimiK2WeightManifest` 就是先按「模型图纸」把字典抄成一张带章节的目录卡——图纸来自 `config.rs` 的编译期常量。抄写过程中任何一页缺词条，立刻在「那一页」报错，而不是等到 GPU 上载时才炸。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
|------|-----------|
| [src/weights.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights.rs) | 模块根：常量 `KIMI_K2_WEIGHT_INDEX`、`TEXT_PREFIX`，子模块声明与再导出 |
| [src/weights/manifest.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs) | **本讲主文件**：manifest 类型树、`from_index_json` 解析、`validate` 校验（以及本讲只预告的 rank 切分方法） |
| [src/weights/load.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs) | text-only 门 `ensure_text_only_model_index`、切片类型 `KimiTensorLoadSlice` 等（GPU 装载函数本身属 u2-l4） |
| [src/weights/tests.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/tests.rs) | 现有测试：`tiny_manifest` 工厂、缺失张量报错测试，是本讲实践的模板 |
| [src/runner/bringup.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/bringup.rs) | manifest 的真实消费者：启动时解析 + `with_parallel_shape` 换拓扑 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**weight_map 解析**（4.1）、**layer manifest 结构**（4.2）、**text-only 门与校验**（4.3）。

### 4.1 weight_map 解析：从 index.json 到张量目录

#### 4.1.1 概念说明

解析要解决三个问题：

1. **找到索引文件**：crate 约定索引固定叫 `model.safetensors.index.json`，放在模型目录根部。
2. **只信名字，不碰字节**：`from_index_json` 只读 `weight_map` 里的字符串键值，不打开任何 `.safetensors` 分片。这使它可以在没有权重的机器上单测（现有测试就是这么做的）。
3. **确定性的错误报告**：用 `BTreeMap`（有序 map）保存中间结果，任何缺失的名字都能立刻定位到具体张量名，报错信息可直接用于排障。

#### 4.1.2 核心流程

```text
from_model_dir(model_path)                     # 入口：给目录
  └─ from_index_file(model_path/model.safetensors.index.json)
       ├─ fs::read_to_string → serde_json 解析成 serde_json::Value
       └─ from_index_json(json)
            ├─ 取 json.weight_map，必须是对象且每个 value 是字符串
            ├─ 全部条目收进 BTreeMap<张量名, shard 文件名>
            ├─ 取 3 个顶层张量（embed_tokens / norm / lm_head），缺失即报错
            ├─ for layer_idx in 0..61：
            │    ├─ attention_manifest(...)        # 9 个名字
            │    └─ layer_idx < 1 ? dense_mlp_manifest : moe_layer_manifest
            ├─ 统计 text_tensor_count（language_model. 前缀的条目数）
            └─ validate()  ← 全部通过才返回 manifest
```

#### 4.1.3 源码精读

**索引文件名与前缀常量**定义在模块根：

[src/weights.rs:61-62](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights.rs#L61-L62) 定义了 `KIMI_K2_WEIGHT_INDEX = "model.safetensors.index.json"` 与 `TEXT_PREFIX = "language_model."`——前者是索引文件名，后者是「文本权重」的身份前缀，4.3 节会再次用到。

**入口三级跳**：

[src/weights/manifest.rs:183-193](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L183-L193) 中，`from_model_dir` 只是拼出索引路径，`from_index_file` 负责读文件与 JSON 解析（两层 `with_context` 分别报「读失败」「解析失败」），真正的工作在 `from_index_json`。

**weight_map 的读取与净化**：

[src/weights/manifest.rs:196-206](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L196-L206) 先用 `json.get("weight_map")` 拿到映射，缺失则报 `Kimi safetensors index missing weight_map`；然后逐条校验每个 value 必须是字符串（分片文件名），全部插入 `BTreeMap<&str, &str>`。注意这里**没有**过滤前缀——所有条目都进表，前缀过滤只发生在「按名字查找」和 `text_tensor_count` 统计时。

**顶层 3 个张量**：

[src/weights/manifest.rs:208-210](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L208-L210) 按固定名字取词嵌入、最终归一化与输出头：

| manifest 字段 | checkpoint 张量名 |
|---|---|
| `token_embedding` | `language_model.model.embed_tokens.weight` |
| `final_norm` | `language_model.model.norm.weight` |
| `lm_head` | `language_model.lm_head.weight` |

注意 `lm_head` 的路径与另外两个不同：它直接挂在 `language_model.` 下而没有 `model.` 中段——这是 HF 命名习惯，代码里按完整字符串逐字匹配。

**查找函数 `tensor`**：

[src/weights/manifest.rs:722-730](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L722-L730) 是所有取名的终点：`BTreeMap` 查不到就报 `Kimi text manifest missing tensor {name}`。每个 `KimiTensorEntry`（[src/weights/manifest.rs:8-11](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L8-L11)）只携带 `name` 与 `shard` 两个字符串——名字到分片的映射就是它唯一记录的信息。

#### 4.1.4 代码实践：列出真实（或手写）weight_map 的张量名

1. **实践目标**：亲眼看到 `weight_map` 的键集合，并与 4.2 节的命名规则对上。
2. **操作步骤**：
   - 有真实 Kimi-K2 权重目录时（示例代码，路径按需替换）：

     ```bash
     python3 -c "
     import json
     wm = json.load(open('models/Kimi-K2/model.safetensors.index.json'))['weight_map']
     text = sorted(k for k in wm if k.startswith('language_model.'))
     print('total entries:', len(wm))
     print('language_model.* entries:', len(text))
     print('shards:', len(set(wm.values())))
     print('\n'.join(text[:12]))
     "
     ```

   - 没有权重时，把上面的脚本指向任意一个 safetensors 索引（例如 HuggingFace 上任何分片模型），只看结构不看数值。
3. **需要观察的现象**：`language_model.*` 条目数；去重后的分片文件数（真实 Kimi-K2.6 checkpoint 是 64 个分片，与测试工厂里写死的 `model-00001-of-00064.safetensors` 一致，见 [src/weights/tests.rs:293-298](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/tests.rs#L293-L298)）；打印出的前几个名字是否都是 `language_model.model.layers.0.*` 形态。
4. **预期结果**：真实 checkpoint 的 `language_model.*` 计数应为 208,215（推导见 4.2.3 的计数表）。若你的计数不同，先检查是否混入了非文本（如视觉塔）张量前缀。
5. 本讲作者未在本地运行该脚本，具体数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`from_index_json` 对一个完全没有 `weight_map` 键的 JSON 会怎么报错？对 `weight_map` 里某条 value 是数字的 JSON 呢？

**答案**：前者命中 [src/weights/manifest.rs:197-199](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L197-L199)，报 `Kimi safetensors index missing weight_map`；后者命中 [src/weights/manifest.rs:201-205](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L201-L205)，报 `weight_map entry {name} is not a shard string`，报错里带具体条目名。

**练习 2**：为什么中间表用 `BTreeMap` 而不是 `HashMap`？

**答案**：两者在这里的正确性相同（按键查找），但 `BTreeMap` 的迭代有序，使任何依赖遍历顺序的逻辑（例如后续按 shard 分组）天然确定，报错与测试输出可复现；这与仓库在 [src/weights/load.rs:90](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L90)（`tensors: BTreeMap`）与 `rank_sliced_load_plan`（[src/weights/manifest.rs:332-340](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L332-L340) 按 shard 的 `BTreeMap` 分组）的用法一致。

### 4.2 layer manifest 结构：61 层的名字树与计数

#### 4.2.1 概念说明

manifest 的主体是 61 个 `KimiLayerManifest`。每层固定由两部分组成：

- **attention 部分**：MLA（Multi-head Latent Attention）的 9 个张量，所有层都有；
- **kind 部分**：`KimiLayerKindManifest` 枚举，第 0 层是 `Dense`（普通 MLP），其余 60 层是 `Moe`。

为什么第 0 层特殊？这是 Kimi-K2.6 的设计：config 里的 `first_k_dense_replace = 1`（probe 在 [src/config.rs:125-127](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L125-L127) 强制它等于 1），前 1 层用 dense MLP，之后全部换成 MoE。manifest 的分层判断与这个 config 语义严格对应。

MoE 层的 routed expert 权重是 INT4 量化的：每个投影（gate/up/down）在 checkpoint 里是一个 `KimiInt4ProjectionManifest`，成对持有 `weight_packed`（打包的 4-bit 权重）与 `weight_scale`（每组缩放）。

#### 4.2.2 核心流程

```text
for layer_idx in 0..61:
    attention = attention_manifest(tensors, layer_idx)      # 9 个固定名字
    kind =
      if layer_idx < KIMI_K2_DENSE_LAYERS ( = 1 ):
          Dense( dense_mlp_manifest )                        # 3 个名字
      else:
          Moe( moe_layer_manifest )                          # 2 + 3 + 384×3×2 个名字
             ├─ router:        mlp.gate.weight + mlp.gate.e_score_correction_bias
             ├─ shared_experts: gate/up/down_proj.weight
             └─ routed_experts: for expert_idx in 0..384:
                    int4_projection_manifest(gate_proj / up_proj / down_proj)
                      → {projection}.weight_packed + {projection}.weight_scale
```

统一命名模板：`language_model.model.layers.{layer_idx}.{suffix}`，由 [src/weights/manifest.rs:711-720](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L711-L720) 的 `layer_tensor` 拼出。

#### 4.2.3 源码精读

**类型树**（结构即文档）：

- [src/weights/manifest.rs:73-78](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L73-L78) `KimiLayerManifest { layer_idx, attention, kind }`；
- [src/weights/manifest.rs:67-71](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L67-L71) `KimiLayerKindManifest` 枚举只有 `Dense` / `Moe` 两个变体；
- [src/weights/manifest.rs:60-65](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L60-L65) `KimiMoeLayerManifest { router, shared_experts, routed_experts }`；
- [src/weights/manifest.rs:46-58](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L46-L58) INT4 投影与单个专家：`KimiInt4ProjectionManifest`（packed + scale）聚成 `KimiRoutedExpertManifest`（三个投影）。

**分层判断**在主循环里只有一行：

[src/weights/manifest.rs:212-224](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L212-L224) 对 `0..KIMI_K2_LAYERS`（61）逐层构造：`layer_idx < KIMI_K2_DENSE_LAYERS`（=1）走 `dense_mlp_manifest`，否则走 `moe_layer_manifest`。

**attention 的 9 个名字**（与 MLA 结构一一对应，u5-l2 会逐个用到）：

[src/weights/manifest.rs:623-646](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L623-L646) `attention_manifest` 固定取这 9 个后缀：

| manifest 字段 | 后缀（补全 `language_model.model.layers.{i}.` 前缀） |
|---|---|
| `input_layernorm` | `input_layernorm.weight` |
| `q_a_proj` | `self_attn.q_a_proj.weight` |
| `q_a_layernorm` | `self_attn.q_a_layernorm.weight` |
| `q_b_proj` | `self_attn.q_b_proj.weight` |
| `kv_a_proj_with_mqa` | `self_attn.kv_a_proj_with_mqa.weight` |
| `kv_a_layernorm` | `self_attn.kv_a_layernorm.weight` |
| `kv_b_proj` | `self_attn.kv_b_proj.weight` |
| `o_proj` | `self_attn.o_proj.weight` |
| `post_attention_layernorm` | `post_attention_layernorm.weight` |

**MoE 层的名字**：

[src/weights/manifest.rs:659-683](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L659-L683) `moe_layer_manifest` 取 router 两个名字（`mlp.gate.weight`、`mlp.gate.e_score_correction_bias`）、共享专家三个名字（`mlp.shared_experts.{gate,up,down}_proj.weight`），再对 `0..KIMI_K2_ROUTED_EXPERTS`（384）逐个构造路由专家；[src/weights/manifest.rs:685-709](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L685-L709) `routed_expert_manifest` / `int4_projection_manifest` 拼出 `mlp.experts.{expert_idx}.{gate,up,down}_proj.{weight_packed,weight_scale}`。

注意：`int4_projection_manifest` 只要求 `weight_packed` 与 `weight_scale` 两个名字。真实 checkpoint 里每个投影还有第三个张量 `weight_shape`，但**任何 kernel 都不读它**——测试 [src/weights/tests.rs:167-174](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/tests.rs#L167-L174) 明确断言 load plan 里不得出现它（引用了 issue #234）。manifest 对多余的 checkpoint 张量采取「不认识就不碰」的态度。

**张量计数（可与代码交叉验证）**：

以「每层 9 个 attention + 第 0 层 3 个 dense + 每 MoE 层 \(2+3\) 个 router/shared + 每专家每投影 2 个 INT4 名字」为生成规则，自造的最小合法 weight_map 共有：

\[ 3 + 61\times9 + 3 + 60\times(5 + 384\times3\times2) = 555 + 60\times2309 = 139{,}095 \]

而真实 checkpoint 因为每投影多一个 `weight_shape`，`text_tensor_count` 为：

\[ 555 + 60\times(5 + 384\times3\times3) = 555 + 207{,}660 = 208{,}215 \]

208,215 正是测试工厂 `tiny_manifest` 写死的 `text_tensor_count`（[src/weights/tests.rs:270-277](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/tests.rs#L270-L277)）——文档、代码、checkpoint 三方对上了。

**text_tensor_count 的统计口径**：

[src/weights/manifest.rs:227-236](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L227-L236) 在构造完 layers 后，用 `weight_map.keys().filter(starts_with(TEXT_PREFIX)).count()` 统计文本张量数——它数的是**原始 weight_map**里带 `language_model.` 前缀的键，包括 manifest 根本不引用的 `weight_shape`。

#### 4.2.4 代码实践：按规则预测名字，写单测验证解析

1. **实践目标**：证明你真的掌握了命名规则——先手写预测，再用 `from_index_json` 验证。
2. **操作步骤**：
   - **第一步（纸上）**：不看源码，写出以下 5 组张量的完整名字：`embed_tokens`、`final norm`、`lm_head`、第 0 层的 dense `gate_proj`、第 1 层专家 7 的 `down_proj.weight_scale`。
   - **第二步（代码）**：在 `src/weights/tests.rs` 中添加一个生成完整名字集合的辅助函数与两个测试（**示例代码**，写入测试文件属于练习内容）：

     ```rust
     // 示例代码：按 manifest.rs 的命名规则生成全部必需名字
     fn full_text_names() -> Vec<String> {
         let mut names = vec![
             "language_model.model.embed_tokens.weight".to_owned(),
             "language_model.model.norm.weight".to_owned(),
             "language_model.lm_head.weight".to_owned(),
         ];
         const ATTN: [&str; 9] = [
             "input_layernorm.weight",
             "self_attn.q_a_proj.weight",
             "self_attn.q_a_layernorm.weight",
             "self_attn.q_b_proj.weight",
             "self_attn.kv_a_proj_with_mqa.weight",
             "self_attn.kv_a_layernorm.weight",
             "self_attn.kv_b_proj.weight",
             "self_attn.o_proj.weight",
             "post_attention_layernorm.weight",
         ];
         for layer in 0..KIMI_K2_LAYERS {
             for s in ATTN {
                 names.push(format!("language_model.model.layers.{layer}.{s}"));
             }
             if layer < KIMI_K2_DENSE_LAYERS {
                 for s in ["mlp.gate_proj.weight", "mlp.up_proj.weight", "mlp.down_proj.weight"] {
                     names.push(format!("language_model.model.layers.{layer}.{s}"));
                 }
             } else {
                 names.push(format!("language_model.model.layers.{layer}.mlp.gate.weight"));
                 names.push(format!(
                     "language_model.model.layers.{layer}.mlp.gate.e_score_correction_bias"
                 ));
                 for s in [
                     "mlp.shared_experts.gate_proj.weight",
                     "mlp.shared_experts.up_proj.weight",
                     "mlp.shared_experts.down_proj.weight",
                 ] {
                     names.push(format!("language_model.model.layers.{layer}.{s}"));
                 }
                 for expert in 0..KIMI_K2_ROUTED_EXPERTS {
                     for proj in ["gate_proj", "up_proj", "down_proj"] {
                         for part in ["weight_packed", "weight_scale"] {
                             names.push(format!(
                                 "language_model.model.layers.{layer}.mlp.experts.{expert}.{proj}.{part}"
                             ));
                         }
                     }
                 }
             }
         }
         names
     }

     #[test]
     fn from_index_json_accepts_generated_text_weight_map() {
         let mut weight_map = serde_json::Map::new();
         for name in full_text_names() {
             weight_map.insert(name, json!("model-00001-of-00064.safetensors"));
         }
         let index = json!({ "metadata": {"total_size": 1}, "weight_map": weight_map });
         let manifest = KimiK2WeightManifest::from_index_json(&index).unwrap();
         assert_eq!(manifest.layers.len(), KIMI_K2_LAYERS);
         assert_eq!(manifest.text_tensor_count, 139_095); // 无 weight_shape 的生成集
         assert!(matches!(manifest.layers[0].kind, KimiLayerKindManifest::Dense(_)));
         assert!(matches!(manifest.layers[1].kind, KimiLayerKindManifest::Moe(_)));
     }
     ```

   - **第三步（运行）**：`cargo test --release -p pegainfer-kimi-k2 --features kimi-k2 --lib weights::`（`weights` 模块在 `kimi-k2` feature 之后；编译会连带构建 `pegainfer-kernels` 的 CUDA 部分，需要 nvcc 工具链，但这些单测本身不占用 GPU）。
3. **需要观察的现象**：生成的名字集是否恰好让解析通过；`layers.len()` 与两种 layer kind 的分布；`text_tensor_count` 是否等于 139,095。
4. **预期结果**：测试通过，说明「manifest 需要的名字 ⊆ 生成的名字集」且分层规则正确；若把 `weight_shape` 也生成进去，`text_tensor_count` 会变成 208,215 而 manifest 依然通过（多余张量被忽略）。本讲作者未运行，**待本地验证**。
5. 若第一步的手写预测与生成器输出有出入，回到 4.2.2 的命名模板核对。

#### 4.2.5 小练习与答案

**练习 1**：第 0 层有 `mlp.gate.weight`（router）吗？第 3 层有 `mlp.gate_proj.weight`（dense）吗？

**答案**：都没有。第 0 层走 `dense_mlp_manifest`（[src/weights/manifest.rs:648-657](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L648-L657)），只取 `gate/up/down_proj`；第 3 层（\(\ge 1\)）走 `moe_layer_manifest`，router 名字是 `mlp.gate.weight`，不存在 dense 的 `mlp.gate_proj.weight`。若 checkpoint 缺这些名字，`tensor` 直接报 missing。

**练习 2**：一个 MoE 层的 manifest 至少引用多少个张量？

**答案**：\(2 + 3 + 384\times3\times2 = 2313\) 个：router 2 个、共享专家 3 个、384 个专家各 3 个投影、每投影 packed + scale 两个（[src/weights/manifest.rs:659-683](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L659-L683) 与 [src/weights/manifest.rs:698-709](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L698-L709)）。

**练习 3**：`e_score_correction_bias` 是谁的偏置？它和 router 的 `gate.weight` 是什么关系？

**答案**：它是 noaux_tc 路由的**打分校正偏置**，名字挂在 `mlp.gate.` 下（[src/weights/manifest.rs:670-674](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L670-L674)）。`gate.weight` 把 hidden 投影成 384 维 logits，`e_score_correction_bias` 在选 top-8 时叠加、但不参与概率归一——这正是 u6-l1「noaux_tc」名字里 no-aux（不进辅助损失/归一）的含义，本讲只需记住它是 router 的第二个必需张量。

### 4.3 text-only 门与 validate 整除校验

#### 4.3.1 概念说明

两个互不相同的校验容易被混淆，本讲把它们分清：

1. **text-only 门**（`ensure_text_only_model_index`，在 load.rs）：确认索引里**存在**文本张量（至少一个 `language_model.*` 条目）。它保证「这是一个文本模型（或至少含文本部分）的索引」，完全空白的索引立刻拒绝。至于索引里多出来的其他张量（例如多模态模型的视觉塔权重），门并不拒绝——它们只是永远不被 manifest 引用、也永远不被加载。
2. **validate**（manifest 私有方法）：结构合同 + 并行形状合同。`from_index_json` 结束时和 `with_parallel_shape` 换拓扑时都会调用。

`validate` 的并行整除校验是纯算术：**专家数对 EP 整除、头数与词表数对 TP 整除**。这一步不通过，后面 u2-l2 的 rank 切分（`local_experts = 384 / ep_world`、`heads_per_tp = 64 / tp_world`、`vocab_per_tp = 163840 / tp_world`）就会产生零头甚至除零。

#### 4.3.2 核心流程

```text
启动期（bringup）:
  ensure_text_only_model_index(model_path)
    ├─ KimiK2WeightManifest::from_model_dir(...)   # 解析 + 默认 tp8_ep8 + validate
    └─ text_tensor_count == 0 ? 报错 : 返回 manifest
  manifest.with_parallel_shape(实际拓扑 shape)     # 替换 parallel 后再次 validate

validate(manifest):
  ├─ layers.len() == 61 ?
  ├─ MoE 层数 == 60 ?
  └─ ep_world > 0 且 384 % ep_world == 0
              且 64 % tp_world == 0 且 163840 % tp_world == 0 ?
```

#### 4.3.3 源码精读

**text-only 门**：

[src/weights/load.rs:67-73](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L67-L73) `ensure_text_only_model_index` 调用 `from_model_dir` 拿到 manifest，若 `text_tensor_count == 0` 则报 `Kimi safetensors index contains no language_model tensors`。这是启动链路上第一道与权重的接触：真实调用点在 [src/runner/bringup.rs:96-97](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/bringup.rs#L96-L97)，紧跟着 `with_parallel_shape(shape)` 把默认拓扑换成实际启动拓扑。

**with_parallel_shape：一次替换、二次校验**：

[src/weights/manifest.rs:241-245](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L241-L245) 消费 `self`、替换 `parallel` 字段后**重新 `validate`**，失败即返回错误。它是 builder 风格：`from_index_json` 默认写入 `tp8_ep8()`（[src/weights/manifest.rs:235](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L235)），TP1/DP8 启动路径靠它切换。

**validate 三连**：

[src/weights/manifest.rs:247-273](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L247-L273)：

- L248-L252：层数必须等于 `KIMI_K2_LAYERS`（61）——`from_index_json` 的循环本身就固定生成 61 层，这条在校验来自其他路径构造的 manifest（如测试工厂）时起作用；
- L253-L261：`kind` 为 `Moe` 的层数必须等于 `KIMI_K2_MOE_LAYERS`（60）；
- L262-L271：并行合同，即

\[ 384 \bmod ep\_world = 0,\quad 64 \bmod tp\_world = 0,\quad 163840 \bmod tp\_world = 0,\quad ep\_world > 0 \]

  失败时报 `Kimi parallel shape TP{tp}/DP{dp}/EP{ep} does not evenly divide model dimensions`。

对照两种合法拓扑：`tp8_ep8`（\(tp=8, ep=8\)）与 `tp1_dp8`（\(tp=1, dp=8, ep=8\)）都满足三条整除；假想的 `tp3` 会在 \(64 \bmod 3 \ne 0\) 处被拦下。

**为什么头和词表跟 TP、专家跟 EP**：TP（张量并行）把注意力头与词表行切到各 rank——头数 64、词表 163,840 必须被 tp_world 整除；EP（专家并行）把 384 个完整专家划给各 rank——专家数必须被 ep_world 整除。这条规则正是 u1-l2 `KimiK2ParallelShape::new`（[src/config.rs:325-335](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L325-L335)）做除法的前提，validate 提前把不整除的拓扑挡在除法之前。

#### 4.3.4 代码实践：缺名字与坏拓扑的负路径单测

1. **实践目标**：验证两个报错路径真的会报出「正确的内容」——错误信息本身就是诊断工具。
2. **操作步骤**：继续在 `src/weights/tests.rs` 添加两个测试（**示例代码**）：

   ```rust
   // 示例代码：缺一个必需名字 → 报错信息必须带那个名字
   #[test]
   fn from_index_json_reports_missing_final_norm() {
       let mut weight_map = serde_json::Map::new();
       for name in full_text_names() {
           if name != "language_model.model.norm.weight" {
               weight_map.insert(name, json!("model-00001-of-00064.safetensors"));
           }
       }
       let index = json!({ "weight_map": weight_map });
       let err = KimiK2WeightManifest::from_index_json(&index).unwrap_err();
       assert!(err.to_string().contains("language_model.model.norm.weight"));
   }

   // 示例代码：不整除的并行形状 → validate 拒绝
   #[test]
   fn with_parallel_shape_rejects_tp3() {
       let manifest = tiny_manifest();
       let bad = KimiK2ParallelShape {
           tp_world: 3, dp_world: 1, ep_world: 3,
           heads_per_tp: KIMI_K2_HEADS / 3,
           local_experts: KIMI_K2_ROUTED_EXPERTS / 3,
           vocab_per_tp: KIMI_K2_VOCAB / 3,
       };
       let err = manifest.with_parallel_shape(bad).unwrap_err();
       assert!(err.to_string().contains("does not evenly divide"));
   }
   ```

   运行方式同 4.2.4。第一个测试与现有测试 [src/weights/tests.rs:198-207](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/tests.rs#L198-L207) 同构（那是个只有 1 个条目的极简版），可以对照阅读。
3. **需要观察的现象**：错误字符串里出现被删掉/不整除的具体对象；`with_parallel_shape` 返回的是 `Err` 而不是 panic。
4. **预期结果**：两个测试通过。注意第二个测试直接构造 `KimiK2ParallelShape` 结构体（字段是 `pub(crate)`，crate 内测试可访问），因为公开构造器只有 `tp8_ep8`/`tp1_dp8` 两个合法拓扑——**待本地验证**。
5. 思考：如果删掉的不是 `norm.weight` 而是某个专家的 `weight_scale`，报错会在哪一层哪个专家上触发？（答案见 4.3.5 练习 2。）

#### 4.3.5 小练习与答案

**练习 1**：一个索引的 `weight_map` 只有 100 个 `language_model.*` 条目，`from_index_json` 会报什么？

**答案**：报 `Kimi text manifest missing tensor {name}`，`{name}` 是第一个找不到的必需名字——解析顺序是顶层 3 个先查，然后第 0 层 attention、第 0 层 dense、第 1 层 attention、第 1 层 router……所以 100 个条目大概率在 `language_model.model.embed_tokens.weight` 或 `language_model.lm_head.weight` 附近就断了。而 text-only 门（`ensure_text_only_model_index`）根本轮不到执行：`from_model_dir` 内部已经失败。

**练习 2**：删除第 41 层第 200 号专家的 `up_proj.weight_scale`，报错信息长什么样？什么时候触发？

**答案**：`Kimi text manifest missing tensor language_model.model.layers.41.mlp.experts.200.up_proj.weight_scale`。触发时机在 `moe_layer_manifest` 遍历到第 41 层、`routed_expert_manifest` 构造该专家的 `up_proj` 时（[src/weights/manifest.rs:685-709](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L685-L709)）——注意 experts 是按 `0..384` 顺序构造的，所以前面的 199 个专家必须全部完整，错误才会轮到它。

**练习 3**：`from_index_json` 会校验张量的 dtype 或形状吗？

**答案**：不会。manifest 阶段只看名字与分片归属，不打开任何 shard；dtype 与形状的校验发生在装载时——`KimiTensorLoadSlice::local_shape`（[src/weights/load.rs:13-33](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L13-L33)）会在切片时核对行/列范围是否落在真实形状内。这是刻意的分层：名字合同便宜、可无权重单测；字节合同昂贵、留给 u2-l4。

## 5. 综合实践

**任务：写一个「checkpoint ↔ manifest 双向对账器」测试。**

把 4.2.4 与 4.3.4 的内容串起来，在 `src/weights/tests.rs` 完成三件事：

1. **正向**：用 `full_text_names()` 生成完整名字集，随机（固定种子即可）抽 5 个名字，逐个确认 `from_index_json` 成功后 manifest 的对应字段（如 `layers[1].kind` 里 MoE 变体的 `routed_experts` 某条目）指向同一名字——练习「预测 → 解析 → 回读」闭环。
2. **计数对账**：向生成集追加 `weight_shape` 三件套（每专家每投影多 1 个名字）后再构造索引，断言 `manifest.text_tensor_count == 208_215`，且解析依旧成功——验证「多余张量被忽略」与 4.2.3 的计数公式。
3. **负向**：删掉 `language_model.model.layers.1.mlp.shared_experts.down_proj.weight`，断言报错字符串恰好等于这个名字；再用非法 `KimiK2ParallelShape`（如 `tp_world: 3`）调 `with_parallel_shape`，断言 `does not evenly divide`。

完成后运行：

```bash
cargo test --release -p pegainfer-kimi-k2 --features kimi-k2 --lib weights::
```

**验收标准**：全部测试通过；能口头回答「manifest 记录了什么（名字+shard）、没记录什么（dtype/形状/字节）、校验了什么（结构 61/60 + 整除）、没校验什么（数值正确性）」。若在真实 8 GPU 机器上，可额外对比 `tiny_manifest`（[src/weights/tests.rs:223-278](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/tests.rs#L223-L278)）的 `text_tensor_count` 与 4.1.4 脚本在真实 checkpoint 上数出的值是否一致（都应为 208,215）。

## 6. 本讲小结

- `from_model_dir → from_index_file → from_index_json` 三级入口把 `model.safetensors.index.json` 的 `weight_map`（张量名 → 分片文件）收进有序 `BTreeMap`，全程只读名字、不打开任何 shard 字节。
- manifest 是一棵与模型同构的名字树：顶层 3 个张量 + 61 层（第 0 层 dense、`layer_idx < 1` 分界）+ 每 MoE 层 2 router + 3 shared + 384 专家 × 3 投影 × 2 个 INT4 名字（`weight_packed`/`weight_scale`）。
- `text_tensor_count` 按 `language_model.` 前缀统计原始索引：自造最小集 139,095，真实 checkpoint 因多出 `weight_shape` 而是 208,215；`ensure_text_only_model_index` 用它拒绝完全没有文本张量的索引。
- `validate` 是双重合同：结构（61 层、60 MoE 层）+ 并行整除（384 % EP、64 % TP、163840 % TP）；`with_parallel_shape` 换拓扑（默认 tp8_ep8 → 实际启动形状）时重新校验。
- 报错即诊断：缺什么名字、哪条整除失败，错误字符串都带具体对象；`from_index_json` 可在无权重机器上单测。

## 7. 下一步学习建议

- **u2-l2（rank 切分计划）**：本讲刻意绕过了 `rank_plan` / `rank_weight_names` / `rank_sliced_load_plan`（[src/weights/manifest.rs:275-490](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L275-L490)）——它们在 manifest 之上做 TP 头区间、EP 专家区间、词表区间的推导，是下一讲的全部内容。可先自行浏览 `KimiRankWeightPlan` 的三个 `Range` 字段。
- **u2-l3 / u2-l4**：INT4 权重如何重排成 Marlin 布局、mmap 切片如何上载 GPU，完成「名字 → 显存字节」的最后一公里。
- 顺带阅读 [src/weights/tests.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/tests.rs) 的 `rank_sliced_load_plan_applies_tp8_ep8_slices`（L62-L182），预习下讲将出现的 `KimiTensorLoadSlice::RowRange/ColRange` 语义。
