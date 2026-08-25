# 第 4 讲：核心概念——ModelRecord、AppConfig 与模型分发

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐字段说出 `ModelRecord` 中 `model`、`model_id`、`model_lib`、`overrides`、`vram_required_MB`、`required_features`、`model_type`、`integrity` 等字段的含义与作用。
2. 解释 `AppConfig` 如何把「模型清单（model_list）」与「缓存后端（cacheBackend / opfsAccessMode）」聚合在一起，以及 `prebuiltAppConfig` 为什么是预置模型兼容性的唯一事实来源。
3. 掌握 `ChatOptions` 的覆盖机制：`mlc-chat-config.json` → `ModelRecord.overrides` → `reload(modelId, chatOpts)` 三层配置如何逐级合并，后写的赢。
4. 能够亲手构造一个只包含单个模型、并覆盖 `context_window_size` 的自定义 `appConfig`，并通过可观察的行为验证覆盖确实生效。

## 2. 前置知识

本讲建立在前三讲的基础上，需要以下概念（已学过的快速回顾，未学过的在此补齐）：

- **ModelRecord 的三类产物**（u1-l1）：WebLLM 把「一个可运行的模型」拆成权重（`model`，HuggingFace 仓库）、模型库（`model_lib`，wasm 文件）和运行时覆盖（`overrides`）。本讲深入到每个字段。
- **`mlc-chat-config.json`**：每个 MLC 模型仓库根目录下的一个 JSON 文件，描述该模型的对话模板、上下文窗口大小、采样默认值等。WebLLM 加载模型时第一个 fetch 的就是它。运行时它被解析成 `ChatConfig` 类型的对象。
- **TypeScript 的 `Partial<T>`**：把接口的所有字段变成可选。`ChatOptions` 就是 `Partial<ChatConfig>`，意味着「ChatConfig 的任意子集」，这正是「覆盖」二字的类型学基础。
- **对象展开合并**：`{ ...a, ...b }` 会用 `b` 的字段覆盖 `a` 中的同名字段。WebLLM 的三层配置合并完全依赖这一个语法。
- **HuggingFace 仓库 URL 的约定**：如 `https://huggingface.co/mlc-ai/Llama-3.2-1B-Instruct-q4f32_1-MLC` 指向一个模型权重仓库；WebLLM 会自动补全 `resolve/main/` 分支路径（见 4.1.3）。
- **量化后缀**（u1-l1 回顾）：`q4f16_1` 表示权重 4 bit、激活 16 bit；带 `shader-f16` 需求的模型要求 GPU 支持半精度 shader。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/config.ts` | 全部配置类型定义 + 预置模型清单 | `ModelRecord`、`AppConfig`、`ChatConfig`、`ChatOptions`、`prebuiltAppConfig` |
| `src/engine.ts` | 引擎编排，消费配置完成模型加载 | `reloadInternal()` 如何逐字段使用 `ModelRecord` |
| `src/support.ts` | 查找模型记录、清洗 URL 等工具函数 | `findModelRecord()`、`cleanModelUrl()` |
| `src/cache_util.ts` | 缓存工具 | `getCacheOptions()` 如何消费 `AppConfig` 的缓存字段 |
| `src/index.ts` | 库入口（barrel file，u1-l3） | 哪些配置类型被导出为公开 API |
| `src/llm_chat.ts` | 推理管线 | `context_window_size` 最终在哪被使用、何时报错 |
| `README.md` | 项目文档 | Cache Backend Policy、Custom Models 章节 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 ModelRecord 接口**、**4.2 AppConfig 与 prebuiltAppConfig**、**4.3 ChatOptions 覆盖机制**。三者关系可以先用一句话串起来：

> `AppConfig.model_list` 是一个 `ModelRecord` 数组（清单里写什么才能加载什么）；每条 `ModelRecord` 又通过 `overrides: ChatOptions` 携带对默认配置的覆盖。

### 4.1 ModelRecord 接口：一个模型的「身份证」

#### 4.1.1 概念说明

在第 1 讲我们知道一个模型由权重、wasm 库、覆盖三部分组成。`ModelRecord` 就是把这三部分**外加运行门槛信息**固化成一个 TypeScript 接口——它回答了引擎加载一个模型前需要的全部元数据问题：

- 权重从哪下载？（`model`）
- 这条记录叫什么名字？（`model_id`，用户传给 `CreateMLCEngine` 的字符串就是它）
- 用哪个 wasm 模型库？（`model_lib`）
- 要覆盖默认配置的哪些字段？（`overrides`）
- 需要多少显存、是否需要特定 GPU 特性？（`vram_required_MB`、`required_features`）
- 这个模型是干什么的：聊天、向量嵌入还是看图？（`model_type`）
- 下载的文件要不要校验哈希防篡改？（`integrity`）

一个关键直觉：**`model_id` 是逻辑名，`model` 与 `model_lib` 是物理地址，两者可以自由组合**。同一份权重配不同 wasm、或同一份 wasm 配不同上下文配置，就是不同的 `model_id`。4.1.3 会用真实记录展示这一点。

#### 4.1.2 核心流程

`ModelRecord` 的每个字段在 `MLCEngine.reloadInternal()` 中按以下顺序被消费（这是「模型分发」的主链路）：

```text
reloadInternal(modelId)
  │
  ├─ 1. findModelRecord(modelId, appConfig)     ← 在 model_list 里按 model_id 查找，找不到抛 ModelNotFoundError
  ├─ 2. cleanModelUrl(record.model)             ← 补全 HF 仓库的 resolve/main/ 路径
  ├─ 3. 下载 {modelUrl}/mlc-chat-config.json    ← 得到 ChatConfig 基础值
  ├─ 4. 校验 integrity.config（可选）            ← SRI 哈希校验
  ├─ 5. 合并 ChatConfig = {...下载的配置, ...record.overrides, ...chatOpts}
  ├─ 6. 下载 record.model_lib 指向的 wasm        ← 缺失抛 MissingModelWasmError
  ├─ 7. 检测 GPU，逐项检查 record.required_features ← 缺 shader-f16 抛 ShaderF16SupportError
  ├─ 8. 从 {modelUrl} 下载权重张量（tensor cache）
  └─ 9. 按 record.model_type 选择管线            ← embedding → EmbeddingPipeline，否则 LLMChatPipeline
