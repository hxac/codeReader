# 构建器架构与八阶段流程

> 本讲属于「C++ 引擎构建器」单元（u4）的第一篇。前置讲义 u1-l5 已经带你在命令行层面把 `export → build → inference` 三步跑通。本讲我们打开黑盒，看 `build` 这一步在 C++ 内部到底做了什么。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚「引擎构建器（Engine Builder）」在三段式流水线中的确切位置，以及它把什么转成什么。
- 记住构建器内部的**八阶段流程**，并能在 `llmBuilder.cpp` 里逐一定位每个阶段对应的函数或代码段。
- 理解为什么 ONNX 模型和 engine **不能跨 TensorRT / Edge-LLM 版本移植**，并在源码里找到这条约束的依据。
- 区分 `LLMBuilder`（编排者）与 `builderUtils`（共用工具函数）的职责边界。

## 2. 前置知识

在进入源码前，先统一几个术语。它们在前面讲义里零散出现过，这里集中复习：

- **检查点（checkpoint）**：HuggingFace 风格的权重目录，主要是 `.safetensors`。
- **ONNX**：一种跨框架的计算图中间表示。本项目由 Python 导出端（`tensorrt_edgellm`）产出。
- **engine（引擎）**：TensorRT 针对具体 GPU 型号、具体精度、具体输入形状范围编译出的**已优化二进制**，只能被 TensorRT 运行时加载。
- **优化 profile（optimization profile）**：TensorRT 的概念，用来声明某个动态输入维度的 `min / opt / max` 取值范围。引擎只为这些范围内的形状优化。
- **strongly-typed network**：TensorRT 10+ 推荐的网络创建方式，每个张量都带显式数据类型，便于混合精度（FP16/FP8/INT4 共存）。

如果你对「prefill（首 token 计算长序列）」与「decode（逐 token 自回归）」两个推理阶段还不熟，记住一句话即可：prefill 一次吃一长串 token、算量大；decode 每次只吃 1 个 token、迭代次数多。构建器会为这两个阶段分别建一套 profile。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `cpp/builder/llmBuilder.h` | `LLMBuilderConfig` 与 `LLMBuilder` 类的声明，是本讲的「骨架」。 |
| `cpp/builder/llmBuilder.cpp` | `LLMBuilder::build()` 八阶段编排主循环，以及配置解析、profile 设置、文件拷贝等方法。 |
| `cpp/builder/builderUtils.h/.cpp` | 八阶段共用的工具函数：建 builder/network、解析 ONNX、编译序列化、校验 profile 维度等。 |
| `cpp/common/trtUtils.h` | `loadEdgellmPluginLib()`，负责第 1 阶段动态加载插件共享库。 |
| `cpp/common/version.cpp` | `checkVersion()`，负责版本兼容性校验（对应「不可移植」约束）。 |
| `examples/llm/llm_build.cpp` | 命令行入口，把命令行参数填进 `LLMBuilderConfig` 并调用 `build()`。 |
| `docs/.../engine-builder.md` | 官方设计文档，给出八阶段表与流程图，是本讲的「路线图」。 |

## 4. 核心概念与源码讲解

### 4.1 构建器的定位：ONNX → engine 的「第二阶段」

#### 4.1.1 概念说明

Edge-LLM 的流水线是「检查点 → Python 导出(ONNX) → **C++ 引擎构建(engine)** → C++ 运行时(推理)」。构建器就是被加粗的那一段：它**吃进 ONNX，吐出 TensorRT engine**。

为什么需要这一段？因为 ONNX 是「平台无关」的计算图，它描述了算什么，但没说**在具体 GPU 上怎么算最快**。TensorRT 的引擎构建器会针对你当前机器的 GPU 型号、Tensor Cores、显存带宽，把 ONNX 图重新排版、融合算子、挑选最快的 kernel，最后生成一个只能在「这台 GPU + 这个 TensorRT 版本」上跑的二进制。这一步很慢（分钟级），但只做一次；运行时加载 engine 很快。

文档里把它明确写为第二阶段，并画了流程图（HF 检查点 → ONNX → **Engine Builder** → engine → 运行时）：

