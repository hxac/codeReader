# 双阶段优化 profile 与模型类型分发

## 1. 本讲目标

本讲承接 u4-l1（构建器八阶段流程），深入构建的第 6 阶段——**优化 profile 设置**。学完后你应该能够：

1. 理解为什么一个 LLM engine 需要同时配 **两套** 优化 profile（context/prefill 与 generation/decode），以及这两套 profile 在形状上的本质差异。
2. 读懂 `setupLLMOptimizationProfiles` 的「决策树」，知道 vanilla / 投机解码 / DFlash / Gemma4-MTP / LoRA 各自走哪条分支。
3. 搞清楚「模型类型检测」第 4 阶段如何在 `lora_model.onnx` 与 `model.onnx` 之间二选一。
4. 理解视觉编码器为何是**另一个 builder**（`VisualBuilder`），它只配**单 profile**，且按 `model_type` 在 5 个 `setup*Profile` 函数间分发。
5. 纠正一个常见误解：代码里**并不存在** `setupVLMProfiles` 函数，VLM 的语言侧复用的是 vanilla 解码 profile。

> 关键认知：**profile = 告诉 TensorRT「这个动态输入维度在运行时会在什么范围内变化」**。TensorRT 据此为每个维度组合挑选并预编译最优 kernel。配错 profile，要么运行时报「超出范围」，要么 engine 跑得慢——因为优化点选错了。

---

## 2. 前置知识

### 2.1 什么是优化 profile（optimization profile）

TensorRT 允许输入张量有「动态维度」（dynamic shape），比如序列长度可以是任意值。但 TensorRT 需要知道这个动态维度的 **min / opt / max** 三个边界：

- **min**：运行时该维度的最小值；
- **opt**：运行时最常见的值，TensorRT 会**重点围绕它做 kernel 选优**；
- **max**：运行时允许的最大值，决定 engine 能承载的形状上界（也决定显存上界）。

三者必须满足 `min <= opt <= max`。构建器里这个校验由 `checkOptimizationProfileDims` 完成。

一个 engine 里可以挂**多个** profile。运行时通过「profile 索引」在它们之间切换——这正是 EdgeLLM 双阶段设计的根基。

### 2.2 LLM 推理的两个阶段

一次 LLM 生成在计算上分为截然不同的两段：

- **Prefill（上下文/Context 阶段）**：一次性把整段 prompt（可能上千个 token）喂进模型，算出每个位置的隐状态，并把 key/value 写进 KV 缓存。这一步是**批量、大矩阵乘**，算力受限（compute-bound）。
- **Decode（生成/Generation 阶段）**：每一步只喂**上一步生成的 1 个 token**，用已有 KV 缓存做注意力，吐出下一个 token。这一步是**单 token、访存受限**（memory-bound）。

这两段的张量形状几乎完全相反（一个长序列、一个单 token），若硬塞进同一个 profile，TensorRT 没法同时为两种极端选到最优 kernel。所以 EdgeLLM 给它们各配一套 profile。

### 2.3 与 u4-l1 的衔接

u4-l1 讲了八阶段中的第 6 阶段是「优化 profile 设置」，由 `setupLLMOptimizationProfiles` 入口承接。本讲就是把这个第 6 阶段彻底拆开。同时本讲也覆盖第 4 阶段「模型类型检测」如何选 ONNX 文件——因为它和 profile 分发共用同一组判据（`spec_decode_type` / `engine_role` / `maxLoraRank`）。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `cpp/builder/llmBuilder.cpp` | LLM builder 主体。`setupLLMOptimizationProfiles` 是 profile 分发总入口；下面挂着 `setupCommonProfiles` / `setupVanillaProfiles` / `setupSpecDecodeProfiles` / `setupDFlashDraftProfiles` / `setupGemma4MTPDraftProfiles` / `setupLoraProfiles` 等分支。还包含 ONNX 文件选择与 engine/config 命名逻辑。 |
| `cpp/builder/llmBuilder.h` | `LLMBuilderConfig` 结构体定义（`maxBatchSize` / `maxInputLen` / `maxKVCacheCapacity` / `specBase` / `specDraft` / `maxVerifyTreeSize` / `maxDraftTreeSize` / `maxLoraRank` 等所有 profile 依赖的旋钮）以及各 `setup*Profiles` 方法声明。 |
| `cpp/builder/builderUtils.cpp` | profile 相关公共工具：`createDims`（构造 Dims）、`checkOptimizationProfileDims`（min<=opt<=max 校验）、`setOptimizationProfile`（把 min/opt/max 写进 profile）、`createBuilderAndNetwork`（strongly-typed 网络）、`buildAndSerializeEngine`（编译）。 |
| `cpp/builder/visualBuilder.cpp` | 视觉编码器 builder。与 LLM builder **完全独立**，只配**单 profile**，按 `model_type` 在 5 个 `setup*ViTProfile` 间分发。 |