```

注意 `vram_required_MB` 和 `low_resource_required` **不参与**加载流程的判定——它们是给开发者做选型参考的「标注性字段」（README 与 mlc.ai/models 页面据此展示），引擎本身不做显存预检。

#### 4.1.3 源码精读

**接口定义**（[src/config.ts:L275-L286](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L275-L286)）：

```ts
export interface ModelRecord {
  model: string;                       // HuggingFace 权重仓库地址
  model_id: string;                    // 这条记录的名字，用户加载时传入的字符串
  model_lib: string;                   // wasm 模型库下载地址
  overrides?: ChatOptions;             // 覆盖 mlc-chat-config.json 的部分字段
  vram_required_MB?: number;           // 预计所需显存（MB），选型参考
  low_resource_required?: boolean;     // 能否跑在低配设备（如安卓手机）
  buffer_size_required_bytes?: number; // 需要的 maxStorageBufferBindingSize
  required_features?: Array<string>;   // 需要的 WebGPU 特性，如 "shader-f16"
  model_type?: ModelType;              // LLM / embedding / VLM，缺省为 LLM
  integrity?: ModelIntegrity;          // 可选的 SRI 哈希，校验下载产物
}
```

每个字段上方都有官方注释，见 [src/config.ts:L257-L274](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L257-L274) 的文档块，其中 `model` 字段注明了接受的四种 URL 格式（带不带末尾斜杠、带不带 `resolve/{BRANCH}`）。

**`model_type` 枚举**（[src/config.ts:L251-L255](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L251-L255)）：只有 `LLM`、`embedding`、`VLM`（视觉语言模型）三个值。缺省时的兜底逻辑在引擎里（见下）。

**引擎侧的消费**（[src/engine.ts:L256-L268](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L256-L268)）——查找记录并把 `model` 清洗成合法 URL，`model_type` 为空时默认 `LLM`：

```ts
const modelRecord = findModelRecord(modelId, this.appConfig);
let modelUrl = cleanModelUrl(modelRecord.model);
const modelType =
  modelRecord.model_type === undefined || modelRecord.model_type === null
    ? ModelType.LLM
    : modelRecord.model_type;