- 文档把构建器定位为第二阶段：[docs/source/developer_guide/software-design/engine-builder.md:9-L9](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/developer_guide/software-design/engine-builder.md#L9-L9)（"The Engine Builder serves as the **second stage**"）。

构建器有两个并列组件，文档用一张表说清分工：

| 组件 | 职责 |
|------|------|
| **LLM Builder** | 把语言模型 ONNX 编成 engine（标准 LLM、EAGLE3/MTP/DFlash 投机解码、VLM 语言部分、LoRA） |
| **Visual Encoder Builder** | 把视觉编码器 ONNX 编成 engine（多模态用），本讲聚焦 LLM Builder |

- 组件总览表见 [docs/source/developer_guide/software-design/engine-builder.md:163-L167](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/developer_guide/software-design/engine-builder.md#L163-L167)。

#### 4.1.2 核心流程

文档把整个构建流程抽象成八阶段，并且强调**两个 builder 都走同一套八阶段**，只是每阶段处理的内容不同：

```
1. Plugin Loading(插件加载)
        ↓
2. Configuration Parsing(配置解析)
        ↓
3. Network Creation(网络创建)
        ↓
4. Model Type Detection(模型类型检测)
        ↓
5. ONNX Parsing(ONNX 解析)
        ↓
6. Optimization Profile Setup(profile 设置)
        ↓
7. Engine Compilation(引擎编译, 调 TensorRT Builder)
        ↓
8. File Management(文件管理: 拷贝 sidecar)
```

对于投机解码（EAGLE3 / MTP / DFlash），LLM Builder 会把这八阶段**跑两遍**，分别产出 base 与 draft 两个 engine——这点很重要，它解释了为什么 u1-l5 里 `--specBase` 和 `--specDraft` 要分两次调用 `llm_build`：

- 投机解码跑两遍八阶段：[docs/source/developer_guide/software-design/engine-builder.md:152-L152](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/developer_guide/software-design/engine-builder.md#L152-L152)。

#### 4.1.3 源码精读

**为什么 ONNX 与 engine 不能跨版本移植？** 文档在最显眼处给出警告：

- 版本兼容性警告：[docs/source/developer_guide/software-design/engine-builder.md:61-L61](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/developer_guide/software-design/engine-builder.md#L61-L61)（"ONNX models and TensorRT engines are **NOT portable** across different versions..."）。

这条约束在代码里有强制实现。构建的第 2 阶段（配置解析）会读取检查点写入的 `edgellm_version` 字段并做版本比对：

```cpp
// cpp/builder/llmBuilder.cpp:333-334
std::string modelVersion = mModelConfig.value(binding_names::kEdgellmVersion, "");
version::checkVersion(modelVersion);
```

`checkVersion` 的真实逻辑在 `version.cpp`，它做了三件事，正好对应「不可移植」的三个层次：

- `checkVersion` 实现：[cpp/common/version.cpp:73-L119](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/version.cpp#L73-L119)。

  1. 没有版本号 → 打 `LOG_WARNING`；
  2. 版本 < 0.8.0 → 打 `LOG_ERROR` 并判定不再支持（要求重新导出）；
  3. 版本号与运行时 `kRUNTIME_VERSION` 不一致 → 打警告，建议重新导出/重新构建。

也就是说：导出 ONNX 时写下的 Edge-LLM 版本，必须和构建/运行 engine 时的 Edge-LLM 版本一致；再加上 engine 本身绑定 GPU 型号与 TensorRT 版本，这就构成了「升级版本必须重新 export + rebuild」的硬约束。

#### 4.1.4 代码实践

**实践目标**：用源码确认「不可移植」是程序级强制，而非口头约定。

**操作步骤**：

1. 打开 `cpp/common/version.cpp`，定位 `checkVersion`（73 行起）。
2. 打开 `cpp/builder/llmBuilder.cpp`，确认 `parseConfig` 在 333–334 行调用了它。
3. 思考：如果有人把一个 0.7.x 时代导出的 ONNX 喂给当前 builder，会发生什么？

**预期现象**：根据 92–98 行，版本号解析为 `major=0, minor<8`，会触发 `LOG_ERROR`，提示 "Minimum supported version is 0.8.0"，并要求重新导出。

**结果**：待本地验证（需要一份旧版本导出的 ONNX 目录）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 ONNX 已经是「跨框架标准」，Edge-LLM 还要求你用配套版本的 Python 导出端重新导出？
**参考答案**：因为本项目的 ONNX 图里使用了自定义算子域（`trt::` / `trt_edgellm::`，见 u2-l5）以及特定的 I/O 契约（KV cache、Mamba 状态以图输入输出暴露）。这些会随 Edge-LLM 版本演进；`edgellm_version` 字段就是这份契约的版本戳。版本不一致意味着 C++ 解析器可能认不出新的自定义算子或绑定名。

**练习 2**：engine 能否从 A 机器拷到 B 机器直接用？
**参考答案**：通常不能。engine 在第 7 阶段由 TensorRT 针对当前 GPU 型号（SM 架构、Tensor Core 代际）和 TensorRT 版本编译，迁移到不同型号 GPU 或不同 TensorRT 版本会无法反序列化。

---

### 4.2 LLMBuilder：构建器类与 `build()` 八阶段编排主循环

#### 4.2.1 概念说明

`LLMBuilder` 是构建器的**编排者（orchestrator）**。它自己几乎不做底层 TensorRT 调用，而是按八阶段顺序，把工作分派给 `builderUtils` 里的工具函数和自己的成员方法。这一点很像 u2-l6 的 `scripts/export.py` 主循环——「指挥」比「亲自干活」更多。

它的全部状态由构造函数三件套决定：`onnxDir`（ONNX 输入目录）、`engineDir`（engine 输出目录）、`LLMBuilderConfig`（构建参数）。`LLMBuilderConfig` 是个 POD 结构体，关键字段如下（与命令行参数一一对应）：

| 字段 | 默认值 | 含义 |
|------|--------|------|
| `maxInputLen` | 1024 | 最大输入序列长度 |
| `maxBatchSize` | 4 | 最大 batch |
| `maxKVCacheCapacity` | 4096 | KV 缓存容量（序列长度维度） |
| `maxLoraRank` | 0 | LoRA 秩，>0 表示构建 LoRA 版 engine |
| `specBase` / `specDraft` | false | 投机解码 base / draft 角色 |
| `maxVerifyTreeSize` / `maxDraftTreeSize` | 60 | base 验证 / draft 提议的最大 token 数 |

这些字段定义在头文件里：

- `LLMBuilderConfig` 定义：[cpp/builder/llmBuilder.h:37-L47](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.h#L37-L47)。

而命令行入口 `llm_build.cpp` 做的事就是「解析参数 → 填 `LLMBuilderConfig` → 构造 `LLMBuilder` → 调 `build()`」：

```cpp
// examples/llm/llm_build.cpp:228-245（节选）
builder::LLMBuilderConfig config;
config.maxInputLen       = args.maxInputLen;
config.maxBatchSize      = args.maxBatchSize;
config.maxLoraRank       = args.maxLoraRank;
config.specDraft         = args.specDraft;
config.specBase          = args.specBase;
// ...
builder::LLMBuilder llmBuilder(args.onnxDir, args.engineDir, config);
if (!llmBuilder.build()) { /* 失败 */ }
```

#### 4.2.2 核心流程

`build()` 是一条近乎线性的八阶段流水线，每一步失败都立即 `return false`。把它的源码结构画出来就是：

```
build()
 ├─ [阶段1] loadEdgellmPluginLib()        # 加载插件共享库
 ├─ [阶段2] parseConfig()                  # 读 config.json, 校验版本与角色
 ├─ [阶段3] createBuilderAndNetwork()      # 建 TRT builder + strongly-typed network
 ├─ [阶段4] 选 model.onnx / lora_model.onnx # 模型类型检测
 ├─ [阶段5] parseOnnxModel()               # ONNX 解析进网络
 ├─ [阶段6] createBuilderConfig()          # 建 builder config
 │         setupLLMOptimizationProfiles()  # 设置双 profile(context/generation)
 ├─ [阶段7] 建目录 + 选 engine 文件名
 │         buildAndSerializeEngine()       # 编译并落盘 .engine
 └─ [阶段8] copyConfig/copyTokenizerFiles/ # 拷贝 sidecar
            copyEagleFiles/copyVocabMappingFiles/
            copyEmbeddingFile/copyExternalWeightFiles
```

注意一个细节：文档里的「八阶段」是**逻辑划分**，而源码里阶段 6 在 `build()` 里拆成了「建 config」+「设 profile」两步，阶段 4 的「模型类型检测」其实贯穿在 `parseConfig()`（读 `spec_decode_type`/`engine_role`）和 `build()` 里两处选择（选 ONNX 文件名、选 engine 文件名）。这是阅读时需要把「文档逻辑」与「代码物理位置」对齐的地方。

#### 4.2.3 源码精读

`build()` 的完整骨架（去掉日志与失败检查后的主线）：

- `build()` 主循环：[cpp/builder/llmBuilder.cpp:166-L322](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L166-L322)。

逐阶段在 `build()` 内的调用点：

```cpp
// cpp/builder/llmBuilder.cpp:180       [阶段1]
auto pluginHandles = loadEdgellmPluginLib();

// cpp/builder/llmBuilder.cpp:183       [阶段2]
if (!parseConfig()) { return false; }

// cpp/builder/llmBuilder.cpp:189       [阶段3]
auto [builder, network] = createBuilderAndNetwork();

// cpp/builder/llmBuilder.cpp:196-206   [阶段4] 模型类型检测: 选 ONNX 文件
if (mBuilderConfig.maxLoraRank > 0) {
    onnxFilePath = (mOnnxDir / "lora_model.onnx").string();
} else {
    onnxFilePath = (mOnnxDir / "model.onnx").string();
}

// cpp/builder/llmBuilder.cpp:209       [阶段5]
auto parser = parseOnnxModel(network.get(), onnxFilePath);

// cpp/builder/llmBuilder.cpp:222, 237  [阶段6]
auto config = createBuilderConfig(builder.get());
if (!setupLLMOptimizationProfiles(*builder, *config, *network)) { return false; }

// cpp/builder/llmBuilder.cpp:270       [阶段7]
if (!buildAndSerializeEngine(builder.get(), network.get(), config.get(), engineFilePath)) { return false; }

// cpp/builder/llmBuilder.cpp:291-319   [阶段8] 文件管理
if (!copyConfig())          { return false; }
if (!copyTokenizerFiles())  { return false; }
if (!copyEagleFiles())      { return false; }
if (!copyVocabMappingFiles()){ return false; }
if (!copyEmbeddingFile())   { return false; }
if (!copyExternalWeightFiles()) { return false; }
```

注意阶段 7 还包含「选 engine 文件名」的小逻辑——它根据角色产出 `llm.engine` / `spec_base.engine` / `spec_draft.engine`：

- engine 文件名选择：[cpp/builder/llmBuilder.cpp:254-L266](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L254-L266)。

这也解释了 u1-l5 里 vanilla 构建产出 `llm.engine`、而投机解码分别产出 `spec_base.engine` / `spec_draft.engine` 的来源。

#### 4.2.4 代码实践

**实践目标**：在 `build()` 里数清楚八阶段的物理调用点。

**操作步骤**：

1. 打开 `cpp/builder/llmBuilder.cpp`，跳到 166 行的 `build()`。
2. 准备一张八行表格（阶段号 / 阶段名 / 对应函数或代码行）。
3. 从上往下读，把每个阶段对应的「函数调用」或「代码段」填进去。

**预期结果**：你应该得到类似 4.2.2 流程图里的清单。注意阶段 4（模型类型检测）在 `build()` 里**没有独立函数**，而是 `maxLoraRank` 三元判断 + `specBase/specDraft` 判断两处内联代码；这是文档八阶段与代码在「一个阶段 = 一个函数」假设上的唯一明显出入。

#### 4.2.5 小练习与答案

**练习 1**：`build()` 里为什么把「创建 engine 目录」（243–251 行）放在 profile 设置之后、编译之前，而不是放在 `build()` 一开头？
**参考答案**：因为 profile 设置失败会提前 `return false`，此时还没必要创建输出目录，避免留下空目录。这是一种「尽量晚地产生副作用」的写法。

**练习 2**：如果把 `LLMBuilderConfig` 里的 `profilingDetailed` 设为 true，会影响哪个阶段？
**参考答案**：影响阶段 6/7 的产出质量。`build()` 在 228–232 行检测到该标志后，会把 `IBuilderConfig` 的 `ProfilingVerbosity` 设为 `kDETAILED`，使最终 engine 的层信息里带上 ONNX 算子名，供 DLSim 等分析工具使用（见 `llm_build.cpp` 的 `--profilingDetailed` 说明）。

---

### 4.3 八阶段共用的工具函数与 profile 分发（builderUtils）

#### 4.3.1 概念说明

`builderUtils` 是构建器的「工具箱」：八阶段里凡是「直接调用 TensorRT C API」的脏活累活，都被抽成这里的无状态函数，供 `LLMBuilder`、`VisualEncoderBuilder`、`AudioBuilder`、`ActionBuilder` 复用（你可以在那几个 builder 里看到对 `loadEdgellmPluginLib()` 的相同调用）。

本模块要讲清两件事：

1. **共用工具函数**分别承担哪个阶段。这是本讲的「函数清单」核心。
2. **阶段 6 的 profile 分发**如何按模型类型（vanilla / spec / dflash / gemma4-mtp）走不同分支——它把「一个 builder，多种模型」的设计具象化了。

#### 4.3.2 核心流程

先给出八阶段 → 函数的映射表（本讲最重要的产出之一）：

| 阶段 | 调用方（在 `build()` 的行） | 实际执行函数（定义位置） |
|------|------------------------------|----------------------------|
| 1. 插件加载 | `loadEdgellmPluginLib()` (180) | `cpp/common/trtUtils.h:60` |
| 2. 配置解析 | `parseConfig()` (183) | `llmBuilder.cpp:324` |
| 3. 网络创建 | `createBuilderAndNetwork()` (189) | `builderUtils.cpp:219` |
| 4. 模型类型检测 | 内联 (196–206, 254–266) | `llmBuilder.cpp` + `parseConfig` 内的 `spec_decode_type` 解析 |
| 5. ONNX 解析 | `parseOnnxModel()` (209) | `builderUtils.cpp:266` |
| 6. profile 设置 | `createBuilderConfig()` (222) + `setupLLMOptimizationProfiles()` (237) | `builderUtils.cpp:241` + `llmBuilder.cpp:489` |
| 7. 引擎编译 | `buildAndSerializeEngine()` (270) | `builderUtils.cpp:294` |
| 8. 文件管理 | `copy*()` 系列 (291–319) | `llmBuilder.cpp:1325/1442/1481/1503/1536/1660` |

阶段 6 的 profile 分发逻辑（`setupLLMOptimizationProfiles`）是一棵决策树：先看是不是 DFlash draft、再看是不是 Gemma4-MTP draft，否则走「通用 profile + 按角色叠加 vanilla/spec/LoRA」的主路径：

```
setupLLMOptimizationProfiles()
 ├─ 是 dflash draft?        → setupDFlashDraftProfiles()          (提前返回)
 ├─ 是 gemma4_mtp draft?    → setupGemma4MTPDraftProfiles()       (提前返回)
 └─ 主路径:
      setupCommonProfiles()        # 通用: KV cache / recurrent / conv 状态
      setupRopeProfiles()          # RoPE 缓存(单/双)
      if specBase||specDraft:  setupSpecDecodeProfiles()
      else:                    setupVanillaProfiles()
      [按需叠加 mtp/dflash base 的中间状态、PLE、Deepstack、CodePredictor lm_head]
      if maxLoraRank>0:        setupLoraProfiles()
      → addOptimizationProfile(contextProfile)
      → addOptimizationProfile(generationProfile)
```

#### 4.3.3 源码精读

**阶段 1 — 插件加载**。`loadEdgellmPluginLib` 用 `dlopen` 加载共享库 `libNvInfer_edgellm_plugin.so`，路径优先取环境变量 `EDGELLM_PLUGIN_PATH`，否则默认 `build/libNvInfer_edgellm_plugin.so`；加载后调用库导出的 `initEdgellmPlugins` 完成插件注册：

- 插件加载实现：[cpp/common/trtUtils.h:60-L92](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/trtUtils.h#L60-L92)。

  这里有两点呼应 u1-l3：一是用 `RTLD_NODELETE` 让库常驻（因为 engine 运行期仍会回调插件代码）；二是这解释了为什么插件库必须是 **SHARED** 而非 static。

**阶段 3 — 网络创建**。用 `createInferBuilder` + `createNetworkV2(kSTRONGLY_TYPED)` 建出 strongly-typed 网络，为混合精度铺路：

```cpp
// cpp/builder/builderUtils.cpp:219-239（节选）
auto builder = std::unique_ptr<nvinfer1::IBuilder>(nvinfer1::createInferBuilder(gLogger));
auto const stronglyTyped = 1U << static_cast<uint32_t>(
    nvinfer1::NetworkDefinitionCreationFlag::kSTRONGLY_TYPED);
auto network = std::unique_ptr<nvinfer1::INetworkDefinition>(
    builder->createNetworkV2(stronglyTyped));
```

- 网络创建实现：[cpp/builder/builderUtils.cpp:219-L239](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/builderUtils.cpp#L219-L239)。

**阶段 5 — ONNX 解析**。用 `nvonnxparser::createParser` 把 ONNX 文件解析进上一步建好的网络：

- ONNX 解析实现：[cpp/builder/builderUtils.cpp:266-L292](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/builderUtils.cpp#L266-L292)。

**阶段 6 — builder config 与 profile**。`createBuilderConfig` 打开 `kMONITOR_MEMORY` 等构建期开关；`setupLLMOptimizationProfiles` 则是分发树本身：

- builder config 创建：[cpp/builder/builderUtils.cpp:241-L264](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/builderUtils.cpp#L241-L264)。
- profile 分发主函数：[cpp/builder/llmBuilder.cpp:489-L576](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L489-L576)。

  其中「双 profile」是核心：函数一开头就建了 `contextProfile`（prefill）与 `generationProfile`（decode）两个，最后都 `addOptimizationProfile` 进 config。这正是文档「Dual-Phase Optimization」的代码实现（文档 218–230 行）。

**阶段 7 — 引擎编译**。这一步直接调用 TensorRT 的 `builder->buildSerializedNetwork(...)`，然后把字节流写进 `.engine` 文件：

```cpp
// cpp/builder/builderUtils.cpp:303-304
auto engine = std::unique_ptr<nvinfer1::IHostMemory>(
    builder->buildSerializedNetwork(*network, *config));
```

- 编译与序列化实现：[cpp/builder/builderUtils.cpp:294-L330](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/builderUtils.cpp#L294-L330)。

  注意这正是文档阶段表里第 7 步标注的 `buildSerializedNetwork()` 调用点——文档与代码完全对得上。

**阶段 8 — 文件管理**。六个 `copy*` 方法把 sidecar 拷进 engine 目录。其中 `copyConfig` 最关键：它把原始 `config.json` 加上 `builder_config` 字段（即 `LLMBuilderConfig::toJson()`），并按角色选不同文件名：

- `copyConfig`：[cpp/builder/llmBuilder.cpp:1325-L1345](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L1325-L1345)（产出 `config.json` / `base_config.json` / `draft_config.json`）。
- 底层写盘 `saveConfigWithBuilderInfo`：[cpp/builder/builderUtils.cpp:354-L382](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/builderUtils.cpp#L354-L382)（把 `builder_config` 合并进模型 config 落盘）。

#### 4.3.4 代码实践

**实践目标**：把文档的八阶段表「翻译」成可执行的函数清单（这正是本讲的核心实践任务）。

**操作步骤**：

1. 打开文档的八阶段表：[docs/source/developer_guide/software-design/engine-builder.md:175-L184](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/developer_guide/software-design/engine-builder.md#L175-L184)。
2. 对照本讲 4.3.2 的映射表，逐阶段在源码里验证每个函数确实存在、行号对得上。
3. 特别验证：阶段 4「模型类型检测」在文档里被描述为「`maxLoraRank > 0` 选 `lora_model.onnx`，否则 `model.onnx`」——在 `llmBuilder.cpp:196-206` 找到这段代码确认。

**预期结果**：得到一张「阶段号 → 函数名 → 定义文件:行号」的完整清单（即 4.3.2 那张表）。你会发现除阶段 4 外，其余阶段都精确对应一个命名函数。

#### 4.3.5 小练习与答案

**练习 1**：`builderUtils` 里的 `setOptimizationProfile`（85 行）做了什么校验？为什么需要它？
**参考答案**：它调用 `checkOptimizationProfileDims` 校验每个维度的 `min <= opt <= max`，且三个 profile 的维数一致，然后才把 min/opt/max 三套维度写进 `IOptimizationProfile`。TensorRT 要求动态形状必须给出合法的三点范围，否则编译会失败，所以这个校验是把错误前置到 profile 设置阶段而非编译阶段。

**练习 2**：为什么阶段 6 的 `setupLLMOptimizationProfiles` 要为 DFlash / Gemma4-MTP draft **提前 return**，而不和 vanilla 走同一条主路径？
**参考答案**：这两种 draft 模型的输入张量集合与普通 LLM 差异很大（如 DFlash draft 消费 `dflash_target_hidden_concat`、`dflash_delta_lengths`；Gemma4 assistant 读 base embedding 与 target hidden/KV），它们不需要 KV cache / RoPE 等通用 profile。所以分发树让它们走独立分支后立即返回，避免叠加不相关的 profile 设置。

## 5. 综合实践

**任务**：做一份「八阶段 × 两种角色」的构建手册。

1. **读图**：打开 [docs/.../engine-builder.md](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/developer_guide/software-design/engine-builder.md) 的八阶段流程图（69–148 行）与八阶段表（175–184 行）。
2. **定位**：在 `cpp/builder/llmBuilder.cpp` 的 `build()` 里，给每个阶段写出对应的函数名或代码行区间（见 4.3.2 表）。
3. **对比角色**：设想你在构建一个 EAGLE3 投机解码模型，分别用 `--specBase` 和 `--specDraft` 调两次 `llm_build`。回答：
   - 两次构建各走几个阶段？（提示：文档 152 行说「跑两遍」。）
   - 两次各自的阶段 4 选了哪个 ONNX 文件？阶段 7 各产出哪个 engine 文件？（提示：看 `llmBuilder.cpp:196-206` 与 `254-266`。）
   - 两次的阶段 6 分别走了 `setupLLMOptimizationProfiles` 的哪个分支？
4. **产出**：一张 Markdown 表，列「阶段 | base 构建时的函数/产物 | draft 构建时的函数/产物」。

**预期结果**：你会清楚看到，base 与 draft 共用同一套八阶段骨架，差异只在阶段 4（文件选择）、阶段 6（profile 分支）和阶段 7（engine 文件名）。这正是「同一 builder，不同配置跑两遍」设计的体现。

> 若本机无 GPU，步骤 1–4 仍可纯靠源码阅读完成；步骤里的命令无需真正执行。

## 6. 本讲小结

- 构建器是三段式流水线的**第二阶段**，负责 ONNX → TensorRT engine 的转换，本质是「针对具体 GPU 做算子融合与 kernel 选优」。
- `LLMBuilder::build()` 是一条**八阶段线性流水线**：插件加载 → 配置解析 → 网络创建 → 模型类型检测 → ONNX 解析 → profile 设置 → 引擎编译 → 文件管理。
- 八阶段中除「模型类型检测」是内联判断外，其余都精确对应 `builderUtils` 或 `llmBuilder.cpp` 里的命名函数（见 4.3.2 映射表）。
- 阶段 6 的双 profile（context=prefill / generation=decode）是 LLM 推理高效的关键；`setupLLMOptimizationProfiles` 用一棵决策树按模型类型分发。
- 「不可移植」是程序级强制：`version::checkVersion` 比对 `edgellm_version` 与运行时版本，engine 又绑定 GPU 型号与 TensorRT 版本——升级必须重新 export + rebuild。
- 投机解码会让八阶段**跑两遍**（base / draft），共用同一套骨架，仅文件名与 profile 分支不同。

## 7. 下一步学习建议

- 想深入阶段 6 的双 profile 与模型类型分发，继续学 **u4-l2（双阶段优化 profile 与模型类型分发）**，那里会逐个拆 `setupVanillaProfiles` / `setupSpecDecodeProfiles` / `setupVLMProfiles` / `setupLoraProfiles`。
- 想从「读懂」走向「会用」构建器 CLI，学 **u4-l3（使用构建器 CLI）**，把 `--specBase/--specDraft`、`--maxKVCacheCapacity` 等参数跟本讲的阶段对应起来。
- 如果你对阶段 6 里出现的 recurrent / conv 状态（Mamba/GDN）好奇，可以先跳到 u5-l5（KV 缓存与混合缓存管理）了解它们在运行时的用途，再回来看构建期如何为它们设 profile。