---

## 4. 核心概念与源码讲解

### 4.1 双阶段优化 profile（context / generation）

#### 4.1.1 概念说明

「双阶段」指的是 LLM builder 一次构建会创建**两个** `IOptimizationProfile`：

- **contextProfile**（也叫 prefill profile）：为「处理整段 prompt」这一形状区间优化。
- **generationProfile**（也叫 decode profile）：为「每步 1 个 token」这一形状区间优化。

构建完成后，两个 profile 都挂到同一个 engine 上（按添加顺序获得索引 0 和 1）。运行时（u5-l3 的 `engineExecutor`）在 prefill 步用 profile 0，进入 decode 后切换到 profile 1，从而让两种极端形状各自命中最优 kernel。

这是 EdgeLLM 区别于「单 profile 笨办法」的核心性能设计：**不牺牲灵活性，也不牺牲针对性**。

#### 4.1.2 核心流程

`setupLLMOptimizationProfiles` 一上来就建好这两个 profile 对象，随后所有 `setup*` 函数都**成对地**往两个 profile 里写同一批 binding 的 min/opt/max，最后统一 `addOptimizationProfile`：

```text
setupLLMOptimizationProfiles(builder, config, network):
    contextProfile    = builder.createOptimizationProfile()   # 索引 0
    generationProfile = builder.createOptimizationProfile()   # 索引 1

    # 1) 两条「提前返回」的特例分支（自己负责填满两个 profile）
    if 是 dflash draft  : setupDFlashDraftProfiles(ctx, gen);  add 两个; return
    if 是 gemma4_mtp draft: setupGemma4MTPDraftProfiles(...); add 两个; return

    # 2) 通用底盘（所有普通/投机 base 都要走）
    setupCommonProfiles(ctx, gen)        # context_lengths / kvcache_start_index / KV / recurrent / conv
    setupRopeProfiles(ctx, gen, network) # rope_cos_sin（单 RoPE 或 sliding/full 双 RoPE）

    # 3) 按角色二选一
    if specBase or specDraft: setupSpecDecodeProfiles(ctx, gen)
    else                     : setupVanillaProfiles(ctx, gen)

    # 4) 一串「可选叠加」分支（按需）
    if mtp/dflash base 且是混合模型: 加 intermediate recurrent/conv + spec-verify
    setupPleProfiles(...)              # Gemma4 PLE（若图里有 ple_token_embeds_*）
    setupDeepstackProfiles(...)        # Qwen3VL deepstack
    setupLmHeadWeightProfiles(...)     # Qwen3-Omni CodePredictor
    if maxLoraRank > 0: setupLoraProfiles(...)

    config.addOptimizationProfile(contextProfile)     # 索引 0
    config.addOptimizationProfile(generationProfile)  # 索引 1
```

一个 profile 里**每个动态输入 binding**都要单独 `setDimensions` 写 min/opt/max；只要有一个动态输入漏配，TensorRT 在运行时绑定该输入就会报错。这也是为什么 builder 里有那么多 `setup*Profiles`——本质是在枚举并照顾「这种模型类型的图里到底有哪些动态输入」。

#### 4.1.3 源码精读

先看两个 profile 的创建与挂载。这是「双阶段」最直接的证据：

[cpp/builder/llmBuilder.cpp:489-495](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L489-L495) —— 入口函数一开头就建两个 profile 对象。

[cpp/builder/llmBuilder.cpp:572-573](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L572-L573) —— 两个 profile 都 `addOptimizationProfile` 挂上 engine，顺序决定索引（context=0，generation=1）。

再看「双阶段差异」最典型的 binding——`inputs_embeds`（输入嵌入，形状 `[batch, seq_len, hidden_size]`）：

[cpp/builder/llmBuilder.cpp:651-656](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L651-L656) —— 在 `setupVanillaProfiles` 里：

- **context profile** 的 `inputs_embeds`：opt = `[maxBatchSize, maxInputLen/2, H]`，max = `[maxBatchSize, maxInputLen, H]`。序列维允许到**整个 `maxInputLen`**（一整段 prompt）。
- **generation profile** 的 `inputs_embeds`：opt = max = `[maxBatchSize, 1, H]`。序列维被钉死成 **1**（每步一个新 token）。