```

`findModelRecord` 本体非常直白（[src/support.ts:L208-L217](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L208-L217)）：在 `appConfig.model_list` 里线性查找 `model_id` 完全相等的记录，找不到抛 `ModelNotFoundError`。`cleanModelUrl`（[src/support.ts:L91-L97](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L91-L97)）把 `.../MODEL` 补成 `.../MODEL/resolve/main/`。

**`model_lib` 的消费**（[src/engine.ts:L305-L324](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L305-L324)）：`model_lib` 为空直接抛 `MissingModelWasmError`；URL 含 `localhost` 或是相对路径时不走缓存直接 fetch（方便本地频繁更新 wasm），否则经 `webllm/wasm` 缓存作用域下载。

**`required_features` 的消费**（[src/engine.ts:L358-L367](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L358-L367)）：GPU 检测完成后逐项检查 WebGPU device 的 features 集合，缺 `shader-f16` 抛 `ShaderF16SupportError`，缺其他特性抛通用的 `FeatureSupportError`——这就是第 2 讂「q4f16_1 模型在老设备上报错」的源头。

**`model_type` 的消费**（[src/engine.ts:L399-L412](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L399-L412)）：`embedding` 类型创建 `EmbeddingPipeline`，其余（含 VLM）创建 `LLMChatPipeline`。

下面用清单里的真实记录做对照（后两条在 4.2 统一精读 `prebuiltAppConfig` 时给出链接上下文）：

**记录 1：最简形态**（清单第一条，[src/config.ts:L358-L370](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L358-L370)）——Llama-3.2-1B q4f32_1，只用了 6 个字段：

```ts
{
  model: "https://huggingface.co/mlc-ai/Llama-3.2-1B-Instruct-q4f32_1-MLC",
  model_id: "Llama-3.2-1B-Instruct-q4f32_1-MLC",
  model_lib: modelLibURLPrefix + modelVersion +
    "/Llama-3.2-1B-Instruct-q4f32_1_cs1k-webgpu.wasm",
  vram_required_MB: 1128.82,
  low_resource_required: true,
  overrides: { context_window_size: 4096 },
}
```

**记录 2：要求 shader-f16**（[src/config.ts:L683-L698](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L683-L698)）——Hermes-2-Pro-Mistral-7B q4f16_1，多了 `required_features: ["shader-f16"]`，且 `overrides` 同时覆盖了 `context_window_size` 和 `sliding_window_size: -1`（-1 表示不启用滑动窗口）。

**记录 3：同一份权重、不同记录**——对比 [src/config.ts:L700-L712](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L700-L712) 的 `Phi-3.5-mini-instruct-q4f16_1-MLC`（上下文 4096，vram 3672.07MB）和 [src/config.ts:L726-L738](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L726-L738) 的 `Phi-3.5-mini-instruct-q4f16_1-MLC-1k`（上下文 1024，vram 2520.07MB）。两条记录的 `model` 和 `model_lib` **完全相同**，仅靠 `overrides.context_window_size` 区分出「完整版」和「省显存版」。这直接印证了 4.1.1 的论断：`model_id` 是逻辑名，物理产物可以复用。

**记录 4：VLM 与 embedding**——[src/config.ts:L753-L767](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L753-L767) 的 Phi-3.5-vision-instruct 标注 `model_type: ModelType.VLM`；清单末尾的嵌入模型（[src/config.ts:L2560-L2601](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L2560-L2601)）标注 `model_type: ModelType.embedding`，且同一份权重按 wasm 的 batch size 拆成 `-b32` / `-b4` 两条记录（如 `snowflake-arctic-embed-s-q0f32-MLC-b32` 需 1022.82MB，`-b4` 只需 238.71MB）——注释写明 `-b` 是 wasm 允许的最大批大小，越小越省内存。

**`integrity` 字段**：本讲只认识它是「可选的 SRI 哈希」，完整机制在第 4 单元 u4-l3 专门讲解。

#### 4.1.4 代码实践

**实践目标**：把一条 `ModelRecord` 的每个字段与真实值对上号，形成自己的字段速查笔记。

**操作步骤**：

1. 打开 [src/config.ts:L354](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L354) 定位 `prebuiltAppConfig`，任选一条含 `required_features` 的记录（例如 L683 的 Hermes-2-Pro-Mistral-7B）。
2. 把这条记录复制到自己的笔记里，逐字段写中文注释（字段含义参考 4.1.3 的表格与官方注释）。
3. 用浏览器打开这条记录的 `model` 地址，找到仓库根目录的 `mlc-chat-config.json`，对比其中 `context_window_size` 的值与记录里 `overrides.context_window_size` 的值——你大概率会发现两者不同，这正是 override 存在的意义（HF 仓库里的默认值被 WebLLM 清单改写了）。
4. 再把 `model_lib` 的 URL（`modelLibURLPrefix + modelVersion + 文件名`）拼出来直接访问，确认 wasm 文件真实存在。

**需要观察的现象**：`mlc-chat-config.json` 中的 `context_window_size` 与 `overrides` 中的值不一致；wasm URL 可直接下载。

**预期结果**：得到一份逐字段注释的 `ModelRecord` 笔记，并直观理解「清单里的 overrides 会改写仓库默认值」。步骤 3、4 的具体取值依赖网络与仓库内容，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`model` 和 `model_lib` 可以分别指向完全不同的仓库吗？举一个清单中的真实例子。

答案：可以。`model` 是权重地址、`model_lib` 是 wasm 地址，互不绑定。例如 4.1.3 记录 2：权重在 `mlc-ai/Hermes-2-Pro-Mistral-7B-q4f16_1-MLC` 仓库，wasm 却是 `Mistral-7B-Instruct-v0.3-q4f16_1_cs1k-webgpu.wasm`（同架构模型复用编译产物）。

**练习 2**：为什么 `vram_required_MB` 超出显卡显存时，引擎不会在加载前报错，而是加载途中可能崩溃？

答案：`vram_required_MB` 只是选型参考字段，`reloadInternal()` 的流程里没有任何一步读取它做预检；显存真正耗尽时 WebGPU device 会 lost，触发 [src/engine.ts:L369-L384](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L369-L384) 的 `device.lost` 回调，日志会提示换更小的模型。

**练习 3**：`model_type` 不写会怎样？

答案：引擎在 [src/engine.ts:L265-L268](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L265-L268) 把 `undefined`/`null` 兜底为 `ModelType.LLM`，随后按普通聊天管线加载。

### 4.2 AppConfig 与 prebuiltAppConfig：模型清单与缓存后端

#### 4.2.1 概念说明

`AppConfig` 回答的是引擎级别的两个问题：

1. **允许加载哪些模型？**（`model_list`：`ModelRecord` 数组——这是白名单，不在清单里的 `model_id` 一律 `ModelNotFoundError`）
2. **模型文件缓存到浏览器的哪里？**（`cacheBackend`：Cache API / IndexedDB / OPFS / cross-origin 四选一，外加 OPFS 专属的 `opfsAccessMode`）

`prebuiltAppConfig` 则是 WebLLM 官方维护的默认 `AppConfig`：`cacheBackend: "cache"` 加一份 163 条记录的 `model_list`（按当前 HEAD 统计）。它的特殊地位来自源码注释里的这句话——「当前 npm 版本与哪些预编译模型库兼容，唯一事实来源就是它」（[src/config.ts:L348-L353](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L348-L353)）。配套常量 `modelVersion = "v0_2_84/base"`（[src/config.ts:L333](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L333)）拼进每条 `model_lib` 的 URL——npm 版本升级不一定要换模型库，只有 `modelVersion` 变了才要重新下载 wasm。

用户传入自定义 `appConfig` 时通常是「改造」而非「从零造」：`{ ...prebuiltAppConfig, model_list: [我的模型们] }` 或只改 `cacheBackend`。

#### 4.2.2 核心流程

`AppConfig` 在引擎中的完整生命周期：

```text
CreateMLCEngine(modelId, engineConfig)
  └─ new MLCEngine(engineConfig)
       └─ this.appConfig = engineConfig?.appConfig || prebuiltAppConfig   ← 不传就用默认清单
            │
            ├─ reload(modelId) → findModelRecord(modelId, this.appConfig)  ← 查 model_list（白名单）
            ├─ createArtifactCache("webllm/config", getCacheOptions(appConfig))  ← 读 cacheBackend
            ├─ createArtifactCache("webllm/wasm",  getCacheOptions(appConfig))
            └─ fetchTensorCache(modelUrl, {...getTensorCacheAccessOptions("webllm/model", appConfig)})
