# 使用构建器 CLI（llm_build / visual_build）

## 1. 本讲目标

在 [u4-l1](u4-l1-builder-architecture-eight-stages.md) 里我们已经从源码层打开了 `LLMBuilder::build()` 的八阶段黑盒，在 [u4-l2](u4-l2-dual-phase-profiles-model-dispatch.md) 里又看清了双阶段优化 profile 与模型类型分发。但读者真正坐在终端前、要把一张 ONNX 编成 engine 时，面对的是两个命令行程序：`llm_build` 与 `visual_build`。本讲的目标就是教会你：

1. 读懂 `llm_build` 与 `visual_build` 的全部命令行参数与默认值。
2. 理解「命令行薄封装」的设计：参数如何被解析、校验、灌进 `LLMBuilderConfig` / `VisualBuilderConfig`，再交给 builder。
3. 掌握 `--maxInputLen` 与 `--maxKVCacheCapacity` 之间的精确关系，以及它们如何决定引擎的动态形状与显存预算。
4. 能独立组装三类命令：标准 LLM 构建、EAGLE base+draft 构建（共用同一 engineDir）、VLM 的 LLM+视觉构建。

学完后，你应该能针对任意一个已导出的 ONNX 目录，凭直觉写出正确的 build 命令，并解释每个参数对最终 engine 的影响。

## 2. 前置知识

阅读本讲前，请先建立以下认知（详见依赖讲义）：

- **三段式流水线**：检查点 → Python 导出（ONNX）→ C++ 引擎构建（engine）→ C++ 运行时（推理）。`llm_build`/`visual_build` 处于第二阶段，输入是 ONNX 目录，输出是 `.engine`（见 [u1-l2](u1-l2-repo-layout-and-pipeline.md)）。
- **engine 不可移植**：engine 绑定具体 GPU 型号与 TensorRT/Edge-LLM 版本，升级须重新 export + rebuild（见 [u4-l1](u4-l1-builder-architecture-eight-stages.md)）。
- **双阶段 profile**：LLM builder 一律创建 context（prefill）与 generation（decode）两个 profile，让长序列 prefill 与单 token decode 各自命中优化的 kernel（见 [u4-l2](u4-l2-dual-phase-profiles-model-dispatch.md)）。
- **优化 profile**：TensorRT 用 min/opt/max 三元组描述动态维度的运行时变化范围。

此外需要一点 C++ 基础：`getopt_long` 是 POSIX 的长选项解析库，`struct option` 描述「选项名 → ID」映射。本讲会用到它，但不展开语法细节。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [examples/llm/llm_build.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/llm_build.cpp) | 语言引擎构建 CLI，把命令行参数解析成 `LLMBuilderConfig` 后调用 `LLMBuilder::build()` |
| [examples/multimodal/visual_build.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/multimodal/visual_build.cpp) | 视觉编码器引擎构建 CLI，把参数解析成 `VisualBuilderConfig` 后调用 `VisualBuilder::build()` |
| [cpp/builder/llmBuilder.h](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.h) | `LLMBuilderConfig` 结构体定义，是 CLI 与 builder 之间的契约 |
| [cpp/builder/visualBuilder.h](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/visualBuilder.h) | `VisualBuilderConfig` 结构体定义 |
| [cpp/builder/llmBuilder.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp) | `LLMBuilder::build()` 实现，本讲引用其中的引擎文件命名与 profile 设置 |
| [examples/llm/CMakeLists.txt](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/CMakeLists.txt) / [examples/multimodal/CMakeLists.txt](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/multimodal/CMakeLists.txt) | 声明 `llm_build`、`visual_build` 可执行目标 |

这两个 CLI 都是「薄封装（thin wrapper）」：自身几乎不含业务逻辑，只负责「解析参数 → 校验 → 灌配置 → 调 build」。真正的八阶段构建逻辑全在 builder 里（[u4-l1](u4-l1-builder-architecture-eight-stages.md)）。

## 4. 核心概念与源码讲解

### 4.1 两个 CLI 的共同骨架：从参数到 builder.build()

#### 4.1.1 概念说明