这就是 prefill「大序列」与 decode「单 token」在形状上的根本对立。注意两个 profile 的 **batch 维都用同一个 `maxBatchSize`**——在 EdgeLLM 的实现里，batch 上界两阶段是共享的，真正的区分点在**序列长度维**。

最能体现「双阶段语义」的一个细节是 `kvcache_start_index`：

[cpp/builder/llmBuilder.cpp:591-594](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L591-L594) —— context profile 的 min 形状是 `0`（注释说明：用 0 表示「本批所有序列的 KV 缓存为空」，用来区分「正常 prefill」与「分块 prefill/chunked prefill」）；而 generation profile 的 min 是 `1`（decode 时 KV 缓存里已经有内容，start index ≥ 1）。同一个 binding 在两个 profile 里语义不同。

profile 的公共写入工具是 `setOptimizationProfile`，它会先校验 `min <= opt <= max` 再写三个选择器：

[cpp/builder/builderUtils.cpp:85-96](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/builderUtils.cpp#L85-L96) —— 这就是「profile = min/opt/max 三元组」在代码里的落点。

#### 4.1.4 代码实践

**实践目标**：用肉眼对比 `inputs_embeds` 在两个 profile 里的形状区间，亲眼确认 prefill/decode 的序列维差异，并理解 batch 维为何共享。

**操作步骤**：
1. 打开 [cpp/builder/llmBuilder.cpp:645-675](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L645-L675)（`setupVanillaProfiles`）。
2. 找到 `kInputsEmbeds`（即 `"inputs_embeds"`）的两次 `setOptimizationProfile` 调用，分别针对 context 与 generation。
3. 在一张纸上抄下两行的 min/opt/max，把第二个维度（seq_len）圈出来对比。

**需要观察的现象 / 预期结果**：
- context：seq_len 维 ∈ \([1,\ \text{maxInputLen}/2,\ \text{maxInputLen}]\)；
- generation：seq_len 维 ∈ \([1,\ 1,\ 1]\)；
- 两者 batch 维都是 \([1,\ \text{maxBatchSize},\ \text{maxBatchSize}]\)，**完全相同**。

**结论**：本实现里，prefill 与 decode 的 profile 差异体现在**序列长度维**（整段 prompt vs 单 token），而**不是** batch 维。这是对「prefill 小 batch、decode 大 batch」这条经验法则的一个实现层面的修正——EdgeLLM 把 batch 上界统一交给 `--maxBatchSize`，用序列维来切分两个阶段。（若想体验「batch 也分阶段」的设计，需要改 builder；当前代码不做这件事。）

> 待本地验证：若你有 GPU，可在 `--debug` 下构建，从日志里 `printOptimizationProfile` 的输出（`context_profile` 与 `generation_profile` 两段）直接读出 `inputs_embeds` 的 MIN/OPT/MAX，对照上面的预期。

#### 4.1.5 小练习与答案

**练习 1**：如果把 generation profile 的 `inputs_embeds` 的 max 也设成 `[maxBatchSize, maxInputLen, H]`（和 context 一样），会出什么问题？
**答案**：TensorRT 会围绕这个更大的 max 重新选 kernel 并预留显存，decode 阶段每步其实只算 1 个 token，却用了为长序列选的、对单 token 低效的 kernel，且多占显存。这正是要分两个 profile 的原因。

**练习 2**：`kvcache_start_index` 在 context profile 的 min 为 0、在 generation profile 的 min 为 1，为什么 decode 时不能是 0？
**答案**：decode 阶段 KV 缓存里至少已存了 prompt（prefill 产物），新 token 要追加在已有缓存之后，起始位置必然 ≥ 1；0 在 context 里被专门用来表示「缓存为空、这是从零开始的 prefill」。

---

### 4.2 模型类型分发与 ONNX 文件选择

#### 4.2.1 概念说明

「模型类型分发」有两层含义，本讲都覆盖：

1. **profile 分发**（第 6 阶段内）：`setupLLMOptimizationProfiles` 根据模型角色（vanilla / spec base / spec draft）和具体 spec 算法（eagle3 / mtp / dflash / gemma4_mtp），在多个 `setup*Profiles` 函数间路由。
2. **ONNX 文件选择**（第 4 阶段）：构建前决定解析哪个 `.onnx` 文件——`model.onnx` 还是 `lora_model.onnx`。

两层共用同一组判据，这些判据来自两处：
- **命令行 → `LLMBuilderConfig`**：`specBase` / `specDraft` / `maxLoraRank` 等（由 `examples/llm/llm_build.cpp` 解析）。
- **检查点 `config.json`**：`spec_decode_type`（none/mtp/eagle3/dflash/gemma4_mtp）与 `engine_role`（llm/base/draft），由导出端写入。

> **重要纠正**：设计文档 `engine-builder.md` 里提到一个叫 `setupVLMProfiles` 的函数，但**当前代码里并不存在它**。VLM 的「语言侧」并不走专门的 profile 函数——它和普通 LLM 一样走 `setupVanillaProfiles`（区别仅在于：当 config 里 `use_vision_bidirectional_attention=true` 时，vanilla 分支会额外配一个 `vision_block_ids` 输入）。VLM 的「视觉侧」则是一个独立的 builder（`VisualBuilder`，见 4.3）。学到这里别被文档的措辞误导。

#### 4.2.2 核心流程

**ONNX 文件选择**（`LLMBuilder::build` 第 4 阶段）非常直接：

```text
if maxLoraRank > 0:  解析 lora_model.onnx   # 图里已插好 LoRA 输入 hook
else               :  解析 model.onnx
```

**profile 分发**是一棵带「提前返回」的决策树（见 4.1.2 的伪代码）。判据优先级如下：

1. `isSpecDecodeDraft(config, "dflash")` → 走 `setupDFlashDraftProfiles`，**提前 return**（它自成一套，不走通用底盘）。
2. `isSpecDecodeDraft(config, "gemma4_mtp")` → 走 `setupGemma4MTPDraftProfiles`，**提前 return**。
3. 其余（vanilla LLM、eagle3/mtp base、eagle3 draft、mtp draft、dflash base、gemma4_mtp base）→ 先跑通用底盘 `setupCommonProfiles + setupRopeProfiles`，再按 `specBase||specDraft` 在 `setupSpecDecodeProfiles` 与 `setupVanillaProfiles` 间二选一，最后按需叠加 PLE/Deepstack/lm_head_weight/LoRA 等分支。

`parseConfig` 会做一致性校验：`engine_role="llm"` 必须配 `spec_decode_type="none"`；base/draft 必须配非 none 的 spec 类型；并且命令行标志（`--specBase`/`--specDraft`/都不加）必须与 config 里的 role 三者一致，否则直接构建失败。

#### 4.2.3 源码精读

ONNX 文件选择（第 4 阶段，lora vs 普通）：

[cpp/builder/llmBuilder.cpp:196-206](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L196-L206) —— `maxLoraRank > 0` 选 `lora_model.onnx`，否则选 `model.onnx`。这要求用户在导出阶段先用 `tensorrt-edgellm-insert-lora` 把 LoRA 输入 hook 插进图（见 u9-l1）。

profile 分发的两条「提前返回」特例：

[cpp/builder/llmBuilder.cpp:497-525](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L497-L525) —— dflash draft 与 gemma4_mtp draft 因为图的 I/O 与众不同（DFlash 吃 target hidden concat 与 delta lengths；Gemma4 assistant 吃 base embedding 与 target KV、自己不持有 KV/tree-mask），所以各自独占一套 profile 函数并提前返回，不复用通用底盘。

通用底盘之后的「vanilla vs spec」二选一：

[cpp/builder/llmBuilder.cpp:527-539](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L527-L539) —— `setupCommonProfiles` + `setupRopeProfiles` 之后，`if (specBase || specDraft) setupSpecDecodeProfiles else setupVanillaProfiles`。

config/role 一致性校验：

[cpp/builder/llmBuilder.cpp:336-365](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L336-L365) —— 校验 `spec_decode_type` 合法性、`engine_role` 合法性、role 与 spec type 的配对关系，以及命令行标志与 config role 的匹配。任何不一致都会在构建早期失败并给出清晰提示。

`setupVanillaProfiles` 与 `setupSpecDecodeProfiles` 的对比（理解差异的钥匙）：

[cpp/builder/llmBuilder.cpp:645-675](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L645-L675) —— vanilla 只配 `inputs_embeds`、可选 `vision_block_ids`、`last_token_ids`。

[cpp/builder/llmBuilder.cpp:677-740](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L677-L740) —— spec decode 额外引入 `draft_model_hidden_states` / `base_model_hidden_states`（draft 角色）、按 `maxVerifyTreeSize`/`maxDraftTreeSize` 决定的 token 树上限、以及对齐到 32 的 packed `attention_mask`。`maxTokens` 取值：draft 用 `maxDraftTreeSize`，base 用 `maxVerifyTreeSize`。

LoRA 分支（动态 rank）：

[cpp/builder/llmBuilder.cpp:1041-1115](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L1041-L1115) —— 扫描图里所有 `lora_A_*` / `lora_B_*` 输入，给 rank 维配 `[0, maxLoraRank/2, maxLoraRank]`（min=0 表示「该 adapter 可缺席」，支持运行时按 `loraWeightsName` 动态开关，见 u9-l1）。

#### 4.2.4 代码实践

**实践目标**：追踪「EAGLE 构建为何 base 与 draft 共用同一 `engineDir`」这条设计，把命名约定串起来。

**操作步骤**：
1. 看 engine 文件命名：[cpp/builder/llmBuilder.cpp:254-266](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L254-L266) —— `specDraft` → `spec_draft.engine`，`specBase` → `spec_base.engine`，否则 `llm.engine`。
2. 看 config 文件命名：[cpp/builder/llmBuilder.cpp:1328-1340](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L1328-L1340) —— draft → `draft_config.json`，base → `base_config.json`，否则 `config.json`。
3. 看外部权重防冲突：[cpp/builder/llmBuilder.cpp:1308-1323](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L1308-L1323) —— `externalWeightDstName` 只给 draft 文件加 `draft_` 前缀（base 保留原名），注释明确说「spec base 和 draft 共享一个 engineDir，否则外部权重文件会互相覆盖」。

**需要观察的现象 / 预期结果**：
- 两次构建（`--specBase` 一次、`--specDraft` 一次）写进**同一个** `--engineDir`，但产物文件名互不冲突：`spec_base.engine`+`base_config.json` vs `spec_draft.engine`+`draft_config.json`。
- 共享的外部权重（如 `external_int4_ffn_weights.safetensors`）：base 写原名，draft 写 `draft_external_int4_ffn_weights.safetensors`。
- tokenizer/embedding/d2t 等 sidecar 由各 `copy*` 函数按角色决定是否拷贝（draft 不拷 tokenizer 与 embedding，复用 base 的）。

**结论**：共用 `engineDir` 的根本原因是——运行时（u5-l1 的 `LLMInferenceRuntime`）构造投机解码 runtime 时要**同时加载** base 与 draft 两个 engine，它们必须在同一目录下才能被一起发现；命名前缀/后缀是为了在「同目录」前提下避免文件互相覆盖。

#### 4.2.5 小练习与答案

**练习 1**：一个带 LoRA 的投机解码 base 模型，构建时第 4 阶段会解析哪个 ONNX？会走哪个 profile 主分支？
**答案**：`maxLoraRank>0` → 解析 `lora_model.onnx`；同时 `specBase=true` → profile 主分支走 `setupSpecDecodeProfiles`，并在末尾额外叠加 `setupLoraProfiles`。即 LoRA 与投机解码是「正交叠加」关系。

**练习 2**：为什么 dflash draft 和 gemma4_mtp draft 要「提前 return」，而不像 eagle3 draft 那样走通用底盘？
**答案**：它们的图 I/O 与通用假设不兼容——DFlash draft 需要 `dflash_target_hidden_concat` 与 `dflash_delta_lengths` 这类独有输入并自己管 KV；Gemma4 assistant 不拥有自己的 KV 缓存和 tree-mask，而是读 base 的 hidden/embedding。强行套通用底盘会配出一堆图里不存在的 binding，所以各自独立。

**练习 3**：若用户运行 `llm_build --specBase` 但 config.json 里 `engine_role="llm"`，会发生什么？
**答案**：[cpp/builder/llmBuilder.cpp:357-365](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L357-L365) 的校验会命中并 `LOG_ERROR` 报「Build mode does not match config」，构建直接返回 false 失败。这是为了让角色不一致在早期暴露，而不是产出一个运行时才崩溃的 engine。

---

### 4.3 visualBuilder：独立的视觉引擎与单 profile 分发

#### 4.3.1 概念说明

视觉编码器（Vision Transformer / ViT）的「形状特征」和 LLM 截然不同：

- 它**没有自回归生成**——一张图一次性前向，不存在 prefill/decode 两段；
- 它的动态维度是「**图像 token 数 / patch 数**」，随图片分辨率与数量变化；
- 它的输入是像素或 patch，不是 token id。

因此视觉侧**不需要双阶段 profile**，只配**一个** `visualProfile`。它由独立的 `VisualBuilder` 类负责，和 `LLMBuilder` 平级（都依赖 `builderUtils` 的公共工具，但各自有自己的 `*BuilderConfig` 与 `setup*` 分发）。

分发依据是 `config.json` 里的 `model_type`（视觉配置），`parseConfig` 用 `multimodal::stringToModelType` 转成枚举，再在 `setupVisualOptimizationProfile` 里 `switch` 分发到 5 个架构专属的 profile 函数。

#### 4.3.2 核心流程

```text
VisualBuilder::build():           # 与 LLMBuilder 同构的精简八阶段
    loadEdgellmPluginLib()         # 1 插件加载
    parseConfig()                  # 2 读 config.json，解析 model_type → mModelType
    createBuilderAndNetwork()      # 3 strongly-typed 网络
    parseOnnxModel(model.onnx)     # 4/5 视觉侧固定用 model.onnx（没有 lora_model.onnx）
    createBuilderConfig()
    setupVisualOptimizationProfile(...)  # 6 单 profile，switch(mModelType) 分发
    buildAndSerializeEngine() → visual.engine   # 7
    copyConfig() + preprocessor_config.json     # 8
```

第 6 阶段的 switch：

| model_type（视觉） | profile 函数 | 关键约束 |
|---|---|---|
| QWEN2_VL / QWEN2_5_VL / QWEN3_VL / QWEN3_5 / QWEN3_OMNI 视觉 | `setupQwenViTProfile` | HW = imageTokens × 4（spatial_merge_size²） |
| INTERNVL / PHI4MM | `setupInternPhi4ViTProfile` | **image tokens 必须是 256 的倍数** |
| NEMOTRON_OMNI（RADIO） | `setupNemotronOmniViTProfile` | 按 patch_size × downsample_ratio 算每块 token 数 |
| GEMMA4_VISION | `setupGemma4ViTProfile` | pooling_kernel_size² 个 patch → 1 个 soft token |
| GEMMA4_UNIFIED_VISION | `setupGemma4UnifiedVisionProfile` | 输入是已打包 patch `[N, 48·48·3]` + `pixel_position_ids [N,2]` |

视觉 builder 的 `VisualBuilderConfig` 只有三个核心旋钮：`minImageTokens` / `maxImageTokens` / `maxImageTokensPerImage`（外加 `useTrtNativeVitAttn`）。和 LLM 的 `maxBatchSize`/`maxInputLen`/`maxKVCacheCapacity` 是两套互不相干的词汇。

#### 4.3.3 源码精读

视觉侧固定解析 `model.onnx`（不像 LLM 那样有 lora 二选一）：

[cpp/builder/visualBuilder.cpp:60-61](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/visualBuilder.cpp#L60-L61) —— 直接 `(mOnnxDir / "model.onnx")`。

只配**一个** profile 并挂载（对比 LLM 的两个）：

[cpp/builder/visualBuilder.cpp:84](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/visualBuilder.cpp#L84) 与 [cpp/builder/visualBuilder.cpp:289](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/visualBuilder.cpp#L289) —— `setupVisualOptimizationProfile` 内部只 `createOptimizationProfile()` 一次，最后只 `addOptimizationProfile` 一次。

按 `model_type` 的 switch 分发：

[cpp/builder/visualBuilder.cpp:257-279](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/visualBuilder.cpp#L257-L279) —— 这就是「视觉模型类型分发」的落点；不认识的类型直接 `LOG_ERROR` 返回 false。

InternVL/Phi4 的「256 倍数」约束（对应实践任务里的一个常考点）：

[cpp/builder/visualBuilder.cpp:413-438](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/visualBuilder.cpp#L413-L438) —— `kBlockLength = 256`（16×16 patch 网格），`minImageTokens` 与 `maxImageTokens` 必须能被 256 整除，否则构建失败。原因：InternVL/Phi4 的视觉编码器按「块」处理图像，每块固定 256 个 token，token 数必须是块数的整数倍。

Qwen-VL 的「HW = imageTokens × 4」：

[cpp/builder/visualBuilder.cpp:298-301](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/visualBuilder.cpp#L298-L301) —— 注释说明 HW 恒为 imageTokens 的 4 倍，因为等于 `spatial_merge_size ** 2`（2×2 合并）。

视觉配置旋钮定义：

[cpp/builder/visualBuilder.h:40-42](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/visualBuilder.h#L40-L42) —— `minImageTokens{4}` / `maxImageTokens{1024}` / `maxImageTokensPerImage{512}` 的默认值。

#### 4.3.4 代码实践

**实践目标**：组装一条 VLM 的完整构建命令（LLM + 视觉），并验证 InternVL/Phi4 的 256 倍数约束。

**操作步骤**：
1. 读示例用法：[docs/source/developer_guide/software-design/engine-builder.md](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/developer_guide/software-design/engine-builder.md)（「Multimodal VLM Build」与「Speculative Decoding Build」小节给出了视觉构建参数）。
2. 为一个 InternVL 视觉编码器组装命令（示例命令，路径需替换为你自己的导出产物）：

```bash
# 示例命令（路径请替换为实际 onnx/引擎目录）
./build/examples/multimodal/visual_build \
  --onnxDir=onnx_models/internvl/visual_enc_onnx \
  --engineDir=visual_engines/internvl \
  --minImageTokens=256 \
  --maxImageTokens=1024 \
  --maxImageTokensPerImage=1024
```

3. 故意把 `--minImageTokens=300`（不是 256 的倍数）再跑一次。

**需要观察的现象 / 预期结果**：
- 正确命令：产出 `visual.engine` 与带 `builder_config` 的 `config.json`，日志打印 `visual_profile`（注意：**只有一个** profile）。
- 错误命令：在 [cpp/builder/visualBuilder.cpp:421-426](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/visualBuilder.cpp#L421-L426) 命中校验，`LOG_ERROR: minImageTokens and maxImageTokens must be divisible by 256`，构建失败。
- 256 的来源：InternVL/Phi4 每「图像块」固定 16×16=256 个 token，token 总数必须是块数整数倍。

> 待本地验证：上述命令需要已导出的视觉 ONNX 与 GPU 环境；若无，至少把命令与参数对照源码解释一遍即可（源码阅读型实践）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `VisualBuilder` 只配一个 profile，而 `LLMBuilder` 要配两个？
**答案**：视觉编码器是一次性前向（无自回归），输入形状只有「不同分辨率/不同图片数」一种变化维度，单 profile 足以覆盖；LLLM 有 prefill（长序列）与 decode（单 token）两种几乎相反的形状，单 profile 无法同时最优，故需双 profile。

**练习 2**：Qwen-VL 里「HW = imageTokens × 4」的 4 从哪来？
**答案**：来自 `spatial_merge_size ** 2 = 2² = 4`。Qwen-VL 把 2×2 个相邻 patch 合并成 1 个送入 LLM 的 token，所以原始 patch 网格规模（HW）是最终 image token 数的 4 倍。

**练习 3**：视觉侧如果带 LoRA 会怎样？
**答案**：视觉侧固定解析 `model.onnx`（[cpp/builder/visualBuilder.cpp:60-61](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/visualBuilder.cpp#L60-L61)），没有 `lora_model.onnx` 的二选一逻辑，也没有 `setupLoraProfiles`。当前 `VisualBuilder` 不支持运行时 LoRA——LoRA 只在 LLM builder 一侧。

---

## 5. 综合实践

**任务**：给定一个「EAGLE3 投机解码 + 视觉」的 VLM，请把构建它所需的命令、产物文件清单、以及涉及的 profile 分发分支完整写出来，并回答两个核心问题。

**背景**：你已用 `tensorrt-edgellm-export` 导出了三个 ONNX 目录：
- `onnx/eagle3_base/`（base 语言模型，`config.json` 里 `spec_decode_type=eagle3, engine_role=base`）
- `onnx/eagle3_draft/`（draft 语言模型，`spec_decode_type=eagle3, engine_role=draft`）
- `onnx/visual/`（Qwen-VL 视觉编码器，`model_type=qwen2_vl` 等）

**要求**：

1. **写出三条构建命令**，注意 base 与 draft 的 `--engineDir` 必须相同（解释为什么）。
2. **列出最终共享 engine 目录里的产物文件**：哪些 engine、哪些 config、tokenizer/embedding 由谁拷贝、draft 的外部权重为何要加 `draft_` 前缀。
3. **指出 profile 分发**：
   - base 走哪条分支？（提示：`setupCommonProfiles` + `setupRopeProfiles` + `setupSpecDecodeProfiles`）
   - draft 走哪条分支？（eagle3 draft **不**提前 return，也走通用底盘 + `setupSpecDecodeProfiles`）
   - 视觉走哪个函数？（`setupQwenViTProfile`，单 profile）
4. **回答两个核心问题**（即本讲规格里的实践任务）：
   - 为什么 prefill profile 倾向「大序列」、decode profile 倾向「单 token」？在本实现里 batch 维是否也分阶段？（答：序列维分阶段——prefill 到 `maxInputLen`、decode 为 1；batch 维在本实现里**不分**，两阶段共享 `maxBatchSize`。）
   - 为什么 EAGLE 的 base 与 draft 要共用同一个 `engineDir`？（答：运行时要同时加载两个 engine 做投机解码，须同目录；靠 `spec_base.engine`/`spec_draft.engine`、`base_config.json`/`draft_config.json` 以及 draft 外部权重的 `draft_` 前缀避免同名覆盖。）

**参考命令**（路径替换为实际值）：

```bash
# 1) EAGLE3 base 语言引擎（profile 索引 0=prefill, 1=decode）
./build/examples/llm/llm_build \
  --onnxDir=onnx/eagle3_base --engineDir=engines/eagle3_vlm \
  --maxBatchSize=1 --maxInputLen=1024 --maxKVCacheCapacity=4096 --specBase

# 2) EAGLE3 draft 语言引擎（与 base 同一个 engineDir！）
./build/examples/llm/llm_build \
  --onnxDir=onnx/eagle3_draft --engineDir=engines/eagle3_vlm \
  --maxBatchSize=1 --maxInputLen=1024 --maxKVCacheCapacity=4096 --specDraft

# 3) 视觉引擎（独立 builder，单 profile）
./build/examples/multimodal/visual_build \
  --onnxDir=onnx/visual --engineDir=visual_engines/eagle3_vlm \
  --minImageTokens=128 --maxImageTokens=512 --maxImageTokensPerImage=512
```

**预期产物**（`engines/eagle3_vlm/`）：
- `spec_base.engine` + `base_config.json`
- `spec_draft.engine` + `draft_config.json`
- tokenizer 文件（仅 base 拷贝，draft 跳过）、`embedding.safetensors`（仅 base/draft 中非 draft 的拷贝）
- `d2t.safetensors`（仅 eagle3 draft 拷贝，draft→target 词表映射）
- 若有外部权重：base 原名、draft 带 `draft_` 前缀

> 待本地验证：以上命令与产物清单需在真实导出产物 + GPU 环境上验证；本实践主要目的是把「命令—分发分支—产物命名」三者的对应关系在源码层面打通。

---

## 6. 本讲小结

- **双阶段 profile**：LLM builder 一律建两个 profile——context（prefill）与 generation（decode），按添加顺序获得索引 0/1，运行时切换。在本实现里两者**共享 `maxBatchSize`**，真正的差异在**序列长度维**（prefill 到 `maxInputLen`、decode 为 1）。
- **profile 分发是一棵决策树**：dflash draft / gemma4_mtp draft 提前 return 自成一套；其余走「通用底盘（common + rope）+ vanilla/spec 二选一」，再按需叠加 PLE/Deepstack/lm_head_weight/LoRA。
- **ONNX 文件选择**（第 4 阶段）= `maxLoraRank>0 ? lora_model.onnx : model.onnx`；判据与 profile 分发共用 `spec_decode_type`/`engine_role`/`maxLoraRank`，且 `parseConfig` 会强校验三者一致。
- **视觉是独立 builder**：`VisualBuilder` 只配**单 profile**（无 prefill/decode 之分），按 `model_type` 在 5 个 `setup*ViTProfile` 间 switch 分发；InternVL/Phi4 要求 image tokens 是 256 的倍数。
- **重要纠错**：代码里**没有** `setupVLMProfiles`。VLM 的语言侧复用 `setupVanillaProfiles`（仅多一个可选 `vision_block_ids`），视觉侧才由 `VisualBuilder` 单独负责。
- **EAGLE base/draft 共用 engineDir**：运行时要同目录同时加载两 engine；靠 `spec_base.engine`/`spec_draft.engine`、`base_config.json`/`draft_config.json` 与 draft 外部权重的 `draft_` 前缀防冲突。

---

## 7. 下一步学习建议

- **u4-l3（使用构建器 CLI）**：把本讲的 `--maxBatchSize`/`--maxInputLen`/`--maxKVCacheCapacity`/`--specBase`/`--specDraft`/`--minImageTokens` 等参数与真实命令行用法对齐，做端到端构建。
- **u5-l1 / u5-l3（运行时与 engineExecutor）**：去看运行时如何用 profile 索引 0/1 在 prefill/decode 间切换、`tensorMap` 如何按 binding 名把这里的 profile 形状绑到运行时张量——那是「双阶段 profile」的另一半故事。
- **u5-l5（KV 缓存管理）**：理解这里 `[batch, 2, numKVHeads, 0..maxKVCacheCapacity, headDim]` 形状在运行时如何被线性 KV cache 消费。
- **u7-l1（投机解码策略）**：对照 base 的 `setupSpecDecodeProfiles`（`maxVerifyTreeSize`/packed mask）与 draft 的 hidden-state 输入，去读 `eagleDecoder`/`mtpDecoder` 等解码器，理解「树如何提议、base 如何验证」。
- **u9-l1（LoRA 支持）**：把这里的 `lora_model.onnx` 选择与 `setupLoraProfiles` 的动态 rank `[0, maxLoraRank/2, maxLoraRank]` 与导出侧的 `insert-lora`、运行时的 `loraManager` 串成全链路。