```

三个缓存作用域（`webllm/config`、`webllm/wasm`、`webllm/model`，第 2 讲已在 DevTools 里见过）全部由同一个 `getCacheOptions(appConfig)` 决定后端。

#### 4.2.3 源码精读

**类型定义**（[src/config.ts:L288-L317](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L288-L317)）：

```ts
export type CacheBackend = "cache" | "indexeddb" | "cross-origin" | "opfs";
export type OPFSAccessMode = "async" | "sync" | "auto";

export interface AppConfig {
  model_list: Array<ModelRecord>;   // 模型白名单
  cacheBackend?: CacheBackend;      // 缓存后端，缺省 "cache"
  opfsAccessMode?: OPFSAccessMode;  // 仅 cacheBackend 为 "opfs" 时生效
}
```

注释明确提醒：Cache API 是目前测试最充分的后端。`getCacheBackend()`（[src/config.ts:L319-L324](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L319-L324)）实现缺省值兜底。

**默认值的注入点**（[src/engine.ts:L150-L166](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L150-L166)）：构造函数第 158 行 `this.appConfig = engineConfig?.appConfig || prebuiltAppConfig;`——这就是「不传 appConfig 也能加载 Llama」的原因。此外引擎还提供运行时替换入口 `setAppConfig()`（[src/engine.ts:L172-L174](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L172-L174)），更换清单后需重新 `reload()` 才生效。

**prebuiltAppConfig 开头**（[src/config.ts:L354-L370](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L354-L370)）：先声明 `cacheBackend: "cache"`，然后以注释分家族（`// Llama-3.2`、`// Phi-3.5-mini-instruct`……）罗列 163 条记录直到文件末尾（[src/config.ts:L2602-L2603](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L2602-L2603)）。`modelLibURLPrefix`（[src/config.ts:L334-L335](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L334-L335)）指向 GitHub 上的 wasm 仓库 `mlc-ai/binary-mlc-llm-libs`。