`llm_build` 与 `visual_build` 的源码结构高度对称，都遵循同一条主线：

> **解析命令行 → 校验输入目录 → 填充 Config 结构体 → 构造 Builder → 调用 `build()`**

这种「薄封装」是有意为之：CLI 不该知道 ONNX 怎么解析、kernel 怎么选——这些是 builder 的职责。CLI 只是把人类友好的命令行参数，翻译成 builder 能消费的强类型 `Config` 结构体。这样一来，builder 既能被 CLI 调用，也能被未来任何 C++ 程序、Python pybind 绑定直接调用，逻辑不重复。

两个可执行目标由 CMake 声明，分别链接 `edgellmBuilder` 库：

- [examples/llm/CMakeLists.txt:13-15](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/CMakeLists.txt#L13-L15)：`add_executable(llm_build ...)` 并链接 `edgellmBuilder`。
- [examples/multimodal/CMakeLists.txt:11-13](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/multimodal/CMakeLists.txt#L11-L13)：`add_executable(visual_build ...)` 并链接 `edgellmBuilder`。

构建完成后，二进制分别位于 `build/examples/llm/llm_build` 与 `build/examples/multimodal/visual_build`（见 [u1-l3](u1-l3-build-system-and-dependencies.md) 的 CMake 构建说明）。

#### 4.1.2 核心流程

两个 `main()` 的执行流程几乎一模一样，可用同一段伪代码描述：

```text
main(argc, argv):
    1. parseXxxArgs(args, argc, argv)     # getopt_long 解析长选项到 args 结构体
    2. if 解析失败 or args.help: 打印用法, 退出
    3. 根据 args.debug 设置日志级别 (kVERBOSE 或 kINFO)
    4. 校验 onnxDir/config.json 是否存在, 不存在则报错退出
    5. [仅 visual_build] 校验图像 token 三元组的取值约束
    6. 用 args 填充 XxxBuilderConfig 结构体
    7. 构造 XxxBuilder(onnxDir, engineDir, config)
    8. 调用 builder.build(); 失败则退出
    9. 打印成功日志
```

第 4 步尤其值得注意：两个 CLI 都会显式检查 `onnxDir + "/config.json"` 是否存在（[llm_build.cpp:217-225](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/llm_build.cpp#L217-L225) 与 [visual_build.cpp:166-174](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/multimodal/visual_build.cpp#L166-L174)）。这印证了 u2-l6 的结论：`config.json` 是 Python 导出端产出的、被 C++ 端消费的 sidecar 契约文件——builder 后续会从它读出 `layer_types`、`edgellm_version` 等元数据。如果 `--onnxDir` 指错了，这里会第一时间报错，而不是等解析 ONNX 时才崩。

#### 4.1.3 源码精读

**参数解析靠 `getopt_long` + 选项表。** `llm_build` 用一个枚举给每个选项分配唯一 ID，再用 `struct option` 数组把「命令行名 → ID」登记起来：

[examples/llm/llm_build.cpp:32-47](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/llm_build.cpp#L32-L47) 定义选项 ID 枚举（`ONNX_DIR=702`、`ENGINE_DIR=703` 等），ID 从 701 起编号。

[examples/llm/llm_build.cpp:103-117](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/llm_build.cpp#L103-L117) 是选项表本体。注意第 112、114 行：`--eagleDraft`/`--eagleBase` 是 `--specDraft`/`--specBase` 的**已弃用别名**（deprecated alias），映射到同一个 ID，方便老脚本平滑迁移。

**默认值在 args 结构体的成员初始化里写死。** 这是理解「不传参数时引擎长什么样」的入口：

[examples/llm/llm_build.cpp:49-64](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/llm_build.cpp#L49-L64)：`LLMBuildArgs` 的默认值——`maxInputLen=1024`、`maxKVCacheCapacity=4096`、`maxBatchSize=4`、`maxLoraRank=0`、`specDraft/specBase=false`、`maxVerifyTreeSize=maxDraftTreeSize=60`。

**把 args 灌进 Config 并调用 build。** `main` 的尾部一气呵成：

[examples/llm/llm_build.cpp:228-245](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/llm_build.cpp#L228-L245) 创建 `LLMBuilderConfig`，逐字段赋值，构造 `LLMBuilder(onnxDir, engineDir, config)`，然后调 `build()`。`visual_build` 的对应段在 [visual_build.cpp:186-200](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/multimodal/visual_build.cpp#L186-L200)。

#### 4.1.4 代码实践

**实践目标**：在不跑构建的前提下，验证两个 CLI 是同构的「薄封装」。

**操作步骤**：

1. 打开 `examples/llm/llm_build.cpp` 与 `examples/multimodal/visual_build.cpp` 并排对比。
2. 找到各自的 `main()`、`parseXxxArgs()`、`printUsage()` 三个函数。
3. 把两者的 `main` 流程逐行对齐，标出「完全相同」「仅参数名不同」「一方独有」三种行。

**需要观察的现象**：

- 两者都先 `parseArgs`，再校验 `config.json`，再填充 Config，再 `build()`。
- `visual_build` 比 `llm_build` 多了一段图像 token 三元组的校验（见 4.3）。
- `llm_build` 比 `visual_build` 多了 `--specBase/--specDraft/--maxLoraRank` 等语言侧特有参数。

**预期结果**：你会得到一张「两边对应关系表」，证明除参数集合不同外，骨架完全一致——这正是「薄封装」复用同一 builder 设计的体现。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `--eagleDraft` 和 `--specDraft` 能共存而不冲突？
**答案**：它们在选项表里映射到**同一个枚举 ID**（`SPEC_DRAFT`），`getopt_long` 解析到任一名字都把 `args.specDraft` 置 true，效果完全等价；`--eagleDraft` 只是历史遗留别名（[llm_build.cpp:112](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/llm_build.cpp#L112)）。

**练习 2**：如果 `--onnxDir` 指向一个没有 `config.json` 的目录，会报什么错、在哪一行？
**答案**：在 `config.json` 存在性校验处报 `config.json not found in onnx directory: <dir>` 并 `return EXIT_FAILURE`（[llm_build.cpp:217-224](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/llm/llm_build.cpp#L217-L224)），不会进入 ONNX 解析阶段。

---

### 4.2 llm_build：参数全解与引擎命名

#### 4.2.1 概念说明

`llm_build` 把一张语言模型 ONNX 编成 `.engine`。它的参数可分为四组：

1. **路径**：`--onnxDir`（输入，必填）、`--engineDir`（输出，必填）。
2. **容量与形状**：`--maxInputLen`、`--maxKVCacheCapacity`、`--maxBatchSize`。这三个共同决定 engine 的动态形状范围与 KV 缓存显存预算。
3. **角色**：`--specBase` / `--specDraft` 把这次构建标记为投机解码的 base 或 draft；`--maxLoraRank` 启用 LoRA 支持。这三者会改变阶段 4 的 ONNX 选择与阶段 6 的 profile 分发（见 [u4-l2](u4-l2-dual-phase-profiles-model-dispatch.md)）。
4. **投机解码树大小**：`--maxVerifyTreeSize`（base 验证时的 token 上限）、`--maxDraftTreeSize`（draft 提议时的 token 上限）。

#### 4.2.2 核心流程：maxInputLen 与 maxKVCacheCapacity 的精确关系

这是本讲最容易被误解、也最重要的概念。两者都和「序列长度」有关，但作用在不同的引擎维度上。它们直接进入阶段 6 的优化 profile 设置：

- **`--maxInputLen`（默认 1024）**：约束 **prefill（context）profile 的输入 embedding 维度**——也就是一次能喂进多少个 prompt token。看 builder 里 context profile 的输入形状：

  [cpp/builder/llmBuilder.cpp:651-653](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L651-L653)：context profile 的 `inputs_embeds`，opt = `{maxBatchSize, maxInputLen/2, hidden}`，max = `{maxBatchSize, maxInputLen, hidden}`。

  而 generation（decode）profile 的同一输入被钉死为单 token：

  [cpp/builder/llmBuilder.cpp:654-655](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L654-L655)：generation profile 的 `inputs_embeds`，min/opt/max 都是 `{maxBatchSize, 1, hidden}`——这正是 [u4-l2](u4-l2-dual-phase-profiles-model-dispatch.md) 讲的「decode 每步只处理 1 个 token」。

- **`--maxKVCacheCapacity`（默认 4096）**：约束 **RoPE 序列位置维度 / KV 缓存能容纳的最大序列长度**，即「上下文窗口」大小。它出现在 RoPE cos/sin 的 profile 里，且 **context 与 generation 两个 profile 用的是同一个值**：

  [cpp/builder/llmBuilder.cpp:616-624](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L616-L624)：RoPE profile，min=`{1, maxKVCacheCapacity, rotaryDim}`，max=`{maxBatchSize, maxKVCacheCapacity, rotaryDim}`，两个 profile 完全相同。

由此可以推出两者的关系：

\[ \text{可生成的最大 token 数} \;\approx\; \text{maxKVCacheCapacity} - \text{实际 input 长度} \]

且必须满足约束：

\[ \text{maxKVCacheCapacity} \;\geq\; \text{maxInputLen} \]

换句话说：

- `maxInputLen` 是「prefill 一步最多喂多少 prompt」。
- `maxKVCacheCapacity` 是「整条序列（prompt + 生成）最多能有多长」，等于 KV 缓存与 RoPE 能寻址的位置上限。
- 生成余量 = `maxKVCacheCapacity − 实际 prompt 长度`。默认配置 `maxInputLen=1024`、`maxKVCacheCapacity=4096` 意味着「最多 1024 token 的 prompt，再留约 3072 token 的生成空间」。
- KV 缓存显存大致正比于 `maxBatchSize × maxKVCacheCapacity × 层数 × 2 × 每头维度 × KV 头数 × dtype 字节数`。所以把 `maxKVCacheCapacity` 或 `maxBatchSize` 调大，会直接抬高显存占用——在 Jetson 等边缘设备上这是要精打细算的。

#### 4.2.3 源码精读

**Config 字段与注释。** CLI 灌进来的字段在 builder 侧的定义：

[cpp/builder/llmBuilder.h:37-47](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.h#L37-L47)：`LLMBuilderConfig`，每个字段都有注释说明用途，例如 `maxKVCacheCapacity` 注释为 "Maximum KV cache capacity (sequence length)"，`maxVerifyTreeSize` 注释为 "Maximum length of input_ids passed into spec base model for verification"。

注意 `toJson()` 序列化时的细节——投机解码的两个树大小只对「归属的角色」写盘：

[cpp/builder/llmBuilder.h:60-69](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.h#L60-L69)：仅当 `specBase` 时写 `max_verify_tree_size`，仅当 `specDraft` 时写 `max_draft_tree_size`。这与运行时按角色读取对应配置呼应。

**引擎文件名按角色分流。** 这解释了「为什么 base/draft 要共用同一 engineDir 却不冲突」：

[cpp/builder/llmBuilder.cpp:254-266](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L254-L266)：`specDraft` → `spec_draft.engine`，`specBase` → `spec_base.engine`，普通 → `llm.engine`。

配置与外部权重文件也按角色加前缀防冲突：

[cpp/builder/llmBuilder.cpp:1325-1337](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L1325-L1337)：draft 写 `draft_config.json`，base 写 `base_config.json`，普通写 `config.json`。

[cpp/builder/llmBuilder.cpp:1315-1322](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L1315-L1322)：draft 的外部权重文件名前加 `draft_` 前缀。

这就是为什么 EAGLE3/MTP/DFlash 的 base 与 draft **必须指向同一个 engineDir**——它们靠文件名（`spec_base.engine` vs `spec_draft.engine`、`base_config.json` vs `draft_config.json`、`draft_` 前缀）在同一目录里和平共处，运行时再按角色分别加载。

#### 4.2.4 代码实践

**实践目标**：组装并理解标准 LLM 构建命令。

**操作步骤**：参照 [quick-start-guide.md:205-210](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/user_guide/getting_started/quick-start-guide.md#L205-L210) 的真实示例，组装：

```bash
./build/examples/llm/llm_build \
    --onnxDir $WORKSPACE_DIR/$MODEL_NAME/onnx/llm \
    --engineDir $WORKSPACE_DIR/$MODEL_NAME/engines \
    --maxBatchSize 1 \
    --maxInputLen 1024 \
    --maxKVCacheCapacity 4096
```

**需要观察的现象 / 预期结果**（如无 GPU 无法真正构建，则作源码阅读型验证）：

- 成功时 `engineDir` 下会生成 `llm.engine`（[llmBuilder.cpp:265](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L265)）。
- 解释：`maxInputLen=1024` 限定 prompt 最多 1024 token；`maxKVCacheCapacity=4096` 限定整条序列最多 4096 位置，故可生成约 3072 token；`maxBatchSize=1` 因为只服务单请求。若实际 prompt 超过 1024，会在 prefill 阶段越界；若 prompt+生成超过 4096，KV 缓存/RoPE 会越界。

> **待本地验证**：实际 engine 文件大小、构建耗时与显存峰值取决于具体 GPU 与模型，需在真实设备上确认。

#### 4.2.5 小练习与答案

**练习 1**：把 `--maxKVCacheCapacity` 设成 512、`--maxInputLen` 保持 1024，会怎样？
**答案**：这违反了 `maxKVCacheCapacity ≥ maxInputLen` 的约束。RoPE/KV 缓存最大只能寻址 512 个位置，却要 prefill 1024 个 prompt token，prefill 后期就会超出缓存容量——运行时会出错或被 builder 的 profile 设置间接限制。正确做法是让 `maxKVCacheCapacity` 至少等于 `maxInputLen` 再加上期望的生成长度。

**练习 2**：为什么 `--maxVerifyTreeSize` 只在 `specBase` 时才会被写进配置 JSON？
**答案**：因为 `maxVerifyTreeSize` 描述的是 **base 模型做验证**时的 token 上限，是 base 角色独有的参数；draft 角色用的是 `maxDraftTreeSize`。`toJson()` 用 `if (specBase)` / `if (specDraft)` 分别写各自字段，避免把无关配置写进错误的 engine（[llmBuilder.h:60-69](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.h#L60-L69)）。

**练习 3**：在不传 `--specBase`/`--specDraft` 的情况下，builder 阶段 4 会选哪个 ONNX 文件？
**答案**：选 `model.onnx`（普通 vanilla）。只有 `--maxLoraRank > 0` 时才会改选 `lora_model.onnx`（见 [u4-l2](u4-l2-dual-phase-profiles-model-dispatch.md) 的模型类型分发）。

---

### 4.3 visual_build：视觉编码器与图像 token 三元组

#### 4.3.1 概念说明

`visual_build` 把视觉编码器（ViT）ONNX 编成 engine，是 VLM（视觉语言模型）推理的另一半。它和 `llm_build` 的关键差异在于：语言引擎关心「序列长度 + KV 缓存」，视觉引擎关心「**图像 token 数**」。

图像 token 是指一张图被视觉编码器切成若干 patch 后、最终送进 LLM 的 token 数。`visual_build` 用三个参数刻画它的运行时范围：

- `--minImageTokens`（默认 4）：一批里图像 token 总数的下界。
- `--maxImageTokens`（默认 1024）：一批里图像 token 总数的上界。
- `--maxImageTokensPerImage`（默认 512）：**单张图**最多贡献多少 token。

视觉引擎**没有 prefill/decode 双 profile**——它只配单 profile（见 [u4-l2](u4-l2-dual-phase-profiles-model-dispatch.md)），因为视觉编码器是一次性前向、不存在逐 token 解码。

另外注意一个工程细节：`visual_build` 不会把 engine 直接写到 `--engineDir`，而是写到 **`--engineDir/visual/`** 子目录（[visual_build.cpp:192](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/multimodal/visual_build.cpp#L192)）。这样 VLM 的语言 engine 与视觉 engine 可以共用一个顶层 `engineDir` 而互不覆盖。

#### 4.3.2 核心流程：图像 token 三元组的约束

三个 token 参数不是任意取值，CLI 在 `main` 里做了一道硬校验：

\[ \text{minImageTokens} \;\leq\; \text{maxImageTokensPerImage} \;\leq\; \text{maxImageTokens} \]

直觉解释：

- `maxImageTokensPerImage` 不能小于 `minImageTokens`：单张图至少要能凑够 batch 的最小 token 数。
- `maxImageTokensPerImage` 不能大于 `maxImageTokens`：单张图不可能超过整批的上界。

此外，[u4-l2](u4-l2-dual-phase-profiles-model-dispatch.md) 提到一个模型特定约束：InternVL/Phi4 要求图像 token 是 **256 的倍数**。这是 `setup*ViTProfile` 内部的约束，CLI 这层只做三元组相对大小校验，不强制倍数。

#### 4.3.3 源码精读

**默认值：**

[examples/multimodal/visual_build.cpp:43-53](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/multimodal/visual_build.cpp#L43-L53)：`ViTBuildArgs` 默认 `minImageTokens=4`、`maxImageTokens=1024`、`maxImageTokensPerImage=512`、`profilingDetailed=false`。

**三元组校验：**

[examples/multimodal/visual_build.cpp:177-185](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/multimodal/visual_build.cpp#L177-L185)：若 `maxImageTokensPerImage < minImageTokens` 或 `> maxImageTokens`，报错并退出。这是 CLI 层唯一的业务校验。

**输出目录与 build：**

[examples/multimodal/visual_build.cpp:186-200](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/multimodal/visual_build.cpp#L186-L200)：填充 `VisualBuilderConfig`，把 `actualEngineDir = engineDir + "/visual"`，构造 `VisualBuilder(onnxDir, actualEngineDir, config)` 并 `build()`。

**Config 结构体：**

[cpp/builder/visualBuilder.h:38-43](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/visualBuilder.h#L38-L43)：`VisualBuilderConfig` 含三元组、`profilingDetailed`，以及一个 CLI 不暴露的 `useTrtNativeVitAttn`（默认 false，表示用 EdgeLLM 自定义注意力插件而非 TRT 原生 IAttention）。

#### 4.3.4 代码实践

**实践目标**：组装 VLM 视觉构建命令，并理解各参数。

**操作步骤**：参照 [phi4.md:72-77](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/user_guide/examples/phi4.md#L72-L77) 的真实 Phi-4-Multimodal 示例：

```bash
./build/examples/multimodal/visual_build \
  --onnxDir $WORKSPACE_DIR/$MODEL_NAME/onnx/visual \
  --engineDir $WORKSPACE_DIR/$MODEL_NAME/engines \
  --minImageTokens 256 \
  --maxImageTokens 1024 \
  --maxImageTokensPerImage 512
```

**需要观察的现象 / 预期结果**：

- `--minImageTokens 256`：注意这里特意用 256——因为 Phi4 要求图像 token 是 256 的倍数（见 [u4-l2](u4-l2-dual-phase-profiles-model-dispatch.md)）。
- 校验三元组：`256 ≤ 512 ≤ 1024`，通过 [visual_build.cpp:177-185](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/multimodal/visual_build.cpp#L177-L185) 的校验。
- engine 实际写到 `$WORKSPACE_DIR/$MODEL_NAME/engines/visual/` 子目录（注意 `/visual` 后缀），与同目录下的语言 engine `llm.engine` 并存。

**练习改参数观察**：试着把 `--maxImageTokensPerImage` 设为 `2048`（大于 `--maxImageTokens 1024`），重新运行命令。

**预期结果**：CLI 在校验处直接报错 `maxImageTokensPerImage must be ... less than or equal to maxImageTokens` 并退出，不会进入 build（[visual_build.cpp:179-184](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/multimodal/visual_build.cpp#L179-L184)）。

> **待本地验证**：视觉 engine 的实际输出路径与文件大小需在真实设备上确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么 VLM 的 `--engineDir` 给同一个目录，视觉 engine 却不会覆盖语言 engine？
**答案**：`visual_build` 在内部把输出目录改成 `engineDir + "/visual"`（[visual_build.cpp:192](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/multimodal/visual_build.cpp#L192)），视觉 engine 落在子目录，而语言 engine `llm.engine` 落在父目录，两者物理隔离。

**练习 2**：`--maxImageTokens` 与 `--maxImageTokensPerImage` 各自约束什么？
**答案**：`--maxImageTokens` 是**一批里图像 token 总数**的上界（batch 维度聚合后的上限），`--maxImageTokensPerImage` 是**单张图**贡献的 token 上界。一批可以含多张图，所以每张图上限 ≤ 一批总上限。

**练习 3**：视觉引擎为什么没有 `--maxKVCacheCapacity` 这种参数？
**答案**：视觉编码器（ViT）是一次性前向处理图像、不生成 token，没有 autoregressive 解码，也就没有需要跨步累积的 KV 缓存。KV 缓存是语言解码器的事（见 [u5-l5](u5-l5-kv-cache-hybrid-cache-management.md)）。

---

## 5. 综合实践

本任务把三类典型构建命令串起来，要求你为每一条解释 `maxKVCacheCapacity` 与输入长度的关系。以下命令均取自项目真实文档（[engine-builder.md:325-354](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/developer_guide/software-design/engine-builder.md#L325-L354)）。

### 任务 A：标准 LLM 构建

```bash
./build/examples/llm/llm_build \
  --onnxDir=onnx_models/my_llm \
  --engineDir=engines/my_llm \
  --maxBatchSize=1 \
  --maxInputLen=1024 \
  --maxKVCacheCapacity=4096
```

**解释 `maxKVCacheCapacity` 与输入长度的关系**：`maxInputLen=1024` 限定 prompt 最多 1024 token（prefill profile 输入形状，[llmBuilder.cpp:651-653](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L651-L653)）；`maxKVCacheCapacity=4096` 是整条序列的长度上限（RoPE/KV 缓存，[llmBuilder.cpp:616-624](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L616-L624)）。两者满足 `4096 ≥ 1024`，生成余量约 3072 token。产物：`engines/my_llm/llm.engine`。

### 任务 B：EAGLE base+draft 构建（共用同一 engineDir）

```bash
# Build base model
./build/examples/llm/llm_build \
  --onnxDir=onnx_models/qwen2.5-vl-7b_eagle3_base \
  --engineDir=engines/qwen2.5-vl-7b_eagle3 \
  --maxBatchSize=1 --maxInputLen=1024 --maxKVCacheCapacity=4096 \
  --specBase

# Build draft model
./build/examples/llm/llm_build \
  --onnxDir=onnx_models/qwen2.5-vl-7b_eagle3_draft \
  --engineDir=engines/qwen2.5-vl-7b_eagle3 \
  --maxBatchSize=1 --maxInputLen=1024 --maxKVCacheCapacity=4096 \
  --specDraft
```

**关键点**：两次命令的 `--engineDir` **必须相同**（文档 [engine-builder.md:327](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/developer_guide/software-design/engine-builder.md#L327) 明确要求）。它们靠文件名分流：base 产 `spec_base.engine` + `base_config.json`，draft 产 `spec_draft.engine` + `draft_config.json`（[llmBuilder.cpp:254-266](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L254-L266)、[1325-1337](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L1325-L1337)），draft 的外部权重还加 `draft_` 前缀（[1315-1322](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L1315-L1322)）。

**`maxKVCacheCapacity` 与输入长度的关系（base 侧）**：base 模型在投机解码里既要 prefill prompt、又要验证 draft 提议的一批候选 token，所以其输入维度受 `maxVerifyTreeSize`（默认 60）影响——验证时一次喂入的 token 数 = 已有上下文 + 候选树。但 KV 缓存容量 `maxKVCacheCapacity=4096` 仍是整条序列的硬上限：已确认接受的 token 累计不能超过 4096。draft 侧的输入则受 `maxDraftTreeSize` 约束（[llmBuilder.cpp:682](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L682)）。两边 `maxKVCacheCapacity` 取值必须协调，否则会出现 base 与 draft 上下文长度不一致的问题。

### 任务 C：VLM 的 LLM + 视觉构建

```bash
# 1) 语言引擎
./build/examples/llm/llm_build \
  --onnxDir=onnx_models/qwen2.5-vl-3b \
  --engineDir=engines/qwen2.5-vl-3b \
  --maxBatchSize=1 --maxInputLen=1024 --maxKVCacheCapacity=4096

# 2) 视觉引擎
./build/examples/multimodal/visual_build \
  --onnxDir=onnx_models/qwen2.5-vl-3b/visual_enc_onnx \
  --engineDir=engines/qwen2.5-vl-3b \
  --minImageTokens=128 --maxImageTokens=512 --maxImageTokensPerImage=512
```

**`maxKVCacheCapacity` 与输入长度的关系（VLM 侧）**：在 VLM 里，「输入长度」不只包含文本 prompt，还包含**图像 token**。图像被视觉引擎编码成 token 后插入文本序列，所以语言侧的 `maxInputLen` 必须能容纳「文本 prompt + 图像 token」之和。因此 VLM 的 `maxInputLen` 通常要比纯文本场景设得更大，相应地 `maxKVCacheCapacity` 也要 ≥ 这个更大的 `maxInputLen`。若图像 token 数（由 `--maxImageTokens` 决定）与文本相加超过 `maxKVCacheCapacity`，prefill 后会越界。视觉引擎本身没有 KV 缓存参数，其图像 token 范围由三元组（`min/maxImageTokens`、`maxImageTokensPerImage`）单独控制（[visual_build.cpp:186-192](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/multimodal/visual_build.cpp#L186-L192)）。

> **待本地验证**：以上三条命令的产物文件清单（engine、config、外部权重）需在真实 GPU 设备上构建后核对。无 GPU 时，请至少把命令与参数含义逐行解释清楚，并对照源码确认每个参数对应的 Config 字段与 profile 行。

## 6. 本讲小结

- `llm_build` 与 `visual_build` 都是**薄封装**：`getopt_long` 解析参数 → 校验 `config.json` → 填充 Config → 构造 Builder → 调 `build()`，业务逻辑全在 builder 里。
- `llm_build` 的核心三参数 `--maxInputLen` / `--maxKVCacheCapacity` / `--maxBatchSize` 决定 prefill/decode 双 profile 的动态形状与 KV 缓存显存预算；`--maxKVCacheCapacity` 是整条序列长度上限，必须 ≥ `--maxInputLen`，生成余量 = 两者之差。
- `--specBase` / `--specDraft` 标记投机解码角色，两者必须共用同一 `--engineDir`，靠 `spec_base.engine` / `spec_draft.engine`、`base_config.json` / `draft_config.json`、`draft_` 前缀防冲突。
- `visual_build` 用图像 token 三元组 `--minImageTokens` / `--maxImageTokens` / `--maxImageTokensPerImage` 刻画视觉引擎范围，CLI 层校验 `min ≤ perImage ≤ max`；引擎落到 `engineDir/visual/` 子目录。
- 视觉引擎无 prefill/decode 双 profile、无 KV 缓存参数；VLM 的语言侧 `maxInputLen` 需把图像 token 也算进去。
- `--eagleDraft`/`--eagleBase` 是 `--specDraft`/`--specBase` 的弃用别名，效果等价。

## 7. 下一步学习建议

- 构建出 engine 后，下一步就是**运行时加载它做推理**——这正是 [u5-l1 LLMInferenceRuntime 与 handleRequest](u5-l1-llminferenceruntime-handle-request.md) 的主题，你会看到运行时如何按角色读 `spec_base.engine`/`spec_draft.engine` 并把双引擎接到投机解码策略上。
- 想理解 KV 缓存如何在运行时按 `maxKVCacheCapacity` 分配、复用，请读 [u5-l5 KV 缓存与混合缓存管理](u5-l5-kv-cache-hybrid-cache-management.md)。
- 想了解 profile 设置阶段 6 内部如何按模型类型分发（vanilla/spec/dflash/gemma4-mtp），可回看 [u4-l2](u4-l2-dual-phase-profiles-model-dispatch.md) 的决策树，本讲的 CLI 参数正是那棵决策树的输入。
- 若你要接入一个全新模型并完整跑通 export→build→inference，可参考 [u9-l4 接入一个新模型架构](u9-l4-adding-a-new-model-architecture.md)。
