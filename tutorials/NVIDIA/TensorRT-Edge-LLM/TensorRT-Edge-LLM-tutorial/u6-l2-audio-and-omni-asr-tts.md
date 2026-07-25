# 音频与 Omni（ASR/TTS）流水线

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清楚一条音频「从原始 PCM 到最终波形」在 C++ 运行时里经过了哪几个组件、各自产出什么中间产物。
2. 理解 `Qwen3OmniAudioRunner` 如何把 PCM 变成 mel 频谱图、再编码成 audio embedding，并以占位 token 的方式塞进文本序列。
3. 掌握 Qwen3-Omni 的 **thinker（理解）** 与 **talker（生成语音码）** 分工：`Qwen3OmniTTSRuntime` 如何用 Talker + CodePredictor 两台 LLM 引擎自回归地生成 RVQ 码。
4. 理解 `Code2WavRunner` 如何把 RVQ 码翻译成可播放的 PCM 波形，以及长音频为什么要分块（chunked）解码。
5. 能画出 Qwen3-Omni「文本+音频输入 → 文本+音频输出」的完整组件协作图。

## 2. 前置知识

本讲默认你已经读过 [u6-l1 多模态运行器与视觉编码器](u6-l1-multimodal-runner-vision-encoders.md)，了解了 `MultimodalRunner` 抽象基类的 `preprocess → infer → getOutputEmbedding` 三段式接口，以及「图像 token id ≥ vocabSize、在 prefill 时被替换成 ViT 输出行」的图文融合约定。音频沿用了几乎相同的套路，只是把「视觉编码器」换成了「音频编码器」。

在进入源码前，先建立三个直觉概念：

**（1）mel 频谱图（mel-spectrogram）。** 人耳对低频敏感、高频迟钝。mel 刻度是一种模仿这种感知的非线性频率轴。把一段 PCM 波形按短时窗（这里用 Whisper 的 400 点 FFT、周期 Hann 窗）切片，对每片做傅里叶变换取幅度，再沿频率轴用一组三角滤波器（mel filter bank）压缩成 128 个 mel 频带、取对数，就得到一张「时间 × mel 频带」的二维图。这张图就是音频编码器的输入。把它想成「音频版的图像」即可。

**（2）RVQ（Residual Vector Quantization，残差向量量化）。** 这是对「一帧音频」的压缩表示。一帧被编码成多层码本里的多个整数 code：第一层 code 粗略逼近这一帧，后续每一层 code 逼近的是上一层的「残差」。层数越多、恢复质量越高。在 Qwen3-Omni 里，每帧有

\[
\text{numCodesPerFrame} = \text{num\_code\_groups}
\]

个 code（Omni=16、TTS=32）。其中第 0 个 code 由 **Talker** 直接生成，其余 15 个（Omni）由 **CodePredictor** 的 15 个独立 lm_head 逐层生成。这正是「thinker/talker/code2wav」三者分工的来源。

**（3）帧率与采样率。** Qwen3-Omni 的语音码帧率是 12.5 Hz（每秒 12.5 帧），输出 PCM 采样率 24000 Hz。因此每一帧对应

\[
\text{samplesPerFrame} = \frac{24000}{12.5} = 1920
\]

个 PCM 样本（见源码常量 `kAudioSamplesPerFrame`）。这意味着 1 秒音频 ≈ 12.5 帧 ≈ 24000 个样本。

> 术语速查：PCM（原始波形采样）、mel/fbank（mel 频谱）、codebook（码本，每层一张）、RVQ code（一帧的多层码本下标）、声码器/vocoder（code→波形）、thinker（理解音频/文本的大 LLM）、talker（生成语音码的小 LLM）、code_predictor（补齐 RVQ 剩余层的小 LLM）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `cpp/multimodal/audioRunner.cpp` / `.h` | `Qwen3OmniAudioRunner`：音频**输入**侧。PCM→mel→audio encoder→audio embedding，是 `MultimodalRunner` 的子类，被 thinker 调用。 |
| `cpp/runtime/qwen3OmniTTSRuntime.cpp` / `.h` | `Qwen3OmniTTSRuntime`：语音**输出**侧的总指挥。管理 Talker + CodePredictor 两台 LLM 引擎，把 thinker 的文本/隐状态变成 RVQ 码。**不是** `MultimodalRunner` 子类，是独立运行时。 |
| `cpp/multimodal/code2WavRunner.cpp` / `.h` | `Code2WavRunner`：声码器。RVQ 码→PCM 波形→`.wav`。 |
| `cpp/multimodal/multimodalRunner.h` | `MultimodalRunner` 基类与 `create()` 工厂，audioRunner 经它被实例化。 |
| `examples/omni/qwen3_tts_inference.cpp` | TTS 端到端示例：把 TTSRuntime + Code2Wav 串起来跑出 `.wav`。 |
| `examples/llm/llm_inference.cpp` | Omni 端到端示例：thinker + TTSRuntime 的流式/非流式编排入口。 |