**缓存侧的消费**（[src/cache_util.ts:L20-L28](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/cache_util.ts#L20-L28)）：`getCacheOptions` 把 `cacheBackend` 映射为 tvmjs 的 `cacheType`，`opfsAccessMode` 有值时透传。README 的 Cache Backend Policy 章节（[README.md:L157-L181](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/README.md#L157-L181)）给出了切换后端的官方示例：`{ ...prebuiltAppConfig, cacheBackend: "cross-origin" }`。

**公开 API 层面**：`index.ts` 把 `ModelRecord`、`AppConfig`、`ChatOptions`、`prebuiltAppConfig`、`modelVersion` 等全部导出（[src/index.ts:L1-L13](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/index.ts#L1-L13)），所以你在页面里能直接 `import { prebuiltAppConfig } from "@mlc-ai/web-llm"` 拿到默认清单做裁剪。

#### 4.2.4 代码实践

**实践目标**：验证 `model_list` 的白名单语义——自定义 `appConfig` 后，不在清单里的模型无法加载。

**操作步骤**（以下为**示例代码**，可基于第 2 讲的 get-started 工程修改）：

1. 在页面脚本中构造只含一个模型的清单：

```ts
import { CreateMLCEngine, prebuiltAppConfig, type AppConfig } from "@mlc-ai/web-llm";

const myAppConfig: AppConfig = {
  ...prebuiltAppConfig,          // 复用默认缓存配置
  model_list: [
    prebuiltAppConfig.model_list[0],   // 只保留 Llama-3.2-1B-Instruct-q4f32_1-MLC
  ],
};

const engine = await CreateMLCEngine("Llama-3.2-1B-Instruct-q4f32_1-MLC", {
  appConfig: myAppConfig,
});
```

2. 正常发起一次对话，确认加载成功。
3. 再调用 `await engine.reload("Qwen2.5-1.5B-Instruct-q4f16_1-MLC")`（一个不在你的清单里的模型）。
4. 打开控制台观察报错。

**需要观察的现象**：步骤 2 正常工作；步骤 3 抛出 `ModelNotFoundError`，错误信息包含你传入的 modelId。

**预期结果**：证明引擎只认 `this.appConfig.model_list` 里登记过的 `model_id`，`prebuiltAppConfig` 被完全替换而非合并。具体错误文案**待本地验证**（可对照 [src/support.ts:L216](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L216) 的抛出点）。

#### 4.2.5 小练习与答案

**练习 1**：`appConfig.model_list` 传空数组 `[]` 会发生什么？

答案：构造引擎不会报错（`appConfig` 只是存进字段），但任何 `reload(modelId)` 都会在 `findModelRecord` 里抛 `ModelNotFoundError`——因为白名单为空。

**练习 2**：为什么 README 推荐用 `{ ...prebuiltAppConfig, cacheBackend: "opfs" }` 而不是自己重写一个 `AppConfig`？

答案：展开运算符保留了官方维护的 163 条 `model_list`（含与当前 npm 版本匹配的 `model_lib` URL），只改缓存后端；自己重写容易漏掉 `modelLibURLPrefix`/`modelVersion` 的拼接约定，导致 wasm 地址失效。

**练习 3**：`setAppConfig()` 换清单后，已加载的模型会立刻失效吗？

答案：不会。`setAppConfig` 只赋值字段（[src/engine.ts:L172-L174](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L172-L174)），已创建的 pipeline 仍在内存中可继续对话；新清单只影响下一次 `reload()` 的查找与缓存选择。

### 4.3 ChatOptions 覆盖机制：三层配置合并的最后一公里

#### 4.3.1 概念说明

`ChatOptions` 的定义只有一行，却是整个配置体系的关键扩展点（[src/config.ts:L114-L118](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L114-L118)）：

```ts
export interface ChatOptions extends Partial<ChatConfig> {}
```

即「`ChatConfig` 的任意子集」。它出现在**两个注入点**：

1. `ModelRecord.overrides`——写在模型清单里，随记录固定（4.1 已见）；
2. `reload(modelId, chatOpts)` 的第二个参数——加载时临时传入。

要理解覆盖什么，先看被覆盖的对象 `ChatConfig`（[src/config.ts:L78-L112](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L78-L112)），它就是 `mlc-chat-config.json` 的运行时形态，字段按影响范围分三组：

| 分组 | 代表字段 | 说明 |
| --- | --- | --- |
| 影响整场对话 | `tokenizer_files`、`conv_config`、`conv_template` | 分词器文件与对话模板（第 3 单元 u3-l2 精读） |
| KV cache 设置 | `context_window_size`、`sliding_window_size`、`attention_sink_size` | 决定 KV cache 大小与上下文上限 |
| 采样默认值 | `temperature`、`top_p`、`frequency_penalty`、`presence_penalty`、`repetition_penalty` | 每次生成的默认采样参数 |

注意区分两个容易混淆的概念：`ChatOptions` 是**加载期**配置（改的是模型的常驻配置），而 `GenerationConfig`（[src/config.ts:L137-L165](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L137-L165)）是**每次请求**的采样参数（`max_tokens`、`stop` 等，发起请求时传入）。前者本讲讲，后者在 u3-l5 展开。

#### 4.3.2 核心流程

三层合并发生在 `reloadInternal()` 中，一行代码完成：

```text
最终 ChatConfig = { ...JSON.parse(mlc-chat-config.json),   ← 第 1 层：仓库默认值
                    ...modelRecord.overrides,               ← 第 2 层：清单里的记录级覆盖
                    ...chatOpts }                           ← 第 3 层：reload() 调用级覆盖
```

用伪代码表述优先级：**同名字段后写的赢，即 `chatOpts` > `overrides` > 仓库默认值**。合并结果存入 `loadedModelIdToChatConfig`，随后作为构造参数传给管线，管线据此分配 KV cache、初始化对话模板。

#### 4.3.3 源码精读

**合并点**（[src/engine.ts:L278-L297](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L278-L297)）：

```ts
const configUrl = new URL("mlc-chat-config.json", modelUrl).href;
const configData = await configCache.fetchWithCache(configUrl, "arraybuffer", ...);
// ...（integrity.config 校验）
const curModelConfig: ChatConfig = {
  ...JSON.parse(new TextDecoder().decode(configData)),  // 第 1 层
  ...modelRecord.overrides,                             // 第 2 层
  ...chatOpts,                                          // 第 3 层
} as ChatConfig;
```

**`chatOpts` 的入口**（[src/engine.ts:L203-L226](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L203-L226)）：`reload()` 接受单个或数组形式的 `ChatOptions`（多模型加载时与 modelId 一一对应），并校验数量匹配与 modelId 唯一性。

**下游消费点（以 `context_window_size` 为例）**：合并后的配置传入 `LLMChatPipeline`，构造函数读取它（[src/llm_chat.ts:L361](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L361) `this.contextWindowSize = config.context_window_size;`），用于决定 KV cache 容量；生成时若 prompt token 数加已填 KV 超过该值，抛 `ContextWindowSizeExceededError`（[src/llm_chat.ts:L2126-L2131](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2126-L2131)，注意滑动窗口模型不受此限制）。**这个报错就是我们验证「覆盖是否生效」的可观察信号**。

**清单中的真实用例回顾**：4.1.3 记录 3 里 `-1k` 变体就是靠 `overrides: { context_window_size: 1024 }` 把 Phi-3.5-mini 从 4096 砍到 1024，省出约 1.1GB 显存；记录 2 里 `sliding_window_size: -1` 关闭滑动窗口。它们都是第 2 层覆盖的官方示范。

#### 4.3.4 代码实践

**实践目标**：通过「把上下文窗口改小 → 长_prompt 触发超限报错」的因果链，证明 `ChatOptions` 覆盖真实生效。

**操作步骤**（**示例代码**，基于第 2 讲的 get-started 工程）：

1. 构造带覆盖的记录并加载（这里用 `chatOpts` 参数演示第 3 层覆盖）：

```ts
import { CreateMLCEngine, prebuiltAppConfig } from "@mlc-ai/web-llm";

const engine = await CreateMLCEngine("Llama-3.2-1B-Instruct-q4f32_1-MLC", {
  appConfig: prebuiltAppConfig,
}, {
  context_window_size: 512,   // ChatOptions：把默认 4096 压到 512
});
```

2. 先调用 `await engine.getMessage()`，记录返回值。
3. 发一条很长的消息（比如粘贴几段重复文本，确保超过 512 token），观察报错。
4. 把 `context_window_size` 改回 4096（或删掉该参数）重复步骤 3，对比行为。

**需要观察的现象**：

- 步骤 2：刚加载完、尚未对话时 `getMessage()` 返回空字符串——它只是管线内部 `outputMessage` 的读取器，初始值为 `""`（[src/llm_chat.ts:L110](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L110)、[src/llm_chat.ts:L513-L515](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L513-L515)），完成一次生成后才返回模型的完整回复。
- 步骤 3：抛 `ContextWindowSizeExceededError`，说明 512 的覆盖已经生效（KV cache 按新值分配）。
- 步骤 4：同样的长消息不再立刻报错。

**预期结果**：覆盖值越小、能容纳的对话越短；报错与否随 `context_window_size` 变化。具体触发所需的 prompt 长度与分词器有关，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`ModelRecord.overrides` 和 `reload` 的 `chatOpts` 都能覆盖 `temperature`，哪个优先？加载后还能改吗？

答案：`chatOpts` 优先（合并顺序在后）。加载后不能再用这两个入口改——它们只在 `reloadInternal` 时生效；要按请求调采样参数应使用请求里的字段（`GenerationConfig` 体系，u3-l5 详解），或重新 `reload`。

**练习 2**：如果把 `context_window_size` 设成比模型预置值更大的数（如把 1B 模型设为 32768），会发生什么？

答案：引擎不会拦截你，KV cache 会按 32768 分配，显存占用随之暴涨，很可能在加载或首次分配时触发 WebGPU device lost（[src/engine.ts:L369-L384](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L369-L384)）。`overrides` 是「信任开发者」的机制，清单中的 `vram_required_MB` 只是参考标注。

**练习 3**：`ChatOptions` 能覆盖 `tokenizer_files` 吗？

答案：类型上可以（它是 `Partial<ChatConfig>`，`tokenizer_files` 是 `ChatConfig` 字段），合并发生在 tokenizer 加载之前（[src/engine.ts:L292-L297](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L292-L297) 先合并、[src/engine.ts:L387-L393](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L387-L393) 后加载 tokenizer），所以传入的相对路径文件名会基于 `modelUrl` 去仓库里取。

## 5. 综合实践

把三个最小模块串成一个「单模型定制加载」小任务（**示例代码**）：

**任务**：做一个页面，它只允许加载一个模型，且该模型的上下文窗口被你定制；页面要能证明配置确实生效。

**步骤**：

1. **准备清单与记录注释**：从 `prebuiltAppConfig.model_list` 中挑出 `Llama-3.2-1B-Instruct-q4f32_1-MLC`，按 4.1.4 的方法逐字段注释，形成注释版 `ModelRecord`。
2. **构造单模型 appConfig 并加覆盖**（同时用上第 2 层与第 3 层覆盖，验证优先级）：

```ts
import { CreateMLCEngine, prebuiltAppConfig } from "@mlc-ai/web-llm";

const myRecord = {
  ...prebuiltAppConfig.model_list[0],
  overrides: { context_window_size: 2048 },   // 第 2 层：记录级
};
const engine = await CreateMLCEngine(myRecord.model_id, {
  appConfig: { ...prebuiltAppConfig, model_list: [myRecord] },
}, {
  context_window_size: 512,                   // 第 3 层：调用级，应压过 2048
});
```

3. **验证一（getMessage 行为）**：加载完成后立即 `console.log(await engine.getMessage())`，应得空字符串；完成一次对话后再调用，应返回模型本轮完整回复。
4. **验证二（覆盖生效）**：发一条超过 512 token 的长消息，预期抛 `ContextWindowSizeExceededError`；去掉 `context_window_size: 512` 重新加载，同样长的消息应能正常生成。
5. **验证三（白名单）**：调用 `engine.reload()` 加载一个不在 `myAppConfig.model_list` 里的 modelId，预期抛 `ModelNotFoundError`。
6. 把三次验证的截图/控制台输出连同注释版记录整理成实验报告。

**预期结果**：三项验证全部与源码行为一致，即 512 覆盖 2048（后写的赢）、白名单外模型被拒、`getMessage()` 反映管线当前消息。若某项与预期不符，回到 [src/engine.ts:L292-L296](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L292-L296) 与 [src/support.ts:L208-L217](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L208-L217) 对照排查。运行结果依赖本机浏览器与网络，**待本地验证**。

## 6. 本讲小结

- `ModelRecord` 是一个模型的身份证：`model_id` 是逻辑名，`model`（权重仓库）与 `model_lib`（wasm 库）是可复用的物理地址，`overrides` 携带配置覆盖，`required_features` / `model_type` / `integrity` 声明运行要求；`vram_required_MB` 只是选型参考，引擎不预检。
- 同一份权重 + 不同 `overrides`/wasm 就是不同的 `model_id`（如 Phi-3.5-mini 的 4096 版与 `-1k` 版、embedding 的 `-b32`/`-b4` 版）。
- `AppConfig = model_list（白名单） + cacheBackend/opfsAccessMode（缓存后端）`；不传时引擎兜底用 `prebuiltAppConfig`（163 条记录），它是预置模型与当前 npm 版本兼容性的唯一事实来源。
- `ChatOptions` 就是 `Partial<ChatConfig>`，通过「仓库 `mlc-chat-config.json` → `ModelRecord.overrides` → `reload` 的 `chatOpts`」三层展开合并，后写的赢。
- `context_window_size` 覆盖的生效可以从 `ContextWindowSizeExceededError` 是否触发来观察；`getMessage()` 读取管线当前输出，加载后未生成时为空字符串。

## 7. 下一步学习建议

本讲结束了第 1 单元（入门层）。接下来进入第 2 单元「引擎接口层」，建议顺序：

1. **u2-l1（引擎生命周期）**：本讲只看了 `reloadInternal` 中消费 `ModelRecord` 的片段，下一讲完整走读 `reload → unload → reload` 的生命周期与 `MLCEngineInterface` 抽象。
2. 提前翻阅 [README.md:L397-L445](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/README.md#L397-L445) 的 Custom Models 章节——它是本讲 `appConfig` 知识向「接入自己编译的模型」的自然延伸（完整实战在第 4 单元 u4-l4）。
3. 若对缓存后端四种取值的差异好奇，可预习 `src/cache_util.ts` 全文，第 4 单元 u4-l1 会精读。
