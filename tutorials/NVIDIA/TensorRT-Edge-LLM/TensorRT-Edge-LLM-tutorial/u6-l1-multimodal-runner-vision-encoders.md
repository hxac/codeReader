# 多模态运行器与视觉编码器

## 1. 本讲目标

本讲进入多模态（VLM）单元。学完后你应该能够：

1. 说清楚 `MultimodalRunner` 基类如何用一套统一接口（`preprocess → infer → getOutputEmbedding`）调度十几种截然不同的视觉/音频编码器。
2. 复现 Qwen-VL 家族的「动态分辨率 + VisionSpan + MRoPE + cu_seqlens」管线，理解一张任意尺寸的图如何被切成变长的视觉 patch。
3. 复现 InternVL 的「固定块（block）模型」，理解为什么它要求图像 token 数必须是 256 的倍数。
4. 解释视觉 embedding 如何通过「token id ≥ vocabSize」的技巧被无缝插入文本 token 序列，从而让 LLM 主干在完全不知情的情况下「读到」图像。

本讲承接 u5-l1（`LLMInferenceRuntime::handleRequest`）与 u4-l2（视觉引擎的 optimization profile），把多模态这块拼进运行时主循环。

## 2. 前置知识

- **VLM / ViT**：视觉语言模型（Vision-Language Model）由一个视觉编码器（Vision Transformer，ViT）和一个语言模型（LLM）组成。ViT 把图像变成一串向量（image embedding），LLM 把这串向量和文本 token 的 embedding 拼在一起继续做自回归解码。
- **patch**：ViT 把图像切成小方块（例如 14×14 像素一块），每块经线性投影成一个向量，称为一个 patch token。
- **embedding lookup**：LLM 把每个 token id 查表得到一个向量。普通文本 token 走词表；本讲会看到图像 token 走「另一张表」——即 ViT 的输出。
- **RoPE / MRoPE**：旋转位置编码（Rotary Position Embedding）给每个 token 注入位置信息。普通 RoPE 是一维的（按序列下标旋转）。**MRoPE（Multi-dimensional RoPE，多维旋转位置编码）** 是 Qwen-VL 的做法：位置被拆成 时间(T)、高(H)、宽(W) 三个维度，让视觉 token 的位置反映其二维空间坐标，而不是在序列里被压扁成一维。
- **cu_seqlens**：变长序列的「前缀和」数组，告诉 flash attention / varlen kernel 每一帧的 patch 从哪到哪。例如 `[0, 196, 392]` 表示第 1 帧 patch 0..195、第 2 帧 patch 196..391。
- **optimization profile**：承接 u4-l2。TensorRT 引擎的动态维度用 min/opt/max 三元组刻画。视觉引擎的输入维度（patch 数 / block 数）就是这样一个动态维度。

> 一句话直觉：**视觉编码器是一个独立的 TensorRT 引擎**，运行时在 prefill 之前先跑它，把图像变成一组向量，再把这些向量「伪装成 token」喂给 LLM 主干。`MultimodalRunner` 就是管理这个独立引擎、并对外暴露统一接口的基类。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [cpp/multimodal/multimodalRunner.h](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/multimodalRunner.h) | 多模态运行器抽象基类，定义 `preprocess/infer/getOutputEmbedding` 等接口 |
| [cpp/multimodal/multimodalRunner.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/multimodalRunner.cpp) | 基类实现 + `create()` 工厂（按 `model_type` 实例化子类） |
| [cpp/multimodal/modelTypes.h](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/modelTypes.h) | `ModelType` 枚举与 `stringToModelType` 字符串映射 |
| [cpp/multimodal/qwenViTRunner.h](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/qwenViTRunner.h) | Qwen-VL 运行器声明，含 `VisionSpan` 结构体与策略钩子 |
| [cpp/multimodal/qwenViTRunner.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/qwenViTRunner.cpp) | Qwen-VL 实现：动态分辨率、MRoPE、cu_seqlens |
| [cpp/multimodal/internViTRunner.h](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/internViTRunner.h) / [.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/internViTRunner.cpp) | InternVL 实现：固定块、256 tokens/block |
| [cpp/runtime/preprocess/embeddingPreprocessor.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/preprocess/embeddingPreprocessor.cpp) | 图文融合：把视觉 embedding 插入 token 序列 |
| [cpp/builder/visualBuilder.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/visualBuilder.cpp) | 视觉引擎构建，解释 256 的来源 |

---

## 4. 核心概念与源码讲解

### 4.1 MultimodalRunner：统一调度基类与工厂

#### 4.1.1 概念说明

EdgeLLM 支持的视觉/音频编码器多达十几种（Qwen2-VL / Qwen2.5-VL / Qwen3-VL / Qwen3-Omni / InternVL / Phi4-MM / Gemma4 / Nemotron-Omni ……），它们的图像预处理方式、位置编码方式、I/O 张量都不同。如果让运行时主循环去逐个 `if/else` 处理，会极其臃肿。