一句话定位三者的数据流方向：

```
[音频输入] PCM --audioRunner--> audio embedding --(塞进序列)--> thinker
[文本/音频输出] thinker --qwen3OmniTTSRuntime--> RVQ 码 --code2WavRunner--> 波形 .wav
```

注意：`audioRunner` 在**输入侧**（喂给 thinker），`qwen3OmniTTSRuntime` + `code2WavRunner` 在**输出侧**（吃 thinker 的产物）。这是本讲最容易搞混的一点，务必记住方向。

## 4. 核心概念与源码讲解

### 4.1 音频编码器 audioRunner：PCM → mel → audio embedding

#### 4.1.1 概念说明

`Qwen3OmniAudioRunner` 是音频输入的唯一入口，职责是：拿到调用方提供的原始 PCM 波形，自己提取 mel 频谱图，跑音频编码器引擎，产出一行行的 audio embedding，并把这些 embedding 的「数量」告诉文本侧——文本侧用同样数量的占位 token `<|audio_pad|>` 占位，在 prefill 时再替换成真正的 audio embedding 行。这与 [u6-l1](u6-l1-multimodal-runner-vision-encoders.md) 里图像「占位 token 在 prefill 时被替换成 ViT 输出」是同一个套路。

它继承自 `MultimodalRunner`，复用 `preprocess/infer/getOutputEmbedding` 三段式接口，因此 thinker 用同一个 `create()` 工厂就能透明地挂上音频编码器。一个关键设计是：**mel 提取由 runner 自己负责**，调用方只需给原始 PCM（见头文件注释「Runner owns mel extraction; callers hand off raw PCM」）。

#### 4.1.2 核心流程

`preprocess` 分三步：

1. **音频预处理**：对每条 `audioBuffers`，把 PCM 变成 `[1, nMel, T]` 的 mel 张量，再分窗（chunk）成音频编码器期望的输入格式，构造块状注意力掩码，设置 `cu_seqlens`/`kv_lengths`，跑音频编码器 `enqueueV3`，得到 `[totalAudioTokens, audioFeatureDim]` 的 audio embedding，并记录 token 数。
2. **文本预处理**：把请求里的文本分词，把单个 `<|audio_pad|>` 占位符原地展开成 `totalAudioTokens` 个占位 token。
3. **MRoPE 初始化**：为音频+文本的（无视觉）模型初始化顺序位置编码缓存（音频用 MRoPE，T/H/W 三维同序）。

mel 提取有两条等价路径，优先走 GPU：

```
PCM (host FP32)
   │
   ├── tryOnlineGpuFbank ──► melSpec [1,128,T] FP16 (GPU)   ← 首选：在线 GPU fbank
   │        (Whisper 规格：FFT=400, 周期Hann, reflect-pad, log10, clamp)
   │
   └── mFeMel.extract ──► hostMel ──► 上传 FP16 GPU          ← 兜底：CPU MelExtractor
```

两者产出**完全相同**的 `[1, nMel, T] FP16` 契约，编码器对走哪条路无感。

#### 4.1.3 源码精读

构造函数加载 `audio_encoder.engine` 并设置 profile 0；mel 提取器的「best-effort」GPU 初始化失败会优雅回退到 CPU，不致命：

