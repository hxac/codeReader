# WebLLM 项目总览：定位、特性与生态

## 1. 本讲目标

读完本讲，你应该能够：

1. 用一句话说清 WebLLM 是什么、它和传统「调服务器 API」的 LLM 应用有什么本质区别。
2. 说出 WebLLM 在 MLC 生态（MLC LLM、WebLLM Chat、TVM）中的位置。
3. 列举 README 中声明的核心特性，并把每条特性粗略对应到源码目录中的模块。
4. 理解「一个模型」在 WebLLM 眼里由哪些产物组成：模型权重（`model`）、模型库（`model_lib`，wasm 文件）、tokenizer 配置等。
5. 学会查阅 `src/config.ts` 中的 `prebuiltAppConfig.model_list`，读懂模型家族、量化命名（如 `q4f16_1`）与显存需求（`vram_required_MB`），并能据此为给定内存的设备挑选模型。

## 2. 前置知识

本讲是整个手册的第一讲，不默认你读过这个项目的任何代码，但以下几个概念最好先有个直觉：

- **LLM（大语言模型）**：一种深度神经网络。给它一段文字（prompt），它会逐个 token 地「续写」出回答。token 是模型处理文字的最小单位，一个汉字、一个英文单词的一部分都可以是 token。
- **推理（inference）**：用训练好的模型生成结果的过程。与「训练」相对，推理不需要更新模型参数，只做前向计算。
- **WebGPU**：浏览器暴露 GPU 计算能力的标准 API，可以理解为「浏览器里的 CUDA」。WebLLM 的全部模型计算都跑在 WebGPU 上，这是它「高性能」的根基。
- **WebAssembly（wasm）**：一种可在浏览器中接近原生速度执行的字节码格式。模型中无法用 JS 高效完成的部分被编译成 wasm。
- **npm / TypeScript**：WebLLM 以 npm 包 `@mlc-ai/web-llm` 发布，源码是 TypeScript（带类型标注的 JavaScript）。看懂接口定义即可，不需要会写。
- **量化（quantization）**：把模型权重从 16/32 位浮点压缩到更低位宽（如 4 位）以减小体积、加快推理的技术。名字里 `q4` 的「4」就是指这个。
- **显存（VRAM）/ 统一内存**：模型权重和计算中间结果都要放在 GPU 可访问的内存里。普通笔记本 CPU 与 GPU 共享内存，所以「8GB 内存的笔记本」意味着模型、浏览器、操作系统要挤在同一块 8GB 里。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目门面：定位、核心特性、内置模型家族、上手方式、自定义模型入口 |
| `src/config.ts` | 全部「模型从哪来」的元数据：`ModelRecord`、`AppConfig`、`prebuiltAppConfig`（167 条预置模型记录） |
| `docs/README.md` | 官方文档站（Sphinx）的本地构建说明 |
| `docs/index.rst` | 文档站首页与目录树（toctree），是官方文档的知识结构图 |

辅助了解但不精读：`src/` 目录（感受模块划分即可，后续各讲逐个深入）。

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：①定位与生态，②关键特性清单，③模型的三类分发产物，④`prebuiltAppConfig` 模型列表与选型，⑤文档站点结构。

### 4.1 WebLLM 是什么：定位与生态

#### 4.1.1 概念说明

WebLLM 是一个**完全运行在浏览器里的 LLM 推理引擎**。传统的网页版 AI 应用（例如各类 ChatGPT 套壳）流程是：浏览器把你的消息发给远端服务器 → 服务器上的大模型生成回答 → 回传浏览器。而 WebLLM 把「大模型本体」直接下载到你自己的浏览器里，用你电脑的 GPU（通过 WebGPU）完成全部推理，**没有任何服务器参与生成过程**。

这带来三个直接后果：

1. **隐私**：对话内容不出本机。
2. **零服务端成本**：部署方只需要一个静态网页，不需要 GPU 服务器。
3. **代价是首次加载慢**：模型动辄几百 MB 到几 GB，第一次访问要下载并缓存到浏览器。

在 MLC 生态中，WebLLM 的位置是：

- **MLC LLM**（`mlc-ai/mlc-llm`）：通用 LLM 编译与部署框架，负责把 HuggingFace 上的开源模型编译成各平台可运行的产物。WebLLM 是它的**伴侣项目（companion project）**，复用 MLC 产出的模型权重格式与编译产物。
- **WebLLM**（本仓库）：一个 TypeScript 库（npm 包），把 MLC 格式的模型「搬进」浏览器运行。
- **WebLLM Chat**（`mlc-ai/web-llm-chat`）：基于 WebLLM 构建的完整聊天应用（chat.webllm.ai），是 WebLLM 的一个大型「用户」。
- **TVM / tvmjs**：底层的机器学习编译运行时，WebLLM 的推理管线建立在其 Web 版（tvmjs，npm 包 `@mlc-ai/web-runtime`）之上。