`MultimodalRunner` 的作用就是把这十几种编码器抽象成**同一组接口**，让上层（`LLMInferenceRuntime`）只需调三个方法：

- `preprocess(request, ...)`：吃请求里的图像/文本，做归一化、切块、生成位置编码，同时把文本里的图像占位符 `<image>` 展开成若干个图像 token id。
- `infer(stream)`：跑视觉引擎，得到 `mOutputEmbedding`。
- `getOutputEmbedding()`：把视觉向量交出去，供主循环插进 LLM 输入。

它同时是一个**工厂（factory）**：静态方法 `create()` 读视觉引擎目录里的 `config.json`，按 `model_type` 字段实例化正确的子类。运行时主循环只持有 `std::unique_ptr<MultimodalRunner>` 基类指针，完全不知道具体是哪一种编码器。

#### 4.1.2 核心流程

```
LLMInferenceRuntime::handleRequest
        │  MultimodalRunner::create(engineDir/visual/, ...)   ← 工厂按 model_type 选子类
        ▼
   mVisionRunner (基类指针)
        │
        ├──> preprocess(request, batchedInputIds, tokenizer, mropeCosSinOut, stream)
        │        ├── imagePreprocess()  归一化+切块 → mVitInput + cu_seqlens + 位置编码
        │        └── textPreprocess()   把 <image> 占位符展开成 N 个 id≥vocabSize 的 token
        │
        ├──> infer(stream)              跑视觉引擎 → mOutputEmbedding [numImageTokens, hidden]
        │
        └──> getOutputEmbedding()       交出向量，主循环用它替换序列里的图像 token
```

注意一个关键设计点：视觉引擎的执行上下文（`IExecutionContext`）用 **USER_MANAGED** 内存策略，即引擎自己不分配 device 内存，而是由运行时统一分配一块共享显存 `mSharedExecContextMemory`，再通过 `setContextMemory` 同时喂给 LLM 执行器和视觉执行器（两者复用同一块上下文显存）。

#### 4.1.3 源码精读

基类构造函数加载视觉引擎 `visual.engine`，并采用 user-managed 内存（device 内存之后由 `setContextMemory` 注入）：