- [audioRunner.cpp:L69-L129](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/audioRunner.cpp#L69-L129) — 加载音频编码器引擎、创建上下文、设置 profile、best-effort 初始化在线 GPU fbank（失败则回退 CPU MelExtractor）。

`validateAndFillConfig` 从 `config.json` 读 `audio_config`（melBins=128、audioFeatureDim=2560、nWindow/nWindowInfer）和 `audio_token_id`，并按引擎顶层 `model_type` 选择 Whisper 风格的 mel 提取器：

- [audioRunner.cpp:L131-L204](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/audioRunner.cpp#L131-L204) — 仅当 `model_type` 为 `qwen3_asr_thinker` 或 `qwen3_omni_audio_encoder` 时绑定 Whisper mel 提取器。

`textPreprocess` 把单个 `<|audio_pad|>` 占位符展开成 N 个（N = 该音频的 token 数），`<|audio_start|>`/`<|audio_end|>` 由统一 chat 模板提供、这里不再重复加：

- [audioRunner.cpp:L352-L371](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/audioRunner.cpp#L352-L371) — 占位 token 原地展开。

GPU fbank 的启用有严格门控：`whisperFbankConfigOk` 校验 mel 提取器配置确为 Whisper 规格（FFT=400、周期 Hann、reflect-pad、log10+max-floor+clamp、`[nMel,T]` 布局、丢最后一帧），任何不符就干净回退 CPU：

- [audioRunner.cpp:L408-L430](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/audioRunner.cpp#L408-L430) — Whisper 规格校验，是 GPU fbank 的唯一「期望规格」事实来源。

`preprocessAudio` 是主菜：PCM→mel→分窗→掩码→设 `cu_seqlens`（始终 `{0, totalAudioTokens}`，因为音频编码器固定 batch=1）→设输入形状与地址→`enqueueV3`：

- [audioRunner.cpp:L785-L804](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/audioRunner.cpp#L785-L804) — 执行音频编码器，`audioTokenLengths.push_back(totalAudioTokens)`。
- [audioRunner.cpp:L689-L728](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/audioRunner.cpp#L689-L728) — 设置 `cu_seqlens`/`kv_lengths`（注释明确「audio encoder always runs at batch size of 1」）。

值得注意：`infer()` 是个 **no-op**，因为编码器已经在 `preprocessAudio` 里跑完了：

- [audioRunner.cpp:L812-L816](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/audioRunner.cpp#L812-L816) — `infer` 不做事（推理已在 preprocessAudio 完成）。
- [audioRunner.cpp:L818-L821](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/audioRunner.cpp#L818-L821) — `getOutputEmbedding` 返回 `[totalAudioTokens, audioFeatureDim]`。

#### 4.1.4 代码实践

**实践目标**：理解 PCM 如何变成 audio embedding，并定位「占位 token 展开」与「编码器执行」两处关键代码。

**操作步骤（源码阅读型，无需 GPU）**：

1. 打开 `cpp/multimodal/audioRunner.cpp`，在 `preprocessAudio`（[L603](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/audioRunner.cpp#L603)）里，从 `audio.pcm`（PCM 输入）一路追到 `mAudioContext->enqueueV3(stream)`（编码器执行）和 `mAudioEmbedding`（输出），用箭头画出这条链。
2. 在 `textPreprocess`（[L327](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/audioRunner.cpp#L327)）里找到「把 1 个 `<|audio_pad|>` 展开成 N 个」的循环，确认它不会重复添加 `<|audio_start|>`/`<|audio_end|>`。
3. 阅读 `tryOnlineGpuFbank`（[L555](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/audioRunner.cpp#L555)）的三个门控条件（fbank 就绪、mel 宽度一致、采样率一致），说明任何一个不满足时会发生什么。

**需要观察的现象**：`preprocessAudio` 末尾对每条音频 push 一个 `totalAudioTokens`；这个数字随后被 `textPreprocess` 用来决定展开多少个占位 token。

**预期结果**：你能写出「PCM → (GPU fbank 或 CPU MelExtractor) → melSpec → preprocessAudioForEncoder 分窗 → 音频编码器 → mAudioEmbedding」这条完整链，并解释 `totalAudioTokens` 如何同时驱动「占位 token 数量」与「embedding 行数」两者一致。

**待本地验证**：若你有 GPU 且已构建带 `-DENABLE_CUTE_DSL=gemm` 的版本，可构造一个最小 Omni 请求（文本+一段 wav）跑 `llm_inference`，在 `--debug` 日志里确认看到 `Online GPU fbank ready` 或 `using CPU MelExtractor for PCM→mel`；无 GPU 时跳过，源码阅读结论已足够。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Qwen3OmniAudioRunner::infer()` 是 no-op？这和视觉 runner 有何不同？

**参考答案**：因为音频编码器的 `enqueueV3` 已经在 `preprocessAudio` 里执行了，`getOutputEmbedding` 直接返回已有的 `mAudioEmbedding`。把推理前移到 preprocess 阶段，是为了在「文本侧还不知道占位 token 数量」之前就把 audio embedding 算好并量出 token 数；视觉 runner 的组织方式类似（preprocess 阶段就把 ViT 跑完），这是 `MultimodalRunner` 子类的共性。

**练习 2**：GPU fbank 在什么情况下会回退到 CPU MelExtractor？回退后编码器输入会变吗？

**参考答案**：当任一门控失败时回退：在线 fbank 未就绪（如未编译 CuTe DSL GEMM、当前 SM 无对应 kernel）、mel 配置不是 Whisper 规格、mel 宽度不一致、采样率不匹配、或帧数超出预分配上限。回退后编码器输入**不变**——两条路都产出相同的 `[1, nMel, T] FP16` 契约，编码器对走哪条路无感。

---

### 4.2 qwen3OmniTTSRuntime：thinker 输出 → RVQ 码（Talker + CodePredictor）

#### 4.2.1 概念说明

`Qwen3OmniTTSRuntime` 是语音**输出**侧的总指挥。它**不是** `MultimodalRunner` 的子类（头文件明确写着「Standalone runtime, not dependent on MultimodalRunner hierarchy」），而是一个与 `LLMInferenceRuntime` 同级的独立运行时，内部管理**两台** LLM 引擎：

- **Talker**：一台小型 LLM 解码器，自回归地生成「每帧的第 0 个 code」（codec token），并产出隐状态喂给 CodePredictor。
- **CodePredictor**：另一台小型 LLM，吃 Talker 的隐状态，用多个独立 lm_head 逐层生成同一帧的剩余 RVQ code。

此外它还持有两个 MLP 投影：`text_projection`（thinker 层 0 embedding → talker 输入空间）与可选的 `hidden_projection`（thinker 某层隐状态 → talker 输入空间，用于多模态 token）。架构哲学见头文件注释：

- [qwen3OmniTTSRuntime.h:L80-L97](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/qwen3OmniTTSRuntime.h#L80-L97) — Talker 是 LLM 解码器而非多模态输入编码器；与 `LLMInferenceRuntime` 类似地管理多引擎；code2wav 声码被分离出去以提升模块性。

「thinker（理解）」与「talker（生成语音码）」的分工由此而来：thinker 负责理解文本+音频输入并生成文本/隐状态；talker 负责把这些变成可合成语音的 RVQ 码。

#### 4.2.2 核心流程

RVQ 的逐帧生成是本模块的核心。每帧要产出 `numCodesPerFrame`（Omni=16）个 code，分工如下：

```
一帧 (frame)
 ├─ code_0      ← Talker 直接采样（codec token）
 └─ code_1..15  ← CodePredictor 的 15 个 lm_head 逐层采样
      ├─ CP prefill (输入 2 token: proj(talker_h), proj(code_0)) → lm_head[0] → code_1
      └─ CP decode (step=2..15): 每步换绑 lm_head[step-1] → code_step
```

`num_code_groups` 决定一切：Omni=16（`mNumRvqLayers=15`、`mNumCodesPerFrame=16`），TTS=32（`mNumRvqLayers=31`）。运行时对外暴露三个入口：

1. **`handleAudioGeneration`**（纯文本 TTS，批量）：自己分词、查 embedding、MLP 投影，跑批量 Talker prefill，再进生成循环。
2. **`handleAudioGenerationFromThinker`**（Omni 非流式，批量）：吃 thinker 预算好的层 0 embedding 与某层隐状态，按 `<|im_start|>` 分段解析、投影、组装 prefill，再进同一个生成循环。
3. **`handleStreamingGeneration`**（Omni 流式，bs=1）：thinker 与 talker 在**同一条 CUDA 流**上交错执行——给 thinker 的 decode 循环装一个 per-token 回调，攒够若干 assistant token 就触发 talker prefill，之后每来一个 thinker token 就增量扩展 trailing hidden 并跑一帧 talker 解码。

三者最终都汇聚到 `runTalkerGenerationLoop`（前两者）或 `runSingleTalkerDecodeFrame`（流式），它们的内层都是「取 Talker 末位隐状态 → CodePredictor 补齐本帧 RVQ → 残差连接 → Talker 解码一步 → 采样下一帧的 code_0」。

#### 4.2.3 源码精读

构造函数一次性把 tokenizer、两台引擎、共享上下文显存、各类权重与 embedding 表全部加载好：

- [qwen3OmniTTSRuntime.cpp:L137-L250](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/qwen3OmniTTSRuntime.cpp#L137-L250) — Talker 与 CodePredictor 都用 `kUSER_MANAGED` 上下文显存，二者**共享同一块** `mSharedExecContextMemory`（取两者需求的最大值）。
- [qwen3OmniTTSRuntime.cpp:L201-L215](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/qwen3OmniTTSRuntime.cpp#L201-L215) — 文本 embedding 表的来源区分：Omni 用 thinker 的 `embedding.safetensors`，纯 TTS 用 `text_embedding.safetensors`。

`validateAndFillConfig` 读 `num_code_groups` 并推导 RVQ 维度，这是整个运行时的「尺寸主键」：

- [qwen3OmniTTSRuntime.cpp:L371-L378](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/qwen3OmniTTSRuntime.cpp#L371-L378) — `mNumRvqLayers = numCodeGroups - 1`、`mNumCodesPerFrame = numCodeGroups`。

采样参数分两套（注意 CodePredictor 的默认值是 HF 硬编码的）：

- [qwen3OmniTTSRuntime.h:L62-L68](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/qwen3OmniTTSRuntime.h#L62-L68) — CodePredictor 采样默认 `temperature=1.0, top_k=50, top_p=0.8`；输出常量 `kAudioSampleRate=24000`、`kAudioSamplesPerFrame=1920`。

批量 Talker 生成循环 `runTalkerGenerationLoop` 每帧分三个相位：

- [qwen3OmniTTSRuntime.cpp:L1654-L1737](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/qwen3OmniTTSRuntime.cpp#L1654-L1737) — **Phase A** 抽取每批 Talker 末位隐状态；**Phase B** 调 `runCodePredictorGenerationForFrame` 为所有活跃批一次性补齐本帧 RVQ；**Phase C** 逐批算残差连接。

CodePredictor 的「每步换绑一个 lm_head」是理解 RVQ 生成的钥匙——引擎把 `lm_head_weight` 做成动态输入张量，每层 code 用不同的权重：

- [qwen3OmniTTSRuntime.cpp:L1966-L1985](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/qwen3OmniTTSRuntime.cpp#L1966-L1985) — CP prefill 产出 code_1（`lm_head[0]`）。
- [qwen3OmniTTSRuntime.cpp:L2023-L2085](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/qwen3OmniTTSRuntime.cpp#L2023-L2085) — CP decode 循环（step=2..mNumRvqLayers）：每步用上一步采样（留在 device 张量里，免主机往返）查 embedding、换绑 `lm_head[step-1]`、解码、采样；所有步的 D2H 拷贝**延迟到循环末尾一次 sync**（pinned buffer）。

残差连接把本帧的 codec embedding 之和加上 trailing 文本隐状态（或 `tts_pad`），作为下一步 Talker 的输入：

- [qwen3OmniTTSRuntime.cpp:L2103-L2144](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/qwen3OmniTTSRuntime.cpp#L2103-L2144) — 残差加和，匹配 PyTorch 参考实现的 step 边界判断。

流式入口 `handleStreamingGeneration` 把 thinker 与 talker 绑在同一条流上，靠 per-token 回调驱动：

- [qwen3OmniTTSRuntime.cpp:L2534-L2685](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/qwen3OmniTTSRuntime.cpp#L2534-L2685) — 装在 thinker decode 循环上的回调：攒够 `talkerPrefillThreshold` 个 assistant token 触发 talker prefill；之后每来一个 token 增量 `appendTrailingToken` 并跑一帧 `runSingleTalkerDecodeFrame`。注释强调该回调只能在同线程同步调用（thinker 的 `handleRequest` 在 decode 循环的 `cudaStreamSynchronize` 里同步触发它）。

#### 4.2.4 代码实践

**实践目标**：搞清「一帧的 16 个 code 分别由谁生成」，并验证 lm_head 动态换绑机制。

**操作步骤（源码阅读型）**：

1. 打开 `runCodePredictorGenerationForFrame`（[L1894](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/qwen3OmniTTSRuntime.cpp#L1894)），确认 `outputCodesPerBatch[b]` 的第 0 个 code 来自入参 `codecTokensPerBatch[b]`（即 Talker 已采样的 code_0），而 code_1..15 来自 CP。
2. 在 CP decode 循环（[L2023](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/qwen3OmniTTSRuntime.cpp#L2023)）里找到「换绑 lm_head」的那一行（`mCodePredictorTensorMap.set(binding_names::kLmHeadWeight, mCodePredictorLmHeadWeights[...])`），数一数一次 Omni 帧要换绑多少次（答案：prefill 绑 head 0 一次，decode 循环 step=2..15 共 14 次，合计 15 个 head）。
3. 在 `executeCodePredictorDecodingStep`（[L1005](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/qwen3OmniTTSRuntime.cpp#L1005)）里确认 CP 每帧都会 `resetForNewSequences`（CP 无跨帧 KV 状态），与 Talker 的 KV 跨帧复用形成对比。

**需要观察的现象**：CP decode 循环里每步采样结果先写进 device 张量 `mCodePredictorSelectedIndices`，下一步直接拿它查 embedding（不走主机）；所有 code 的 D2H 拷贝在循环外只做**一次** sync。

**预期结果**：你能说清「Talker 出 code_0 → CP prefill 出 code_1 → CP decode 出 code_2..15」这 16 个 code 的生产者与顺序，并解释为何把 D2H sync 推迟到循环末尾是安全的（CP 无 EOS、无 per-token 回调、同流保序）。

**待本地验证**：若已构建并跑过 Omni 推理，在 profiling 输出里确认 `kCODEPREDICTOR_GENERATION` 阶段的 GPU 时间约占单帧大头（CP 要跑 15 个 head）；无 GPU 时源码阅读结论已足够。

#### 4.2.5 小练习与答案

**练习 1**：Talker 和 CodePredictor 的 KV 缓存生命周期有何本质区别？

**参考答案**：Talker 的 KV 缓存跨帧复用（像普通 LLM 一样 prefill 一次、之后每帧 decode 步进 +1，见 `executeTalkerDecodingStep` 末尾的 `commitSequenceLength(/*increment=*/1)`）；CodePredictor 则**每帧都重置** KV（`executeCodePredictorPrefillStep` 开头 `resetForNewSequences`），因为它只在一帧内部自回归（2..15 层），帧与帧之间无状态依赖。

**练习 2**：为什么 CodePredictor 的 lm_head 要做成引擎的「动态输入张量」而不是直接烤进引擎？

**参考答案**：因为 15 个 RVQ 层各有独立的 lm_head 权重，若都烤进引擎会重复存储且无法按层切换；做成动态输入后，运行时每步用 `setTensorAddress(kLmHeadWeight, ...)` 换绑对应层的权重即可（见 `executeCodePredictorDecodingStep`）。这也让 CUDA graph 能按「绑定哈希」自然区分不同 head 的图槽位。

---

### 4.3 code2WavRunner：RVQ 码 → 波形（声码器）

#### 4.3.1 概念说明

`Code2WavRunner` 是声码器（vocoder），职责单一：吃进一帧帧的 RVQ 码 `[numQuantizers][seqLen]`，跑 `code2wav.engine`，产出 PCM 波形 `[1, samples]`。它是「码」与「可播放音频」之间的最后一座桥。和前两个组件不同，它既不接 thinker、也不是 MultimodalRunner，就是个独立的 TRT 引擎封装，由示例层（如 `qwen3_tts_inference.cpp`）在拿到 RVQ 码后显式调用。

#### 4.3.2 核心流程

```
RVQ codes [numQuantizers][seqLen]  (来自 TTSRuntime 的 batchRvqCodes 转置)
   │
   ├─ seqLen ≤ maxCodeLen ?  直接推理 :  分块推理 (runChunkedInference)
   │
   ▼
code2wav.engine  →  waveform [1, 1, samples]   (FP16 for Qwen3-Omni / FP32 for Qwen3-TTS)
   │
   ▼
AudioData { waveform, sampleRate=24000, numChannels=1, hasWaveform=true }
```

关键点：波形长度 ≈ `seqLen × upsampleRate`；`upsampleRate` 由配置里所有 `upsample_rates` 与 `upsampling_ratios` 连乘得出。长音频会超出引擎最大 profile，此时走分块推理：按 `chunkSize` 切片、每片带 `leftContextSize` 重叠以消除边界伪影、再裁掉重叠段拼接。

#### 4.3.3 源码精读

构造函数加载 `code2wav.engine`、设单 profile（声码器无 prefill/decode 双 profile之分）：

- [code2WavRunner.cpp:L46-L81](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/code2WavRunner.cpp#L46-L81) — 加载引擎、创建上下文、设置 profile 0。

`validateAndFillConfig` 连乘所有上采样率得到 `upsampleRate`（码→样本的总放大倍数）：

- [code2WavRunner.cpp:L118-L141](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/code2WavRunner.cpp#L118-L141) — `rate *= upsample_rates[i]` 再 `*= upsampling_ratios[i]`。

`allocateBuffer` 探测引擎实际输出 dtype——这一步至关重要，搞错会把字节重解释成乱音：

- [code2WavRunner.cpp:L172-L176](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/code2WavRunner.cpp#L172-L176) — Qwen3-Omni 的 code2wav 输出 FP16，Qwen3-TTS 的 tokenizer_decoder 输出 FP32；运行时按引擎事实分配。

`generateWaveform` 按 seqLen 选直推或分块，并裁到期望长度（某些声码器总返回 max-profile 长度，需裁掉 stale 缓冲）：

- [code2WavRunner.cpp:L365-L411](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/code2WavRunner.cpp#L365-L411) — 短序列直推、长序列 `runChunkedInference`；末尾设 `sampleRate=24000, numChannels=1`。
- [code2WavRunner.cpp:L266-L346](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/code2WavRunner.cpp#L266-L346) — 分块：每片带 `leftContextSize` 重叠，推理后裁掉 `contextSamples` 再拼接。

#### 4.3.4 代码实践

**实践目标**：跑通「RVQ 码 → .wav」并理解为何要转置、为何要分块。

**操作步骤**：

1. 打开 `examples/omni/qwen3_tts_inference.cpp`，定位 `code2wavRunner->generateWaveform(transposed, audioOutput, stream)`（[L618](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/omni/qwen3_tts_inference.cpp#L618)），向上看 `transposed` 是怎么从 `framesCodes`（`[frames][codesPerFrame]`）转置成 `[layers][frames]` 的（[L607-L616](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/omni/qwen3_tts_inference.cpp#L607-L616)）。
2. 在 `generateWaveform`（[L348](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/code2WavRunner.cpp#L348)）里找到「seqLen ≤ maxCodeLen 直推、否则分块」的分支，说明 `maxCodeLen` 来自引擎的 kMAX profile。
3. 阅读分块推理里「裁掉 `contextSamples`」的那段（[L307-L316](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/code2WavRunner.cpp#L307-L316)），解释为什么重叠部分必须裁掉。

**需要观察的现象**：TTSRuntime 产出的 `batchRvqCodes[k]` 形状是 `[frames][numCodesPerFrame]`（帧优先），而 code2wav 引擎要的是 `[numQuantizers][seqLen]`（层优先），所以中间必须转置。

**预期结果**：你能解释「帧优先 → 层优先转置 → 直推或分块推理 → 裁重叠 → 拼接 → .wav」全链，并算出 N 帧≈ `N×1920` 个样本 ≈ `N/12.5` 秒。

**待本地验证**：在有引擎的环境跑 `qwen3_tts_inference --outputAudioDir=...`，用 `--dumpOutput` 打印 `[k] Audio: <samples> (<秒>)`；确认样本数 ≈ 帧数 × 1920。无引擎时源码阅读结论已足够。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Code2WavRunner` 要在运行时探测波形 dtype，而不是写死 FP16？

**参考答案**：因为不同模型族的声码器输出 dtype 不同——Qwen3-Omni 的 code2wav 是 FP16，Qwen3-TTS 的 tokenizer_decoder 是 FP32（见源码注释）。写死会在 FP32 引擎上把每 4 字节当成两个 FP16，重解释字节产生乱音。运行时按引擎 `getTensorDataType` 事实分配最稳妥。

**练习 2**：长音频为什么必须分块推理？分块为何要带 `leftContextSize` 重叠？

**参考答案**：引擎的 kMAX profile 限定了最大输入码长，超长音频（长语音）放不进一次推理，必须分块。带 `leftContextSize` 重叠（默认 25 帧）是为了给 CausalConvNet 提供左侧上下文、消除块边界的卷积伪影；推理后再把每块开头的 `contextSamples` 个样本裁掉，只保留有效输出，最后按序拼接。

---

## 5. 综合实践：画出 Qwen3-Omni 组件协作图

**任务**：基于本讲三个模块，画出 Qwen3-Omni 从「文本+音频输入」到「文本+音频输出」的完整组件协作图，标注 thinker / talker(code_predictor) / code2wav / audioRunner 各自的角色与产物。

**操作步骤**：

1. 先阅读 Omni 端到端示例 `examples/llm/llm_inference.cpp` 里创建 TTSRuntime 的位置（[L603-L611](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/llm_inference.cpp#L603-L611)），以及流式（[L764](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/llm_inference.cpp#L764)）与非流式（[L904](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/llm_inference.cpp#L904)）两条调用路径。
2. 用方框 + 箭头画出下面的协作图（建议在纸上或任意画图工具里完成），每个方框标注「组件名 / 角色 / 产物」：

```
[输入] 文本 prompt + 音频 PCM
   │
   ▼
┌───────────────────────────┐
│ Qwen3OmniAudioRunner       │  角色：音频输入编码
│  PCM → mel(Whisper fbank)  │  产物：audio embedding [tokens, 2560]
│  → audio_encoder.engine    │        + totalAudioTokens
└─────────────┬──────────────┘
              │ embedding 替换 <|audio_pad|> 占位
              ▼
┌───────────────────────────┐
│ Thinker (LLMInferenceRuntime)│ 角色：理解文本+音频，生成文本/隐状态
│  text + audio tokens        │ 产物：assistant 文本 token 流
│  → llm.engine               │       + 层0 embedding + 层N hidden
└─────────────┬──────────────┘
              │ (流式: per-token 回调; 非流式: 一次性)
              ▼
┌───────────────────────────┐
│ Qwen3OmniTTSRuntime        │  角色：生成语音 RVQ 码
│  Talker (llm.engine)       │   - Talker: 每帧 code_0 + 隐状态
│   → code_0                 │   - CodePredictor: 每帧 code_1..15
│  CodePredictor (llm.engine │     (15 个 lm_head 逐层换绑)
│   + 动态 lm_head) → code_n │  产物：RVQ 码 [frames][16]
└─────────────┬──────────────┘
              │ 转置为 [16][frames]
              ▼
┌───────────────────────────┐
│ Code2WavRunner             │  角色：声码器
│  code2wav.engine           │  产物：PCM 波形 [1, samples]
│  (直推 / 分块)             │        sampleRate=24000
└─────────────┬──────────────┘
              │
              ▼
[输出] 文本 (来自 thinker) + audio.wav (来自 code2wav)
```

3. 在图上额外标注三个「跨组件契约」：
   - audioRunner → thinker：占位 token id 与 embedding 行数必须一致（`totalAudioTokens`）。
   - thinker → talker：层 0 embedding 与 `accept_hidden_layer` 指定层的 hidden（流式经 portal，非流式经显式张量）。
   - talker → code2wav：RVQ 码必须从「帧优先」转置成「层优先」。

**预期结果**：你能指着图说清——音频**输入**经 `audioRunner` 进 thinker，音频**输出**由 thinker 经 `qwen3OmniTTSRuntime`（Talker+CodePredictor）变成 RVQ 码、再经 `code2WavRunner` 变成波形；三者方向、角色、产物都不混淆。

**待本地验证**：若已构建并跑过 Omni 推理（`llm_inference` 带 `--generateAudio`），对照 `--dumpProfile` 输出确认图中每个阶段都有对应的计时项（`kAUDIO_ENCODER`、`kTALKER_PREFILL`、`kTALKER_GENERATION`、`kCODEPREDICTOR_GENERATION`、`kCODE2WAV`）。无 GPU 时，画图 + 源码引用即为完成。

## 6. 本讲小结

- 音频在运行时里是**双向**的：`Qwen3OmniAudioRunner` 负责**输入**（PCM→mel→audio embedding，喂给 thinker），`Qwen3OmniTTSRuntime` + `Code2WavRunner` 负责**输出**（thinker 产物→RVQ 码→波形）。
- `audioRunner` 是 `MultimodalRunner` 子类，自己拥有 mel 提取（GPU fbank 优先、CPU MelExtractor 兜底，产出相同契约），编码器在 preprocess 阶段就跑完，故 `infer()` 是 no-op。
- `Qwen3OmniTTSRuntime` 是独立运行时（非 MultimodalRunner），管理 Talker + CodePredictor 两台 LLM 引擎，靠共享 user-managed 上下文显存复用资源。
- 一帧 RVQ 码的 16 个（Omni）code 分工明确：code_0 由 Talker 采样，code_1..15 由 CodePredictor 的 15 个 lm_head 逐层换绑生成；Talker 的 KV 跨帧复用、CodePredictor 的 KV 每帧重置。
- 三种生成入口对应三种场景：`handleAudioGeneration`（纯文本 TTS）、`handleAudioGenerationFromThinker`（Omni 非流式）、`handleStreamingGeneration`（Omni 流式，thinker/talker 同流交错、靠 per-token 回调驱动）。
- `Code2WavRunner` 是声码器：RVQ 码（层优先）→ 波形（24kHz），短序列直推、长序列分块带重叠裁剪拼接；运行时按引擎事实探测 FP16/FP32 输出 dtype。
- 关键常量：帧率 12.5 Hz、`kAudioSamplesPerFrame=1920`、`kAudioSampleRate=24000`；Omni `num_code_groups=16`（TTS=32）。

## 7. 下一步学习建议

- 想深入「流式交错」的同步细节，精读 `handleStreamingGeneration` 的回调（[qwen3OmniTTSRuntime.cpp:L2449-L2778](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/qwen3OmniTTSRuntime.cpp#L2449-L2778)），结合 u5 的 `LLMInferenceRuntime::handleRequest` 理解 per-token 回调为何必须同线程同步触发。
- 想理解 mel 提取的 GPU kernel 实现，阅读 `cpp/multimodal/audioUtils.h` 与 `cpp/runtime/melSpectrogram.h`、`cpp/kernels/` 下 fbank 相关源（及 `CuteDslGemmRunner`），这会承接 u8 的自定义 CUDA 算子专题。
- 想把整条多模态链路串起来，回到 u6-l1 对比「视觉编码器」与「音频编码器」在 `MultimodalRunner` 框架下的异同，并阅读 `multimodalRunner.h` 的 `create()` 工厂理解 `model_type` 如何路由到具体 runner。
- 若计划接入新的音频/语音模型，参考 [u9-l4 接入一个新模型架构](u9-l4-adding-a-new-model-architecture.md) 的思路，重点关注 config 解析、权重 sidecar（`codec_embeddings.safetensors`、`lm_heads.safetensors`、`text/hidden_projection.safetensors`）与引擎 I/O 契约。