一句话总结四者关系：**TVM 提供运行时底座，MLC LLM 负责编译模型，WebLLM 把两者包装成浏览器可用的 API，WebLLM Chat 用它做出产品。**

#### 4.1.2 核心流程

用户访问一个基于 WebLLM 的网页后，整体数据流是：

```text
用户提问
   │
   ▼
浏览器页面（JS/TS）
   │  engine.chat.completions.create(...)   ← OpenAI 风格 API
   ▼
MLCEngine（WebLLM 核心）
   │  首次运行：下载权重 + wasm 模型库 → 存入浏览器缓存
   │  之后：直接读缓存
   ▼
LLMChatPipeline（推理管线）
   │  tokenizer 编码 → prefill 预填充 → decode 逐 token 生成
   ▼
tvmjs + WebGPU（GPU 上的真正矩阵运算）
   │
   ▼
逐 token 的回答返回页面（可流式输出）
```

本讲只需要记住这条链路的「形状」，链路上每个环节的源码会在单元二、单元三逐一精读。

#### 4.1.3 源码精读

README 开头一句副标题就是项目的自我定位：

- [README.md:11](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/README.md#L11) —— "High-Performance In-Browser LLM Inference Engine"（高性能浏览器内 LLM 推理引擎），这是全项目的定位句。

Overview 一节展开说明了「浏览器内 + 无服务器 + WebGPU 加速」三个关键词，并明确指出 OpenAI API 兼容是主要卖点：

- [README.md:17-28](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/README.md#L17-L28) —— 说明 WebLLM 无需服务器支持、以 WebGPU 加速、完全兼容 OpenAI API（包括流式、JSON mode 等），并且「是 MLC LLM 的伴侣项目」。

仓库徽章区直观展示了生态关系（WebLLM Chat 已部署、关联 MLC LLM 仓库）：

- [README.md:5-9](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/README.md#L5-L9) —— NPM 包、WebLLM Chat 在线演示、以及两个关联仓库（web-llm-chat、mlc-llm）的徽章。

关于 TVM 运行时依赖，README 的「从源码构建 TVMjs」一节有明确交代：

- [README.md:479-483](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/README.md#L479-L483) —— "WebLLM's runtime largely depends on TVMjs"，并给出 npm 包 `@mlc-ai/web-runtime`。这印证了上面说的「TVM 是底座」。

#### 4.1.4 代码实践

1. **实践目标**：建立对 WebLLM 的第一手体感，理解「浏览器内推理」不是营销话术。
2. **操作步骤**：
   - 打开在线演示 [https://chat.webllm.ai/](https://chat.webllm.ai/)，任选一个小模型（如 1B 级别）加载。
   - 打开浏览器 DevTools 的 **Network** 面板，刷新页面后观察：模型文件从 `huggingface.co` 等地址被下载进浏览器；再打开 **Application → Cache Storage**，能看到已缓存的模型分片。
   - 断网（或 DevTools Network 面板切到 Offline），再次发一条消息——模型仍能回答，证明生成过程完全不依赖网络。
3. **需要观察的现象**：首次加载有明显的下载进度；断网后对话依旧正常。
4. **预期结果**：确认「推理发生在本机浏览器」这一事实；Cache Storage 中出现模型缓存条目。
5. 由于演示站的模型与界面会随版本变化，具体面板截图以你本机为准，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：WebLLM 和「调用 OpenAI 服务器 API」的应用，最本质的区别是什么？

**答案**：计算发生的位置不同。WebLLM 把模型下载到浏览器、用本机 GPU（WebGPU）推理，数据不出本机、无服务端算力成本；调 API 的应用计算在远端服务器，数据要出本机。两者在 API 语法上反而高度相似（WebLLM 刻意兼容 OpenAI API）。

**练习 2**：WebLLM、MLC LLM、WebLLM Chat 三个项目谁是「库」、谁是「编译框架」、谁是「应用」？

**答案**：WebLLM 是浏览器端推理**库**（npm 包）；MLC LLM 是**编译框架**，负责把开源模型编译成可部署产物（WebLLM 复用其产物）；WebLLM Chat 是基于 WebLLM 构建的**网页应用**。

### 4.2 关键特性清单：README Key Features 精读

#### 4.2.1 概念说明

README 的 Key Features 一节是官方给出的功能地图。理解它的正确姿势不是背诵条目，而是把每条特性「挂」到源码的对应模块上——这样后续学源码时就知道每个文件服务哪个卖点。本模块先建立这张映射表，细节留给后续讲义。

#### 4.2.2 核心流程

特性 → 源码模块的映射逻辑：WebLLM 的 `src/` 目录按「用户接口 / 推理管线 / 运行环境 / 协议 / 基础设施」分层，每条 README 特性基本都能落到某一层：

```text
README 特性                        主要源码归属（后续讲义精读）
─────────────────────────────────────────────────────────────
In-Browser Inference        →  src/llm_chat.ts（推理管线）
OpenAI API 兼容             →  src/engine.ts + src/openai_api_protocols/
Structured JSON Generation  →  管线内的语法约束（xgrammar，wasm 部分实现）
Extensive Model Support     →  src/config.ts（prebuiltAppConfig）
Custom Model Integration    →  src/config.ts（AppConfig 自定义 model_list）
Plug-and-Play Integration   →  src/index.ts（库入口导出）+ rollup 构建
Streaming                   →  engine.chat.completions.create({stream:true})
Web/Service Worker Support  →  src/web_worker.ts、src/service_worker.ts
Chrome Extension Support    →  src/extension_service_worker.ts
```

#### 4.2.3 源码精读

Key Features 一节逐条列出了九大特性：

- [README.md:36-54](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/README.md#L36-L54) —— 九条特性：In-Browser Inference（WebGPU 硬件加速）、Full OpenAI API Compatibility（流式、JSON mode、logit 级控制、种子）、Structured JSON Generation（在模型库的 WebAssembly 部分实现以获得最优性能）、Extensive Model Support（Llama 3、Phi 3、Gemma、Mistral、Qwen 等）、Custom Model Integration、Plug-and-Play Integration（npm/CDN 即插即用）、Streaming、Web Worker & Service Worker Support、Chrome Extension Support。

其中两条对新手最容易产生疑问，README 原文给出了关键澄清：

- [README.md:22-24](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/README.md#L22-L24) —— 「fully compatible with OpenAI API」的含义：你可以用**同一套 OpenAI API 写法**操作任意开源模型，且全部在本地。
- [README.md:42](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/README.md#L42) —— 结构化 JSON 生成「实现在模型库的 WebAssembly 部分」，也就是说语法约束不是在 TypeScript 层做的，这是它能保持高性能的原因，细节在高级篇（u6-l3）展开。

「即插即用」的具体形态是 CDN 一行导入：

- [README.md:110-120](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/README.md#L110-L120) —— 通过 `https://esm.run/@mlc-ai/web-llm` 直接 `import`，无需任何构建步骤即可在 JSFiddle/Codepen 里使用。

#### 4.2.4 代码实践

1. **实践目标**：验证「特性 → 源码模块」映射表不是空谈，每个卖点都能摸到真实文件。
2. **操作步骤**：
   - 在仓库根目录执行 `ls src/`，再执行 `ls src/openai_api_protocols/`。
   - 对照上面 4.2.2 的映射表，逐条在 `src/` 下找到对应文件：`llm_chat.ts`、`engine.ts`、`web_worker.ts`、`service_worker.ts`、`extension_service_worker.ts`、`config.ts`、`index.ts`。
   - 用编辑器打开 `src/index.ts` 只看 `export` 语句（先不读实现），数一数导出了哪些符号。
3. **需要观察的现象**：映射表中的每个文件都真实存在；`src/openai_api_protocols/` 下能看到 `chat_completion.ts`、`completion.ts`、`embedding.ts` 等协议文件。
4. **预期结果**：对 `src/` 形成「文件名 → 职责」的初版 mental map；本讲不要求看懂实现。
5. 若你尚未 clone 仓库，可在 GitHub 网页端浏览 `src/` 目录完成同样观察（结果一致，无需本地验证环境）。

#### 4.2.5 小练习与答案

**练习 1**：「WebLLM 完全兼容 OpenAI API」和「WebLLM 能访问 OpenAI 的服务器」是一回事吗？

**答案**：不是。兼容指的是**接口语法与语义对齐**（请求/响应字段、流式协议等），让你可以用 OpenAI SDK 的写法调用本地模型；WebLLM 从不连接 OpenAI 服务器，所有推理都在本机完成。

**练习 2**：如果要做「页面上打字机效果逐字输出」的聊天应用，对应 README 哪条特性、源码哪个模块？

**答案**：对应 Streaming 特性；用户侧入口是 `engine.chat.completions.create({ stream: true })`（`src/engine.ts` 与 `src/openai_api_protocols/chat_completion.ts`），流式细节在 u2-l3 讲。

### 4.3 一个模型由什么构成：ModelRecord 与三类分发产物

#### 4.3.1 概念说明

在服务器世界里，「部署一个模型」往往是一个目录、一套服务。而在浏览器里，WebLLM 必须显式地描述「模型的每个部件从哪个 URL 下载」。`src/config.ts` 中的 `ModelRecord` 接口就是**一条模型记录的完整描述**，它揭示了 WebLLM 世界的「一个模型」由哪些产物组成：

1. **模型权重与元数据（`model` 字段）**：一个 HuggingFace 仓库 URL。里面除了量化后的权重，还包括 `mlc-chat-config.json`（模型的聊天配置：上下文窗口、对话模板、默认采样参数等）和 tokenizer 文件（分词器，决定文字如何切成 token）。
2. **模型库（`model_lib` 字段）**：一个 `.wasm` 文件的 URL，包含执行该模型计算图的「可执行代码」。不同架构的模型（Llama、Phi、Qwen……）需要不同的模型库；同一架构的不同量化变体通常可以**共用**同一个模型库。
3. **覆盖项（`overrides` 字段）**：可选的运行时配置覆盖，比如把 `context_window_size` 改小以省显存。

理解了这三类产物，就理解了 WebLLM 的分发模型：**权重决定「模型是谁」，wasm 决定「模型怎么算」，配置决定「模型怎么聊」。**

#### 4.3.2 核心流程

加载一个模型时（细节在 u2-l1 精读），WebLLM 大致做这些事：

```text
1. 拿到 model_id（如 "Llama-3.2-1B-Instruct-q4f32_1-MLC"）
2. 在 AppConfig.model_list 里查到对应 ModelRecord
3. 从 record.model（HuggingFace 仓库）拉取 mlc-chat-config.json 与 tokenizer 文件
4. 从 record.model（同仓库）分片拉取量化权重
5. 从 record.model_lib 拉取 wasm 模型库
6. 以上产物写入浏览器缓存（Cache API / IndexedDB / OPFS，见 u4-l1）
7. 组装推理管线，开始服务请求
```

#### 4.3.3 源码精读

`ModelRecord` 接口的每个字段都有官方注释，是理解分发机制的钥匙：

- [src/config.ts:257-286](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L257-L286) —— `ModelRecord` 定义：`model`（HuggingFace 权重链接，支持 4 种 URL 格式）、`model_id`（模型的名字）、`model_lib`（wasm 模型库链接）、`overrides`（可覆盖 KV cache 等设置）、`vram_required_MB`（所需显存，可用 `utils/vram_requirements` 计算）、`low_resource_required`（能否跑在手机等受限设备）、`buffer_size_required_bytes`（对设备 `maxStorageBufferBindingSize` 的要求）、`required_features`（如 shader-f16）、`model_type`（LLM/embedding/VLM）、`integrity`（可选的 SRI 完整性校验哈希）。

模型的「类型」用一个枚举区分，说明 model_list 里不只有聊天模型：

- [src/config.ts:251-255](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L251-L255) —— `ModelType` 枚举包含 `LLM`、`embedding`、`VLM`（视觉语言模型）三种，未标注时默认 LLM。

预置模型库的公共下载前缀由两个常量拼出：

- [src/config.ts:326-335](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L326-L335) —— `modelVersion = "v0_2_84/base"` 标记当前 npm 版本兼容的预置模型库版本（注释明确说明它不必与 npm 版本同步），`modelLibURLPrefix` 指向 GitHub 上 `mlc-ai/binary-mlc-llm-libs` 仓库的 wasm 存放目录。

一条真实的模型记录长这样（model_list 的第一条）：

- [src/config.ts:358-370](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L358-L370) —— `Llama-3.2-1B-Instruct-q4f32_1-MLC` 的完整记录：权重来自 `mlc-ai/Llama-3.2-1B-Instruct-q4f32_1-MLC` 仓库；wasm 库为 `modelLibURLPrefix + modelVersion + "/Llama-3.2-1B-Instruct-q4f32_1_cs1k-webgpu.wasm"`；需要约 1128.82 MB 显存；`low_resource_required: true` 表示可在低配设备运行；`overrides` 把上下文窗口设为 4096。

「不同模型变体可共用同一个模型库」在 README 中也有说明：

- [README.md:443-446](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/README.md#L443-L446) —— 举例 `NeuralHermes-Mistral` 可复用 `Mistral` 的模型库，即只换权重、不换 wasm。

#### 4.3.4 代码实践

1. **实践目标**：亲手摸到 `ModelRecord` 的数据形态，而不仅是读接口定义。
2. **操作步骤**：
   - 新建一个 HTML 文件（或直接在支持 ESM 的页面/Codepen 中），写入下面的**示例代码**（依据 README 的 CDN 用法）：

     ```html
     <!doctype html>
     <html>
     <body>
       <script type="module">
         // 示例代码：通过 CDN 导入 webllm，打印第一条模型记录
         import * as webllm from "https://esm.run/@mlc-ai/web-llm";
         const first = webllm.prebuiltAppConfig.model_list[0];
         console.log("model_id:", first.model_id);
         console.log("model:", first.model);
         console.log("model_lib:", first.model_lib);
         console.log("vram_required_MB:", first.vram_required_MB);
         console.log("记录总数:", webllm.prebuiltAppConfig.model_list.length);
       </script>
     </body>
     </html>
     ```

   - 用浏览器直接打开该文件（需可访问外网），打开控制台查看输出。
3. **需要观察的现象**：控制台打印出 `Llama-3.2-1B-Instruct-q4f32_1-MLC` 及其权重 URL、wasm URL 和显存需求；记录总数约为一百多条（随版本变化）。
4. **预期结果**：与 4.3.3 中引用的 [src/config.ts:358-370](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L358-L370) 源码内容一致——你看到的就是这条 TypeScript 字面量在运行时的形态。
5. CDN 加载与打印依赖网络环境，**待本地验证**；若无法联网，替代方案是在编辑器里直接阅读 `src/config.ts` 第 354 行起的 `prebuiltAppConfig` 字面量。

#### 4.3.5 小练习与答案

**练习 1**：`model` 和 `model_lib` 分别存放什么？为什么拆成两个 URL？

**答案**：`model` 指向 HuggingFace 上的模型仓库（量化权重 + mlc-chat-config.json + tokenizer），`model_lib` 指向 wasm 模型库（执行计算的编译产物）。拆开是因为「换了权重但架构相同」的模型变体可以复用同一个 wasm，避免重复分发执行代码。

**练习 2**：`overrides.context_window_size: 4096` 起什么作用？

**答案**：把该模型运行时的上下文窗口（KV cache 能缓存的最大 token 数）覆盖为 4096。窗口越大越能记长对话，但 KV cache 占用显存越多；预置记录里常以此在长上下文能力与显存之间取平衡。

### 4.4 prebuiltAppConfig 模型列表：家族、量化与选型

#### 4.4.1 概念说明

`prebuiltAppConfig` 是 WebLLM 内置的默认 `AppConfig`：不传自定义配置时，`CreateMLCEngine` 就在这份列表里找模型。它是「**当前 npm 版本兼容哪些预置模型库的唯一事实来源**」（源码注释原话）。

阅读它需要掌握三个概念：

1. **模型家族（family）**：同架构的不同型号，如 Llama、Phi、Gemma、Mistral、Qwen（通义千问）。`config.ts` 里用注释（如 `// Llama-3.2`、`// Qwen-3`）给记录分组。
2. **量化变体命名**：`model_id` 后缀如 `q4f32_1`、`q4f16_1`，这是 MLC 社区的命名约定——`q4` 表示权重量化为 4 bit，`f32`/`f16` 表示激活值使用 32/16 位浮点精度，末尾 `_1` 是量化方案代号。同一家族同一尺寸常有多个量化变体：位宽越低体积越小、显存越省，但精度损失可能越大。具体量化参数由模型库侧定义，本讲只需会「读名识大小」。
3. **资源需求字段**：`vram_required_MB`（运行所需显存，MB）、`low_resource_required`（适合手机等低配设备）、`required_features`（如 `shader-f16`，要求设备 WebGPU 支持 f16 shader）。

选型的核心是显存估算直觉。权重大小可粗估为：

\[ \text{权重大小（字节）} \approx N_{\text{参数量}} \times \frac{b_{\text{权重位宽}}}{8} \]

例如 1.5B 参数、4 bit 量化的模型，权重约 \( 1.5 \times 10^9 \times 4 / 8 = 7.5 \times 10^8 \) 字节 ≈ 715 MB；再加上 KV cache、激活缓冲与运行时开销，就得到 `vram_required_MB` 里的 1629.75 MB 这个量级。反过来，看到 `vram_required_MB` 就能反推模型大致规模。

#### 4.4.2 核心流程

在 `prebuiltAppConfig.model_list` 里定位模型信息的流程：

```text
1. 确定目标：想找某家族（如 Qwen）、某尺寸（如 1.5B）的模型
2. 在 src/config.ts 中搜索该家族的分组注释（如 "// Qwen-2"）或直接搜 model_id
3. 读该条记录：model_id（含量化后缀）→ 量化格式
             vram_required_MB → 显存需求
             low_resource_required / required_features → 设备限制
4. 与目标设备显存对比（注意共享内存设备要给系统留余量）
5. （可选）在 mlc.ai/models 上交叉核对同一模型的完整信息
```

#### 4.4.3 源码精读

`prebuiltAppConfig` 的定义与官方注释：

- [src/config.ts:348-356](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L348-L356) —— 注释说明这是「未指定时的默认模型与模型库映射」，且是「预置模型库与当前 npm 版本兼容性的唯一事实来源」；默认缓存后端为 `"cache"`（浏览器 Cache API）。截至当前 commit，`model_list` 共收录 **167 条**模型记录（含同型号的不同量化变体），文件第 354 行一直延伸到第 2603 行。

三个不同家族的代表记录（本讲综合实践要用）：

- [src/config.ts:463-475](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L463-L475) —— **Llama 家族**：`Llama-3.1-8B-Instruct-q4f32_1-MLC`，量化格式 q4f32_1，需要 6101.01 MB 显存，`low_resource_required: false`（不适合低配设备）。
- [src/config.ts:699-712](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L699-L712) —— **Phi 家族**：`Phi-3.5-mini-instruct-q4f16_1-MLC`，量化格式 q4f16_1，需要 3672.07 MB 显存。
- [src/config.ts:1494-1506](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L1494-L1506) —— **Qwen 家族**：`Qwen2.5-1.5B-Instruct-q4f16_1-MLC`，量化格式 q4f16_1，需要 1629.75 MB 显存，`low_resource_required: true`。

另外两个家族的记录展示了 `required_features` 的用法：

- [src/config.ts:810-824](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L810-L824) —— **Mistral 家族**：`Mistral-7B-Instruct-v0.3-q4f16_1-MLC`，4573.39 MB 显存，且声明 `required_features: ["shader-f16"]`——设备的 WebGPU 必须支持 f16 shader 才能运行。
- [src/config.ts:1015-1027](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L1015-L1027) —— **Gemma 家族**：`gemma-2-2b-it-q4f16_1-MLC`，仅需 1895.3 MB 显存，同样要求 shader-f16。

model_list 末尾还有非聊天模型——embedding 模型，它们用 `model_type` 标注：

- [src/config.ts:2592-2601](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L2592-L2601) —— `snowflake-arctic-embed-s-q0f32-MLC-b4`，`model_type: ModelType.embedding`，仅需 238.71 MB 显存。embedding 模型不生成文本，而是把文本映射成向量（见 u2-l5）。

README 的 Built-in Models 一节给出主要家族清单，并指向 `prebuiltAppConfig.model_list` 作为权威来源：

- [README.md:56-68](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/README.md#L56-L68) —— 列出 Llama、Phi、Gemma、Mistral、Qwen 五个主要家族，完整列表以 `prebuiltAppConfig.model_list` 为准；还提供两条扩展路径：开 issue 申请新模型，或按 Custom Models 自行编译。

> **阅读提示**：README 第 58 行把 `prebuiltAppConfig.model_list` 链接标注为 `config.ts#L293`，但在本手册使用的 commit（`90f6709`）中它实际位于 [src/config.ts:354](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L354)。文档中的行号锚点会随代码演进滞后——**遇到对不上号的引用，养成用搜索（如 `prebuiltAppConfig`）重新定位的习惯**。同理，README 的家族清单也比 `config.ts` 里的实际内容（还有 Qwen-3、Phi-4、embedding 模型等）保守，以源码为准。

#### 4.4.4 代码实践

1. **实践目标**：学会用工具快速统计与筛选 `model_list`，而不是肉眼扫 2000 多行配置。
2. **操作步骤**：
   - 在仓库根目录执行以下只读命令：

     ```bash
     # 统计预置模型记录总数
     grep -c "model_id:" src/config.ts

     # 列出所有标记为低资源可用（适合弱设备）的模型 id
     grep -B4 "low_resource_required: true" src/config.ts | grep "model_id:"

     # 找出所有要求 shader-f16 特性的模型
     grep -B8 'required_features: \["shader-f16"\]' src/config.ts | grep "model_id:"
     ```

   - 对照 4.4.3 引用的源码行号，验证命令输出里包含 `Llama-3.2-1B-Instruct-q4f32_1-MLC`（low_resource 组）和 `Mistral-7B-Instruct-v0.3-q4f16_1-MLC`（shader-f16 组）。
3. **需要观察的现象**：`grep -c` 输出 167；low_resource 列表里出现多个 1B 级小模型；shader-f16 列表里多为 f16 量化的中大型模型。
4. **预期结果**：掌握「grep + 源码」的选型调研方法；输出内容与 4.4.3 的表格互相印证。
5. 命令只读、无副作用，可在任意 clone 好的仓库执行（Windows 用户可在 Git Bash 中运行）。

#### 4.4.5 小练习与答案

**练习 1**：`Llama-3.1-8B-Instruct-q4f32_1-MLC` 和 `Phi-3.5-mini-instruct-q4f16_1-MLC` 的量化后缀各是什么含义？谁的激活精度更高？

**答案**：`q4f32_1` = 权重 4 bit、激活 32 位浮点；`q4f16_1` = 权重 4 bit、激活 16 位浮点。Llama-3.1-8B 的激活精度更高（f32 > f16），代价是显存需求更大（6101.01 MB vs 3672.07 MB）。

**练习 2**：一台手机的 GPU 不支持 shader-f16。以下哪个模型一定无法运行：`gemma-2-2b-it-q4f16_1-MLC`（见 [src/config.ts:1015-1027](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L1015-L1027)）还是 `Llama-3.2-1B-Instruct-q4f32_1-MLC`（见 [src/config.ts:358-370](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L358-L370)）？

**答案**：`gemma-2-2b-it-q4f16_1-MLC` 一定无法运行，因为它的记录声明了 `required_features: ["shader-f16"]`；而 `Llama-3.2-1B-Instruct-q4f32_1-MLC` 的记录没有该要求，且 `low_resource_required: true`、只需约 1.1 GB 显存，是低配设备的典型选择。

**练习 3**：粗估一个 8B 参数、`q4` 量化模型的权重体积，并说明为什么它和 `vram_required_MB` 不完全相等。

**答案**：\( 8 \times 10^9 \times 4/8 = 4 \times 10^9 \) 字节 ≈ 4 GB。`vram_required_MB`（如该 Llama 记录的 6101.01 MB）还包含 KV cache、激活缓冲区、运行时等开销，因此总是大于纯权重大小。

### 4.5 文档站点结构：docs/ 与官方文档

#### 4.5.1 概念说明

除了 README，WebLLM 还有一套基于 **Sphinx** 的正式文档站（[webllm.mlc.ai/docs](https://webllm.mlc.ai/docs/)），其源文件就在仓库的 `docs/` 目录。文档分为「User Guide」和「Developer Guide」两大板块，覆盖从上手到贡献的全流程。学会看这套文档的结构，能让你在后续学习中随时找到官方的第一手说明。

#### 4.5.2 核心流程

```text
docs/ 目录结构：
docs/
├── README.md        # 如何本地构建文档
├── index.rst        # 首页 + 目录树（toctree）
├── conf.py          # Sphinx 配置
├── Makefile         # make html 入口
├── requirements.txt # 文档依赖
├── user/            # 用户指南
│   ├── get_started.rst
│   ├── basic_usage.rst
│   ├── advanced_usage.rst
│   └── api_reference.rst
└── developer/       # 开发者指南
    ├── building_from_source.rst
    └── add_models.rst
```

#### 4.5.3 源码精读

本地构建文档的三个步骤：

- [docs/README.md:9-19](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/docs/README.md#L9-L19) —— 依次执行 `pip3 install -r requirements.txt` 安装依赖、`make html` 构建、进入 `_build/html` 起一个 HTTP 服务查看。

文档站首页（index.rst）用 reStructuredText 的 toctree 指令组织全部页面：

- [docs/index.rst:21-35](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/docs/index.rst#L21-L35) —— 两棵目录树：**User Guide**（get_started → basic_usage → advanced_usage → api_reference）与 **Developer Guide**（building_from_source、add_models）。这个顺序本身就是官方建议的学习路径。

首页同样概括了定位与特性，措辞与 README 一致：

- [docs/index.rst:6-10](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/docs/index.rst#L6-L10) —— 「WebLLM 是高性能浏览器内语言模型推理引擎……提供 MLC 引擎 web 后端的专用运行时，利用 WebGPU 本地加速，提供 OpenAI 兼容 API，并内置 Web Worker 支持以将重计算与 UI 流程分离」。

#### 4.5.4 代码实践

1. **实践目标**：能在本地跑起官方文档站，或至少熟练使用在线版定位资料。
2. **操作步骤**（二选一）：
   - **在线版（推荐，零依赖）**：打开 [https://webllm.mlc.ai/docs/](https://webllm.mlc.ai/docs/)，按左侧导航依次点开 User Guide 的四个页面，浏览每页的小节标题。
   - **本地构建版**：进入 `docs/` 目录，执行 `pip3 install -r requirements.txt`，然后 `make html`，最后按 [docs/README.md:23-30](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/docs/README.md#L23-L30) 的说明 `cd _build/html && python3 -m http.server`，在浏览器访问 `http://localhost:8000`。
3. **需要观察的现象**：文档结构与 4.5.2 的目录树一致；`api_reference.rst` 是 API 的权威索引（后续遇到不确定的接口签名优先查这里）。
4. **预期结果**：把官方文档站纳入你的学习工具箱；本地构建是否成功取决于 Python 环境，**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：想知道「如何给 WebLLM 添加一个新模型」，应该读 docs 下哪个文件？

**答案**：`docs/developer/add_models.rst`（Developer Guide 板块）。它对应的正是手册 u4-l4「接入自定义模型」的主题。

**练习 2**：README、docs 站点、`src/config.ts` 三处都提到支持的模型，遇到不一致时以谁为准？

**答案**：以 `src/config.ts` 中的 `prebuiltAppConfig` 为准——它的注释明确写着它是预置模型兼容性的「唯一事实来源」（[src/config.ts:348-353](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L348-L353)）；README 与文档是给人读的摘要，更新可能滞后。

## 5. 综合实践

本讲的综合实践是**一次完整的模型选型调研**（对应大纲中的实践任务）：

**任务**：在 `src/config.ts` 的 `prebuiltAppConfig.model_list` 中找出三个不同家族的模型（如 Llama、Phi、Qwen），记录 `model_id`、量化格式与 `vram_required_MB`，然后写一段文字说明：你会为 8GB 内存的笔记本选择哪个模型，为什么。

**操作步骤**：

1. 打开 [src/config.ts:354](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L354) 起的 `prebuiltAppConfig`，用搜索定位三个家族的分组注释（`// Llama-3.1`、`// Phi-3.5-mini-instruct`、`// Qwen-2` 等）。
2. 从每个家族挑一条记录，摘录三要素：`model_id`（后缀即量化格式）与 `vram_required_MB`，同时留意 `low_resource_required` 与 `required_features`。
3. 也可在 [mlc.ai/models](https://mlc.ai/models) 上交叉核对同 `model_id` 的信息（该站点还能按 VRAM 筛选）。
4. 写选型分析（100–200 字），要求同时考虑：显存预算、系统与浏览器占用、是否需要 shader-f16、上下文窗口需求。

**参考记录（你应得到类似的表）**：

| 家族 | model_id | 量化格式 | vram_required_MB | 备注 |
| --- | --- | --- | --- | --- |
| Llama | `Llama-3.1-8B-Instruct-q4f32_1-MLC` | q4f32_1（4bit 权重 / 32bit 激活） | 6101.01 | 不适合低配设备 |
| Phi | `Phi-3.5-mini-instruct-q4f16_1-MLC` | q4f16_1（4bit 权重 / 16bit 激活） | 3672.07 | — |
| Qwen | `Qwen2.5-1.5B-Instruct-q4f16_1-MLC` | q4f16_1（4bit 权重 / 16bit 激活） | 1629.75 | 低资源可用 |

**参考选型结论（示例）**：8GB 内存的笔记本通常是 CPU/GPU 共享内存，模型要和操作系统、浏览器、其他标签页挤在同一块 8GB 里，实际可放心分配给模型的显存一般在 4–5GB 以下。因此 6101.01 MB 的 Llama-3.1-8B 风险过高（很容易触发显存不足或被系统杀掉标签页）；更稳妥的选择是 `Qwen2.5-1.5B-Instruct-q4f16_1-MLC`（约 1.6GB，`low_resource_required: true`，记录未声明 shader-f16 要求），剩余内存足以支撑 4096 的上下文窗口和页面本身。若更看重英文对话质量且设备支持 shader-f16，`gemma-2-2b-it-q4f16_1-MLC`（约 1.9GB）也是合理候选。**注意**：选型结论取决于你的具体设备与浏览器，请在自己机器上实际加载验证。

## 6. 本讲小结

- WebLLM 是**完全跑在浏览器里**的 LLM 推理引擎：权重下载到本地、WebGPU 加速、无服务器参与；它是 MLC LLM 的伴侣项目，底层依赖 TVMjs 运行时。
- README 的九大特性各对应明确的源码模块：OpenAI 兼容 → `engine.ts`/`openai_api_protocols/`，推理 → `llm_chat.ts`，Worker → `web_worker.ts`/`service_worker.ts`，模型清单 → `config.ts`。
- WebLLM 世界的「一个模型」= **权重仓库（`model`）+ wasm 模型库（`model_lib`）+ 可选覆盖（`overrides`）**，由 `ModelRecord` 接口描述；同架构变体可共用模型库。
- `prebuiltAppConfig.model_list`（当前 167 条记录）是预置模型兼容性的**唯一事实来源**；读 `model_id` 后缀可知量化格式（`q4` 位宽 / `f` 激活精度），`vram_required_MB`、`low_resource_required`、`required_features` 决定设备适配。
- 官方文档站源码在 `docs/`（Sphinx 构建），分 User Guide 与 Developer Guide 两大板块；文档行号可能滞后，遇不一致以 `src/config.ts` 为准。

## 7. 下一步学习建议

下一讲（u1-l2「跑通第一个例子：get-started 实战」）将动手运行 `examples/get-started`，把本讲的「模型记录」变成一次真实的模型加载与对话，并观察 `initProgressCallback` 汇报的下载进度。

在进入下一讲前，建议你先：

1. 通读 [README.md:81-231](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/README.md#L81-L231) 的 Get Started 章节，预告下一讲的安装与调用步骤。
2. 浏览 [src/index.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/index.ts) 的导出列表，混个眼熟——u1-l3 会逐组拆解它。
3. 若想提前感受推理细节，可粗读 [src/engine.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts) 中 `reload` 方法附近的注释，不必看懂实现。