[cpp/multimodal/multimodalRunner.cpp:49-73](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/multimodalRunner.cpp#L49-L73) —— 反序列化 `visual.engine`，创建 `kUSER_MANAGED` 上下文，并设置 optimization profile 0、挂上 non-blocking 辅助流。

工厂 `create()` 是本节核心。它解析 `config.json` 的 `model_type`，转成 `ModelType` 枚举，然后用一连串 `if/else` 实例化子类：

[cpp/multimodal/multimodalRunner.cpp:117-204](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/multimodalRunner.cpp#L117-L204) —— 按 `model_type` 分发。可以观察到几个值得注意的设计：

- **Qwen 家族共用一套两阶段初始化**：`QWEN2_VL`/`QWEN2_5_VL`/`QWEN3_VL`/`QWEN3_5`/`QWEN3_OMNI_VISION_ENCODER` 都走 `makeInitializedQwenViTRunner<T>()`（[cpp/multimodal/multimodalRunner.cpp:107-114](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/multimodalRunner.cpp#L107-L114)）。它先 `make_unique<RunnerT>()` 构造，**再单独调用 `initialize(stream)`**。这个「构造与初始化分离」是故意的——子类的虚函数钩子（`validateExtraConfig` 等）要等到对象构造完成后才能正确分发到子类，所以必须分两步。
- **InternVL / Phi4-MM / Gemma4 / Nemotron 等走各自独立的 `make_unique`**：它们的预处理差异大到无法共享 Qwen 那套继承体系，各自直接在构造函数里完成配置与缓冲分配。

`model_type` 字符串到枚举的映射在 modelTypes.h：

[cpp/multimodal/modelTypes.h:51-85](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/modelTypes.h#L51-L85) —— 注意 InternVL 接受 `"internvl"` 或 `"internvl_vision"`，Phi4-MM 只接受 `"phi4mm"`。这套字符串必须和 Python 导出端写入 `config.json` 的 `model_type` 完全一致，否则落到 `UNKNOWN` 抛异常。

三个统一接口在头文件里声明：

[cpp/multimodal/multimodalRunner.h:110-135](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/multimodalRunner.h#L110-L135) —— `preprocess` 与 `infer` 是纯虚函数（`= 0`），`getOutputEmbedding` 有默认实现直接返回 `mOutputEmbedding` 成员。注意 `preprocess` 的 `mropeCosSinOut` 参数：只有用 MRoPE 的编码器（Qwen 家族）才会写它，用标准 RoPE 的编码器（InternVL/Phi4-MM）忽略它。

#### 4.1.4 代码实践（源码阅读型）

**目标**：理解工厂的分发边界与「两阶段初始化」的必要性。

**步骤**：

1. 打开 [multimodalRunner.cpp 的 create()](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/multimodalRunner.cpp#L117-L204)。
2. 数一数：共有多少种 `model_type` 会被映射到「Qwen 继承体系」（即走 `makeInitializedQwenViTRunner`），又有多少种走独立 `make_unique`？
3. 思考：为什么 Qwen 家族要单独把 `initialize(stream)` 拆出来调用，而 InternVL 直接在构造函数里就调了 `validateAndFillConfig` + `allocateBuffer`？（提示：InternVL 没有子类继承，构造期就是最终类型；Qwen 有 `Qwen3VLViTRunner` 等子类要重写虚函数钩子。）

**预期观察**：Qwen 家族共 5 个 model_type 走继承体系；InternVL/Phi4MM/Gemma4(N个)/Nemotron 等走独立构造。

**待本地验证**：若你手边有导出好的视觉引擎目录，可打印其 `config.json` 的 `model_type`，对照 modelTypes.h 确认会落到哪个分支。

#### 4.1.5 小练习与答案

**练习 1**：如果有一种全新的视觉编码器要接入，工厂 `create()` 里要加几处改动？
**答案**：(1) 在 `modelTypes.h` 的 `ModelType` 枚举加一个值，并在 `stringToModelType` 加字符串映射；(2) 写一个继承 `MultimodalRunner` 的子类，实现 `preprocess/infer/validateAndFillConfig/allocateBuffer`；(3) 在 `create()` 的 `if/else` 链加一个分支 `make_unique`。

**练习 2**：基类里 `mVisualEngine` 和 `mAudioEngine` 为什么都保留？（看 `getRequiredContextMemorySize`）
**答案**：因为有些子类是「音频专用」runner（如 `Qwen3OmniAudioRunner`），其 `mVisualEngine` 为空、`mAudioEngine` 非空；`getRequiredContextMemorySize` / `setContextMemory` 用 `mAudioEngine ? mAudioEngine : mVisualEngine` 选择真正要配上下文内存的引擎，这样基类能同时服务纯视觉与纯音频子类。

---

### 4.2 QwenViTRunner：动态分辨率与 VisionSpan

#### 4.2.1 概念说明

Qwen-VL（含 Qwen2-VL / Qwen2.5-VL / Qwen3-VL）是 EdgeLLM 里最复杂也最通用的视觉编码器家族，本类 `QwenViTRunner` 是其基类实现（= Qwen2-VL），其余型号是它的子类。它的图像处理有三个核心思想：

1. **动态分辨率（dynamic resolution）**：不把所有图统一 resize 到固定大小，而是允许 patch 网格在 `[minImageTokensPerImage, maxImageTokensPerImage]` 范围内变化，只要高宽都凑成 `patchSize × mergeSize` 的整数倍。大图多切几块、小图少切几块，保留分辨率信息。

2. **VisionSpan 双视图**：一张图（或一段视频帧组）同时用两个视角描述：
   - **ViT 视图（`VitFrameGrid`）**：ViT 引擎实际看到的 patch 网格 `gridT × gridH × gridW`（时间×高×宽，patch 为单位），用来填 `cu_seqlens` 与 rotary 位置编码。
   - **LLM 视图（`LlmVisionBlock`）**：LLM/MRoPE 看到的 token 数 `llmGridT × llmGridH × llmGridW`（merge 之后的单位），用来展开文本里的占位符。

   两者的换算是 `mergeSize`：ViT 的 \(2\times2\) 个 patch 被「合并（merge）」成 1 个送进 LLM 的 token。所以 `llmGridH = gridH / mergeSize`。

3. **MRoPE（多维位置编码）**：文本 token 在三个位置维度（T,H,W）上同步递增；视觉 token 则按其空间坐标 \((t,h,w)\) 编码，让注意力感知二维结构。

> 直觉：Qwen-VL 把「一张可变尺寸的图」翻译成「一段可变长度的、带二维空间坐标的 token 序列」，再无缝插进文本流。

#### 4.2.2 核心流程

`preprocess` 是总入口，依次完成图像处理、文本展开、MRoPE 生成：

```text
preprocess(request, batchedInputIds, mropeCosSinOut, stream)
  │
  ├─ imagePreprocess()
  │     for 每张图 buffer:
  │        getResizedImageSize()       # 动态 resize 到 patchSize*mergeSize 的倍数
  │        formatPatch()               # 归一化 + transposeToPatch + 追加 VisionSpan
  │           └─ computeVisionSpans()  # 算 gridT/H/W 与 llmGridH/W
  │     buildCuSeqlens()               # 每帧 gridH*gridW 一个段，前缀和
  │     initRotaryPosEmbQwenViT()      # ViT 侧 3D rotary 位置编码
  │     buildExtraInputs()             # 子类钩子（窗口注意力等）
  │     [缓存优化] 若 spans 与上次相同，跳过 cu_seqlens/rotary 重算
  │
  ├─ textPreprocess()
  │     把每个 <image_pad>/<video_pad> 占位符展开成
  │     numTokens 个递增的 id（从 vocabSize 开始计数）
  │
  └─ generateMropeParams()
        getMRopePositionIds()          # [bs,3,seqLen] 的 T/H/W 三维位置 id
        initializeMRopeCosSin()        # 算 cos/sin 缓存写入 mropeCosSinOut
```

随后 `infer()` 跑视觉引擎，产出 `mOutputEmbedding [numImageTokens, outHiddenSize]`。

#### 4.2.3 源码精读

**VisionSpan 双视图结构体**定义在头文件，是理解本类的钥匙：

[cpp/multimodal/qwenViTRunner.h:30-61](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/qwenViTRunner.h#L30-L61) —— `LlmVisionBlock`（LLM 视图）和 `VitFrameGrid`（ViT 视图），合在一起就是 `VisionSpan`。注意 `VitFrameGrid` 还带 `patchStart`（本 span 在全局 ViT patch 序列里的起始偏移）和一个 `operator==`，后者是缓存命中的判等键。

**动态 resize** 的实现严格对齐 HuggingFace 参考实现（含「银行家舍入」round-half-to-even）：

[cpp/multimodal/qwenViTRunner.cpp:384-430](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/qwenViTRunner.cpp#L384-L430) —— 核心：`factor = patchSize * mergeSize`，把高宽分别 round 到 factor 的倍数；若像素数超出 `maxPixels` 就按 \( \beta=\sqrt{HW/\text{maxPixels}} \) 等比缩小，若小于 `minPixels` 就放大。最终保证 patch 网格落在 `[minImageTokens, maxImageTokens]` 内且是 factor 的整数倍。

**VisionSpan 的计算**——把一张已 resize 好的图翻译成两个视角：

[cpp/multimodal/qwenViTRunner.cpp:285-311](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/qwenViTRunner.cpp#L285-L311) —— 关键公式：

\[
\text{gridT}=\lceil \text{frames}/\text{temporalPatchSize}\rceil,\quad
\text{gridH}=H/\text{patchSize},\quad \text{gridW}=W/\text{patchSize}
\]

\[
\text{llmGridH}=\text{gridH}/\text{mergeSize},\quad
\text{llmGridW}=\text{gridW}/\text{mergeSize},\quad
\text{tokensPerFrame}=\text{llmGridH}\cdot\text{llmGridW}
\]

静态图 `frames=1`，所以 `gridT=1`；视频则每帧贡献一个 `gridH*gridW` 的 patch 块。

**cu_seqlens 构建**——把每帧的 patch 块做前缀和：

[cpp/multimodal/qwenViTRunner.cpp:358-382](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/qwenViTRunner.cpp#L358-L382) —— 每个 span 按 `gridT` 展开成 `gridT` 个 `gridH*gridW` 大小的段，逐段累加写入 `cu_seqlens`。这正是 varlen/flash attention 需要的「每帧边界」。

**缓存优化**是性能关键：如果本次的 spans 几何与上次完全相同（`VitFrameGrid::operator==`），就跳过 `cu_seqlens`、rotary、extra inputs 的全部重算：

[cpp/multimodal/qwenViTRunner.cpp:480-518](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/qwenViTRunner.cpp#L480-L518) —— `vitGeometryUnchanged` 用 `std::equal` 逐 span 比对 ViT 视图。多轮同尺寸图像对话时这能省掉大量不变张量的初始化。

**文本展开**——这是与图文融合（4.4 节）的接口：

[cpp/multimodal/qwenViTRunner.cpp:632-682](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/qwenViTRunner.cpp#L632-L682) —— 注意 `int32_t nextImageTokenId = mConfig.vocabSize;`：每个图像占位符被展开成 `block.numTokens` 个**从 vocabSize 起递增的 id**。这些 id 不在词表里，它们是「占位编号」，4.4 节会看到运行时如何把它们映射成 ViT 输出向量。

**MRoPE 三维位置 id** 是 Qwen-VL 区别于普通 LLM 的核心：

[cpp/multimodal/qwenViTRunner.cpp:521-596](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/qwenViTRunner.cpp#L521-L596) —— 位置 id 张量形状是 `[bs, 3, maxPosEmbeddings]`，三个通道分别是 T/H/W。文本段三个通道同步递增；视觉段则分别填 `t / h / w`，即把 token 的二维坐标编进位置。末尾还计算 `mropeRopeDeltasPerBatch`（= `maxMropePositionId + 1 - inputIdSize`），用于让后续生成 token 的位置 id 从正确起点继续（因为视觉 span 让位置 id 的推进不均匀）。

#### 4.2.4 代码实践（源码阅读型）

**目标**：用具体数字走通一张静态图，体会动态分辨率与 merge。

**步骤**：

1. 假设配置 `patchSize=14, mergeSize=2, temporalPatchSize=2, minImageTokensPerImage=256, maxImageTokensPerImage=1280`。
2. 设想输入一张 resize 后为 `H=672, W=448` 的静态图（`frames=1`）。
3. 在 [computeVisionSpans](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/qwenViTRunner.cpp#L285-L311) 里手算：
   - `factor = 14*2 = 28`，672 和 448 都是 28 的倍数 ✓
   - `gridT = ceil(1/2) = 1`
   - `gridH = 672/14 = 48`，`gridW = 448/14 = 32`
   - `llmGridH = 48/2 = 24`，`llmGridW = 32/2 = 16`
   - 一张图送进 LLM 的 token 数 = `24*16 = 384`
4. 在 [buildCuSeqlens](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/qwenViTRunner.cpp#L358-L382) 里写出 cu_seqlens：`[0, 48*32] = [0, 1536]`。

**需要观察的现象**：ViT 看到 `1×48×32 = 1536` 个 patch；经 `mergeSize=2` 合并后，LLM 只看到 `384` 个图像 token——这正是「ViT 视图」与「LLM 视图」的 4 倍差（\(2\times2=4\)）。

**待本地验证**：上述数字是否在 `[256, 1280]` 范围内？是（384），所以这次 resize 合法。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Qwen-VL 要引入 `mergeSize`？直接把所有 ViT patch 送进 LLM 不行吗？
**答案**：ViT patch 数（如 1536）远大于 LLM 能高效处理的长度。merge 把相邻 \(2\times2\) patch 合并成 1 个 token，把图像 token 数压缩到 1/4，显著降低 LLM 的序列长度与 KV 缓存开销，同时由 ViT 内部的 merge 投影保留信息。

**练习 2**：`mropeRopeDeltasPerBatch` 为什么不为零？
**答案**：MRoPE 在视觉段按二维空间推进位置 id，而生成阶段的新 token 必须从「序列中已用过的最大位置 id + 1」继续，不能简单按 token 计数。这个 delta 记录了「位置 id 比线性计数多出多少」，供生成阶段修正后续 token 的位置编码。

---

### 4.3 InternViTRunner：固定块与 256 tokens/block

#### 4.3.1 概念说明

InternVL（及其同类 Phi4-MM）走的是和 Qwen-VL **截然不同**的另一条路：**固定块（block）模型**。

- 图像被切成若干个固定大小的「块」，每块是 `blockImageSizeH × blockImageSizeW` 像素（典型 448×448）。
- 块数 = `(H/blockImageSizeH) × (W/blockImageSizeW)`，随图像尺寸变化。
- **每块硬编码产出 256 个 token**（引擎内部 16×16 patch 网格）。因此总图像 token 数永远是 256 的倍数：`totalImageTokens = numBlocks × 256`。
- 若一张图超过 1 个块，还会额外生成一张「缩略图（thumbnail）」整图压缩成 1 个块，附在最后，让模型既看到细节也看到全局。

对比 Qwen-VL：Qwen-VL 的图像 token 数可以是任意合法整数（受网格约束）；InternVL 的图像 token 数**只能是 256 的倍数**。这就是 4.4 节实践里「为什么 InternVL/Phi4 要求图像 token 为 256 的倍数」的根因。

InternVL 用**标准 RoPE**（不是 MRoPE），所以它的 `preprocess` 忽略 `mropeCosSinOut` 参数。

#### 4.3.2 核心流程

```text
preprocess(request, batchedInputIds, ..., stream)
  │
  ├─ imagePreprocess()
  │     for 每张图:
  │        computeBestBlockGridForResize()  # 选块数使 token 数落在 [min,max]
  │        resizeImage() → formatPatch()    # 每块归一化+transposeToPatch
  │        若 mainImageBlocks>1 或 minNumBlocks>1:
  │           生成 thumbnail，formatPatch(isThumbnail=true)  # 并入最后一张图
  │     reshape mVitInput=[totalNumBlocks, C, blockH, blockW]
  │
  ├─ textPreprocess()
  │     编码文本；遇 <image> 占位符 → 插 <img> + N 个 id≥vocabSize 的递增 token + </img>
  │
  └─ （无 MRoPE 生成）

infer(stream)  跑视觉引擎 → mOutputEmbedding [totalNumBlocks*256, outHiddenSize]
```

#### 4.3.3 源码精读

**配置与缓冲**——`outHiddenSize` 取自引擎输出形状，输出 embedding 容量按 `maxNumBlocks*256` 预分配：

[cpp/multimodal/internViTRunner.cpp:111-160](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/internViTRunner.cpp#L111-L160) —— 关键行：`mOutputEmbedding = rt::Tensor({mConfig.maxNumBlocks * 256, mConfig.outHiddenSize}, ...)`，注释明说「In InternVL3, each block generates 256 tokens」。`mVitInput` 形状是 `[maxNumBlocks, C, blockH, blockW]`——**输入的第 0 维是「块数」而不是像素**，这是它和 Qwen-VL（第 0 维是 patch 数）的根本区别。

**切块与 token 计数**：

[cpp/multimodal/internViTRunner.cpp:162-214](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/internViTRunner.cpp#L162-L214) —— `curNumBlocks = (height/blockImageSizeH) * (width/blockImageSizeW)`，`curTokenLength = curNumBlocks * 256`。注意 `isThumbnail` 分支：缩略图的 token 不另算一张图，而是 `imageTokenLengths.back() += curTokenLength` 并入当前图。

**缩略图策略**：

[cpp/multimodal/internViTRunner.cpp:240-251](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/internViTRunner.cpp#L240-L251) —— 当主图块数 `>1`，或引擎 MIN profile 要求 `>1` 块时，追加一张整图缩略图。后者是个细节：若引擎用 `--minImageTokens>256` 构建，单块图也要补缩略图凑够最小块数，否则触发 profile 越界。

**文本展开**（与 4.4 节接口一致）：

[cpp/multimodal/internViTRunner.cpp:282-331](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/internViTRunner.cpp#L282-L331) —— 与 Qwen-VL 思路相同：`imageTokenId = mConfig.vocabSize` 起递增。区别是 InternVL 在图像 token 前后显式包了 `<img>`（`imgStartTokenId`）和 `</img>`（`imgEndTokenId`）。

**为什么必须是 256 的倍数——构建器侧的证据**：

[cpp/builder/visualBuilder.cpp:417-430](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/visualBuilder.cpp#L417-L430) —— 注释「each image block contains 256 tokens (16x16 patch grid)」，并强制校验 `minImageTokens % 256 != 0 || maxImageTokens % 256 != 0` 则报错。`minNumBlocks = minImageTokens / 256`。也就是说：**视觉引擎的 optimization profile 是以「块数」为单位的**，而每块硬编码 256 token，所以总 token 数必须是 256 的倍数，无法表达「257 个图像 token」这种需求。

Phi4-MM 同理：[cpp/multimodal/phi4mmViTRunner.cpp:129-130](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/phi4mmViTRunner.cpp#L129-L130) —— 「In Phi-4MM, each block generates 256 tokens」。

#### 4.3.4 代码实践（参数对照型）

**目标**：把 u4-l3 学的 `visual_build` 参数与本类的块模型对上号。

**步骤**：

1. 假设你给 InternVL 执行 `visual_build --minImageTokens 256 --maxImageTokens 1024`。
2. 对照 [visualBuilder.cpp:417-430](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/visualBuilder.cpp#L417-L430) 推算：`minNumBlocks = 256/256 = 1`，`maxNumBlocks = 1024/256 = 4`。引擎可处理 1~4 块的图。
3. 对照 [internViTRunner.cpp:99-105](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/internViTRunner.cpp#L99-L106)：`maxNumBlocks` 取自引擎 profile 的 MAX 形状第 0 维（即 4），`minNumBlocks` 取自 MIN 形状第 0 维（即 1）。
4. 若误传 `--minImageTokens 300`（不是 256 的倍数），构建期就会因第 421 行校验失败而报错退出。

**需要观察的现象**：`minImageTokens/maxImageTokens` 在 InternVL/Phi4-MM 语境下必须被 256 整除；否则连引擎都构建不出来。

**待本地验证**：实际运行 `visual_build` 时传一个非 256 倍数，确认报错信息与本讲描述一致。

#### 4.3.5 小练习与答案

**练习 1**：一张被切成 3 个块的 InternVL 图，最终送进 LLM 多少个图像 token？
**答案**：3×256 = 768 个。又因为块数 >1，还会追加 1 块缩略图，所以是 4×256 = 1024 个（缩略图并入该图，不另算图）。

**练习 2**：InternVL 的 `mVitInput` 第 0 维和 Qwen-VL 的 `mVitInput` 第 0 维语义有何不同？
**答案**：InternVL 第 0 维是「块数」`[numBlocks, C, blockH, blockW]`，每块是完整像素图；Qwen-VL 第 0 维是「patch 数」`[numPatches, inputDim]`，已经是 patch 化的向量序列。前者引擎内部自己切块，后者由 host 侧先切好。

---

### 4.4 图文融合：token id ≥ vocabSize 的嵌入插入

#### 4.4.1 概念说明

前面三节都在讲「如何把图变成向量」。本节回答最后一个问题：**这些向量怎么进 LLM？**

答案是一个优雅的约定：**把图像 token 的 id 编码成 ≥ vocabSize 的递增整数**。于是在 embedding lookup 阶段：

- id `< vocabSize` → 正常查词表（文本 token）；
- id `≥ vocabSize` → 不查词表，而是按「id − vocabSize」当索引，取 ViT 输出 `mOutputEmbedding` 的第 `k` 行。

这样 LLM 主干拿到的输入 embedding 序列里，文本 token 和图像 token 已经**无缝交错**，主干完全无需感知「这里是图」。这个约定在 [textPreprocess](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/qwenViTRunner.cpp#L632-L682)（`nextImageTokenId = vocabSize`）和 InternVL 的对应代码里被写入，在 embedding preprocessor 里被消费。

> 直觉：图像 token 的 id 不是「词」，而是「第 k 个视觉向量」的指针。词表只覆盖 `[0, vocabSize)`，`≥ vocabSize` 的区间被借用为视觉索引空间。

#### 4.4.2 核心流程

```text
LLMInferenceRuntime (主循环)
  ├─ mVisionRunner->preprocess(...)   # 文本里写入了 id≥vocabSize 的图像占位
  ├─ mVisionRunner->infer(...)        # 得到 mOutputEmbedding [numImageTokens, hidden]
  ├─ mVisionRunner->getOutputEmbedding() → context.visualEmbeddings
  │
  └─ prefill 时 EmbeddingPreprocessor::embed(tokenIds, visualEmbeds, ...)
        若 visualEmbeds 有值（且是 legacy 视觉族）:
           kernel::embeddingLookupWithImageInsertion(tokenIds, 词表, 图像向量, out)
              对每个 id：id<vocabSize 查词表；id≥vocabSize 取图像向量[id-vocabSize]
        否则: embeddingLookup（纯文本查表）
```

#### 4.4.3 源码精读

**主循环的调用顺序**——先 preprocess+infer 视觉，再取出 embedding：

[cpp/runtime/llmInferenceRuntime.cpp:1255-1265](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1255-L1265) —— `mVisionRunner->preprocess(...)` 后紧跟 `mVisionRunner->infer(stream)`。

[cpp/runtime/llmInferenceRuntime.cpp:1305-1316](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1305-L1316) —— 关键注释「gate on request having multimodal data, not just runner existence, to avoid leaking stale embeddings」：只有当本次请求真的含视觉数据时才取 `getOutputEmbedding()`，否则传 `nullopt`，避免上一轮的残影图像向量泄漏到纯文本请求。

**融合 kernel 的分派**：

[cpp/runtime/preprocess/embeddingPreprocessor.cpp:38-85](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/preprocess/embeddingPreprocessor.cpp#L38-L85) —— 三条路径：

1. **显式 id 路径**（Omni 类、audioTokenId≥0）：用 `embeddingLookupMultimodal`，按 `audioTokenId`/`imageTokenId` 显式标记。
2. **Legacy 视觉路径**（Qwen2.5-VL、InternVL：id≥vocabSize 或未设）：`embeddingLookupWithImageInsertion`——就是本节讲的「id≥vocabSize 取图像向量」。
3. **纯文本**：`embeddingLookup`。

[cpp/runtime/preprocess/embeddingPreprocessor.cpp:73-79](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/preprocess/embeddingPreprocessor.cpp#L73-L79) —— Legacy 视觉路径的具体调用：`embeddingLookupWithImageInsertion(tokenIds, 词表, scales, imageEmbedsTensor, out, stream)`。该 kernel 内部对每个 token 判断 id 范围并选择词表行或图像向量行。

#### 4.4.4 代码实践（跟踪型）

**目标**：跟踪一张图从像素到 LLM 输入 embedding 的完整路径。

**步骤**（以 Qwen-VL 为例）：

1. **像素 → patch**：[formatPatch](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/qwenViTRunner.cpp#L313-L356) 做 H2D 拷贝、`normalizeImage`（减均值除标准差）、`transposeToPatchQwenViT`（像素重排成 patch 向量），写入 `mVitInput`。
2. **patch → ViT 输出**：[infer](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/qwenViTRunner.cpp#L750-L798) 跑视觉引擎，绑定 `mVitInput/rotary/cu_seqlens`，`enqueueV3` 得到 `mOutputEmbedding`。
3. **占位 id 写入**：[textPreprocess](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/qwenViTRunner.cpp#L632-L682) 把 `<image_pad>` 展开成 `vocabSize, vocabSize+1, …` 的递增 id。
4. **融合**：prefill 时 [embeddingLookupWithImageInsertion](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/preprocess/embeddingPreprocessor.cpp#L73-L79) 把这些 id 替换成 `mOutputEmbedding` 的对应行，文本 token 仍查词表，拼成最终 `inputsEmbeds` 送进 LLM。

**需要观察的现象**：图像在 LLM 主干看来，只是输入 embedding 序列里一段「非词表来源」的向量；它既不需要 LLM 改结构，也不需要新的输入端口。

**待本地验证**：可在 `embeddingLookupWithImageInsertion` 加日志，打印遇到 `id≥vocabSize` 的次数，应等于该请求的图像 token 总数。

#### 4.4.5 小练习与答案

**练习 1**：为什么主循环要「按请求是否含多模态数据」而非「runner 是否存在」来决定取不取 embedding？
**答案**：runner 一旦构造就常驻，其 `mOutputEmbedding` 会保留上一轮的值。若纯文本请求误取，会把上一张图的向量错误插进当前序列。故必须按本次请求的实际数据 gating。

**练习 2**：图像 token 的 id 范围与词表会冲突吗？
**答案**：不会。词表只覆盖 `[0, vocabSize)`，而 `textPreprocess` 明确从 `vocabSize` 起编号图像 token，二者区间不重叠。这正是「id≥vocabSize 当视觉索引」约定成立的前提。

---

## 5. 综合实践

**任务**：用一张图，对比 Qwen-VL 与 InternVL 两种视觉编码器在「图像 token 数」上的约束差异，并解释其与 `visual_build` profile 的关系。

1. **准备**：阅读 [multimodalRunner::create](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/multimodalRunner.cpp#L117-L204)，确认 `qwen3_vl` 与 `internvl` 分别落到哪个子类。
2. **Qwen-VL 路**：给定 `patchSize=14, mergeSize=2, minImageTokens=256, maxImageTokens=1280`，对一张 `H=W=784` 的图，按 [getResizedImageSize](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/qwenViTRunner.cpp#L384-L430) 与 [computeVisionSpans](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/qwenViTRunner.cpp#L285-L311) 推算送进 LLM 的图像 token 数（应为 `(784/28)²=28²=784` 的 1/4 = 196，落在 [256,1280] 内需 resize，请实际算出合法的 resize 尺寸）。
3. **InternVL 路**：对同一张图，按 [internViTRunner 的块模型](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/internViTRunner.cpp#L162-L214) 与 [构建器的 256 约束](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/visualBuilder.cpp#L417-L430)，说明其图像 token 数只能是 256 的倍数，并算出若块数为 4（含缩略图）时是 1024。
4. **结论**：写一段话说明——Qwen-VL 的 token 数由动态网格决定（连续可变），InternVL 的 token 数由块数决定（离散、步长 256），这一差异如何在 `visual_build` 的 `--minImageTokens/--maxImageTokens` 上体现（InternVL/Phi4 必须被 256 整除，Qwen 不必）。

> 若无 GPU：本实践以源码推算为主，第 2 步的精确 resize 尺寸可标注「待本地验证」。

## 6. 本讲小结

- `MultimodalRunner` 是十几种视觉/音频编码器的统一抽象，对外只暴露 `preprocess / infer / getOutputEmbedding` 三件套；`create()` 工厂按 `config.json` 的 `model_type` 实例化子类。
- **Qwen-VL 家族**用动态分辨率 + `VisionSpan` 双视图（ViT 视图 vs LLM 视图，由 `mergeSize` 换算）+ MRoPE（T/H/W 三维位置）+ cu_seqlens 处理任意尺寸图与视频，并按 spans 几何做缓存复用。
- **InternVL / Phi4-MM**用固定块模型，每块硬编码 256 个 token，因此图像 token 数必须是 256 的倍数；多块图额外附一张缩略图。
- 两类编码器都用**标准 RoPE**（InternVL）或 **MRoPE**（Qwen）的区别，决定了 `preprocess` 是否需要写 `mropeCosSinOut`。
- 图文融合靠「图像 token id ≥ vocabSize」的约定：`textPreprocess` 写入递增 id，`embeddingLookupWithImageInsertion` 在 prefill 时把它们替换成 ViT 输出行，LLM 主干对图像无感。
- 视觉引擎用 user-managed 上下文内存，与 LLM 执行器复用同一块 `mSharedExecContextMemory`。

## 7. 下一步学习建议

- 下一讲 **u6-l2（音频与 Omni 流水线）** 会把 `audioRunner`、`code2WavRunner`、`qwen3OmniTTSRuntime` 拼进来，建议先复习本讲的「id≥vocabSize 融合」约定，因为 Omni 的 thinker/talker 也依赖它。
- 想深入 Qwen3-VL 子类如何扩展 `QwenViTRunner`（窗口注意力、fast position embedding、deepstack 特征），可阅读 [cpp/multimodal/qwen3vlViTRunner.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/multimodal/qwen3vlViTRunner.cpp) 及其重写的 `validateExtraConfig/allocateExtraBuffers/buildExtraInputs` 钩子。
- 想理解 `embeddingLookupWithImageInsertion` 的 GPU 实现细节，可阅读 [cpp/kernels/embeddingKernels/embeddingKernels.cu](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/kernels/embeddingKernels/embeddingKernels.cu)。
- 视觉引擎如何被构建出来（块数 profile、256 校验）已在 [cpp/builder/visualBuilder.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/visualBuilder.cpp) 体现，可对照 u4 单元复习。
