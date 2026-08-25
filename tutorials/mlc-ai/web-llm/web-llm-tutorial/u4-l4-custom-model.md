# 接入自定义模型：从 MLC 编译到 appConfig

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清「给 WebLLM 新增一个模型」需要准备哪两类产物（权重目录与 wasm 模型库），以及它们分别由 `ModelRecord` 的哪个字段指向。
2. 掌握用 `mlc_llm` 的 `convert_weight` / `gen_config` / `compile` 三条命令产出 WebGPU 产物的要点，并判断自己的模型走「变体复用库」还是「全新编译库」哪条路线。
3. 理解 `model_id` 只是一个逻辑名，它与 `model` / `model_lib` URL 的对应规则由 `findModelRecord` 与 `cleanModelUrl` 决定；同架构 + 同量化的模型可以复用同一个 wasm。
4. 学会构造自定义 `AppConfig` 交给 `CreateMLCEngine`，并用 `overrides` 覆盖 `context_window_size`、`conv_config`（对话模板微调）等元数据，同时明白覆盖是「浅合并、后写者赢」。

本讲是第四单元的收官篇：前面三讲分别搞清楚了配置体系（u1-l4）、缓存机制（u4-l1/u4-l2）与完整性校验（u4-l3），本讲把它们串成一条「从编译产物到浏览器里跑起来」的完整链路。

## 2. 前置知识

### 2.1 回顾：一个模型由三类产物描述

在 u1-l4 我们拆过 `ModelRecord`：WebLLM 眼里「一个模型」= **model**（HuggingFace 权重仓库 URL，内含 `mlc-chat-config.json`、tokenizer、量化权重分片）+ **model_lib**（wasm 模型库 URL，内含 prefill/decode 等计算内核）+ **overrides**（运行时元数据覆盖）。本讲要回答的问题是：这两个 URL 指向的东西从哪里来、怎么自己做一份。

### 2.2 回顾：ChatConfig 的三层合并

u1-l4 讲过，`reload()` 时最终的 `ChatConfig` 由三层展开：仓库里的 `mlc-chat-config.json` → 记录级 `overrides` → `reload` 传入的 `chatOpts`，后写者赢。本讲 4.3 节会用源码再确认一次合并位置，并补充一个重要细节：这是**浅合并**，覆盖 `conv_template` 这类对象字段时必须整体替换。

### 2.3 回顾：缓存与 appConfig 的身份

u4-l1/u4-l2 讲过 `AppConfig` 不只携带 `model_list`，还携带 `cacheBackend` 等缓存后端配置；查、删、载必须使用同一个 appConfig 才能对上缓存键。本讲自定义 appConfig 时要记得这一点。

### 2.4 新术语

| 术语 | 通俗解释 |
| --- | --- |
| MLC LLM | 一个模型编译引擎：把 HuggingFace 上的 PyTorch 权重转换/量化成各平台（CUDA、iOS、WebGPU…）可高效执行的格式。WebLLM 是它在浏览器端的「伴侣库」，自身**不做编译**。 |
| wasm 模型库（model library） | `mlc_llm compile --device webgpu` 的产物，一个 `.wasm` 文件，包含模型的前向计算内核。 |
| 量化方案（如 `q4f16_1`） | 权重存储位宽与激活精度的编码：`q4f16_1` = 权重 4 bit、激活 f16、方案代号 1；`q0f32` 表示不量化。 |
| `/resolve/main/` | HuggingFace 仓库文件的直链路径：`https://huggingface.co/{用户}/{仓库}/resolve/main/{文件}` 指向 main 分支的原始文件。 |
| 四要素 | MLC 文档归纳的决定一个 model library 的四个维度：模型架构、量化、metadata（如 `prefill_chunk_size`）、平台。四要素相同即可复用同一个库。 |

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| `src/config.ts` | `ChatConfig`/`ChatOptions`/`ModelRecord`/`AppConfig` 类型定义；`prebuiltAppConfig` 中的复用范例（同权重多记录、复用他架构库） |
| `src/engine.ts` | `CreateMLCEngine` 三参数；`reloadInternal` 中查记录、拼 URL、三层合并、wasm 三分支下载 |
| `src/support.ts` | `cleanModelUrl`（URL 规范化）、`findModelRecord`（model_id 查找） |
| `src/conversation.ts` | `getConversation`：`conv_template` + `conv_config` 如何合并成最终对话模板 |
| `src/cache_util.ts` | `getCacheOptions`：appConfig 中的缓存配置如何被消费（承接 u4-l1） |
| `src/error.ts` | `ModelNotFoundError`、`MissingModelWasmError`、`ContextWindowSizeExceededError` |
| `README.md` | Custom Models 章节：官方最小示例与「变体复用库」说明 |
| `examples/get-started/src/get_started.ts` | 官方示例中注释好的「Option 2：自定义 appConfig」代码块，本讲主实践的基础 |
| `examples/simple-chat-upload/src/{simple_chat.ts,gh-config.js}` | 一个用 appConfig 驱动模型下拉框的真实应用 |

外部依赖文档：[MLC LLM 的 WebLLM 部署文档](https://llm.mlc.ai/docs/deploy/webllm.html)（README 中 Custom Models 章节指向的正是它，本讲所有编译命令均出自该文档）。

## 4. 核心概念与源码讲解

本讲的三个最小模块：**4.1 mlc_llm 编译流程**、**4.2 model_lib 上传与 URL 配置**、**4.3 AppConfig 自定义注入**。

---

### 4.1 mlc_llm 编译流程

#### 4.1.1 概念说明

WebLLM 在 `reload()` 里只做「下载 + 实例化」，不做任何编译。所以想让 WebLLM 跑一个它不认识的模型，必须先在 MLC LLM 侧产出两类产物：

1. **权重目录**（上传到 HuggingFace，由 `ModelRecord.model` 指向）：包含 `mlc-chat-config.json`（模型自述文件：对话模板、上下文窗口、tokenizer 位置等）、`params_shard_*.bin`（量化权重分片）、`tokenizer.json` / `tokenizer_config.json`。
2. **wasm 模型库**（放在任意可访问 URL，由 `ModelRecord.model_lib` 指向）：`--device webgpu` 编译出的计算内核。

根据「四要素」（架构、量化、metadata、平台）是否与某个预置库相同，有两条路线：

- **路线 A：模型变体（Bring Your Own Model Variant）**。你的模型只是某个已支持模型的微调版（同架构、同量化），例如 Mistral 的领域微调版。此时只需转换权重，**复用现有的 wasm**，不用装 Wasm 编译环境。
- **路线 B：全新模型库（Bring Your Own Model Library）**。架构不在预置列表、量化格式不同、或需要不同 metadata 时，需要自己跑 `mlc_llm compile --device webgpu`，这要求从源码构建 `mlc_llm` 并安装 Wasm 构建环境，否则会报 `RuntimeError: Cannot find libraries: wasm_runtime.bc`。

#### 4.1.2 核心流程

以 MLC 文档的两个官方示例（WizardMath-7B 变体 / RedPajama-3B 全新库）为蓝本，流程如下：

```text
# 0. 前置：mlc_llm 可用（mlc_llm --help 能看到 compile/convert_weight/gen_config）
# 1. 拉取原始权重
git clone https://huggingface.co/{作者}/{模型}

# 2. 转换 + 量化权重（两条路线都要）
mlc_llm convert_weight ./dist/models/{模型}/ \
    --quantization q4f16_1 \
    -o dist/{模型}-q4f16_1-MLC

# 3. 生成 mlc-chat-config.json 并处理 tokenizer（两条路线都要）
mlc_llm gen_config ./dist/models/{模型}/ \
    --quantization q4f16_1 --conv-template {模板名} \
    -o dist/{模型}-q4f16_1-MLC/

# 4. 仅路线 B：编译 WebGPU 模型库
mlc_llm compile ./dist/{模型}-q4f16_1-MLC/mlc-chat-config.json \
    --device webgpu \
    -o dist/libs/{模型}-q4f16_1-webgpu.wasm
#   （大模型建议加 --prefill_chunk_size 1024 以降低显存占用）

# 5. 分发：权重目录上传 HuggingFace；wasm 上传 GitHub（或本地静态服务器）
# 6. 注册：把两个 URL 写进自定义 AppConfig 的 model_list
```

产出物一览（摘自 MLC 文档）：

```text
dist/libs/
  {模型}-q4f16_1-webgpu.wasm        # ===> 模型库（model_lib 指向它）
dist/{模型}-q4f16_1-MLC/
  mlc-chat-config.json               # ===> 聊天配置（reload 时第一个被读取的文件）
  tensor-cache.json                  # ===> 权重分片索引
  params_shard_0.bin, ...            # ===> 量化权重
  tokenizer.json, tokenizer_config.json
```

#### 4.1.3 源码精读

**① 官方入口：README 的 Custom Models 章节**。它明确了两要素模型观——`model` 装权重与元数据，`model_lib` 装「加速模型计算的执行程序」：

- [README.md:L404-L409](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/README.md#L404-L409)：定义 `model` 与 `model_lib` 两个可定制要素。
- [README.md:L411-L441](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/README.md#L411-L441)：官方最小自定义示例——一个只有 `model` / `model_id` / `model_lib` 三字段的 `model_list`，加可选的 `chatOpts` 覆盖。
- [README.md:L443-L446](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/README.md#L443-L446)：点出路线 A 的典型场景——「只想要新权重变体、不想要新模型」（如 NeuralHermes-Mistral 复用 Mistral 的库），并让读者参考 `webllm.prebuiltAppConfig` 里的复用写法。

**② 引擎如何消费 `mlc-chat-config.json`**。`reload()` 里第一个网络请求就是在权重仓库根下取这个文件——这正是 `gen_config` 步骤的产出：

- [src/engine.ts:L277-L296](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L277-L296)：`configUrl = new URL("mlc-chat-config.json", modelUrl)` 拼出配置地址，经 `webllm/config` 作用域缓存取回，`JSON.parse` 后与 `overrides`、`chatOpts` 合并成 `curModelConfig`。

`ChatConfig` 接口的注释直说它就是 `mlc-chat-config.json` 中「会影响会话」那部分字段的镜像（含指向 mlc.ai 配置文档的链接）：

- [src/config.ts:L77-L112](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L77-L112)：`ChatConfig` 的字段与 `mlc-chat-config.json` 一一对应——`tokenizer_files`、`conv_template`、`context_window_size`、采样默认值等。

**③ 引擎如何消费 wasm**。wasm 被 `tvmjs.instantiate` 实例化成一个完整的 TVM 运行时，后续的 prefill/decode 都是它导出的函数：

- [src/engine.ts:L299-L325](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L299-L325)：从 `modelRecord.model_lib` 取 wasm 字节（下载策略详见 4.2.3），缺失则抛 `MissingModelWasmError`。
- [src/engine.ts:L336-L345](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L336-L345)：`tvmjs.instantiate(wasm.buffer, ...)` 把字节变成运行时实例，并注册加载进度回调。

#### 4.1.4 代码实践

**实践目标**：分两种环境二选一——(A) 有 Python 环境时跑通三条编译命令的「干跑检查」；(B) 没有环境时完成源码阅读型实践，亲眼确认权重仓库里确实躺着 `mlc-chat-config.json`。

**操作步骤（A：命令干跑）**：

1. 按 MLC 文档验证工具链：`mlc_llm --help` 应列出 `{compile, convert_weight, gen_config}`；`python -c "import tvm; print(tvm.__file__)"` 应打印 tvm 路径。
2. 选一个小模型（文档示例用 7B，本讲建议 TinyLlama-1.1B 或 Qwen2-0.5B 级别，编译快），依次执行 4.1.2 的第 1–3 步。
3. 若走路线 B（需要 `--device webgpu` 编译），还需按文档安装 Wasm 构建环境；缺环境时会在 compile 阶段报 `Cannot find libraries: wasm_runtime.bc`。

**操作步骤（B：源码阅读，无需任何安装）**：

1. 在浏览器打开任一预置模型的配置文件，例如：
   `https://huggingface.co/mlc-ai/Llama-3.1-8B-Instruct-q4f32_1-MLC/resolve/main/mlc-chat-config.json`
2. 在返回的 JSON 中找到 `context_window_size`、`conv_template`（注意它的 `roles`、`seps`、`system_template` 字段）、`tokenizer_files` 三个字段。
3. 对照 [src/config.ts:L77-L112](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L77-L112) 的 `ChatConfig` 接口，给 JSON 里的每个字段在接口中找到同名对应。

**需要观察的现象**：`mlc-chat-config.json` 里的字段名与 `ChatConfig` 高度一致；`conv_template` 是一个完整对象（不是零散字段）。

**预期结果**：你能指出「`gen_config --conv-template` 写入的模板」最终变成运行时 `ChatConfig.conv_template` 的值，被 `getConversation` 消费。命令执行部分**待本地验证**（本讲义写作环境未安装 mlc_llm）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Hermes-2-Pro-Mistral-7B` 不需要自己编译 wasm？

> **答案**：它四要素与预置 `Mistral-7B-Instruct-v0.3` 库相同（同为 Mistral 架构、同为 `q4f16_1`、WebGPU 平台），所以直接把 `model_lib` 指向 Mistral 的 wasm 即可——见 4.2.3 的源码引用。这正是路线 A。

**练习 2**：`gen_config` 的 `--conv-template` 参数影响哪个文件、最终被 WebLLM 哪段代码消费？

> **答案**：写入权重目录中 `mlc-chat-config.json` 的 `conv_template` 字段；WebLLM 在 `reload()` 里下载并解析该文件（[src/engine.ts:L277-L296](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L277-L296)），之后由 `getConversation` 据此构造 `Conversation` 对象。

**练习 3**：什么情况下编译时要加 `--prefill_chunk_size 1024`？不加会怎样？

> **答案**：编译较大模型（如 Llama-3-8B）时用它降低运行时显存。不加的话，运行时可能报 `Failed to execute 'createBuffer' on 'GPUDevice' ... Value is outside the 'unsigned long long' value range`（MLC 文档原文）。回顾 u3-l3：prefill 块大小由 wasm 固化，运行期不可改。

---

### 4.2 model_lib 上传与 URL 配置

#### 4.2.1 概念说明

编译完成后，产物要放到浏览器能 `fetch` 到的 URL 上，然后写进 `ModelRecord`。这里有三条规则：

1. **`model_id` 只是逻辑名**。引擎查找时拿 `model_id` 在 `appConfig.model_list` 里线性匹配，与 URL 内容没有任何绑定；同一个权重配不同 wasm、或只改 overrides，都可以注册成新的 `model_id`。
2. **`model` 接受四种 HuggingFace URL 格式**（带不带尾斜杠、带不带 `/resolve/{分支}`），`cleanModelUrl` 会统一补全为 `.../resolve/main/` 形式。权重目录也可以不用 HF，只要目标 URL 下有同样的文件布局即可。
3. **`model_lib` 可以是任何 URL**：官方预置库放在 GitHub 的 `binary-mlc-llm-libs` 仓库（`raw.githubusercontent.com` 直链），自定义库可以放 GitHub、自己的 CDN，或本地开发服务器。

官方库的 URL 由 `modelLibURLPrefix + modelVersion` 拼出。注意 `modelVersion` 与 npm 包版本是两个独立编号——不是每次 npm 升级都换模型库，它是「当前 npm 兼容哪些预置库」的唯一事实来源。

#### 4.2.2 核心流程

`reloadInternal()` 里与 URL 相关的处理顺序：

```text
findModelRecord(modelId, appConfig)          # 按 model_id 在 model_list 中查找
        │  找不到 → ModelNotFoundError
        ▼
cleanModelUrl(modelRecord.model)             # 补尾斜杠；无 /resolve/<分支>/ 则补 resolve/main/
        ▼
非 http 开头 ? → 再用页面 URL 作 base 解析    # engine.ts:L262-L264
        ▼
modelUrl 派生出三个下载地址：
  - mlc-chat-config.json  → webllm/config 缓存作用域
  - tokenizer 文件         → 经 ChatConfig.tokenizer_files 定位
  - 权重分片               → webllm/model 张量缓存
model_lib 则单独三分支下载（见 4.2.3 ③）
```

#### 4.2.3 源码精读

**① `cleanModelUrl`：URL 规范化**。

- [src/support.ts:L86-L97](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L86-L97)：先保证以 `/` 结尾；若不匹配正则 `/.+\/resolve\/.+\//` 则追加 `resolve/main/`；最后经 `new URL(...)` 规范化。这就是「四种 HF 格式」等价的实现。注意一个推论：若你想用本地静态服务器托管权重目录，要么让 URL 显式包含 `/resolve/main/` 路径，要么按 `<模型目录>/resolve/main/` 的结构摆放文件，否则引擎会去不存在的路径下找 `mlc-chat-config.json`（此推论基于源码逻辑，具体表现**待本地验证**）。

**② `findModelRecord`：model_id 的全部含义**。

- [src/support.ts:L208-L217](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L208-L217)：`appConfig.model_list.find(item => item.model_id == modelId)`，找不到抛 `ModelNotFoundError`（[src/error.ts:L1](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L1)）。整个查找就这么几行——`model_id` 与 URL 之间没有任何校验。

- [src/engine.ts:L255-L269](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L255-L269)：`reloadInternal` 开头完成查记录、清洗 URL、解析非 http 地址、登记 model_type（缺省 `ModelType.LLM`）。

**③ wasm 下载的三分支**。

- [src/engine.ts:L299-L325](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L299-L325)：`model_lib` 缺失抛 `MissingModelWasmError`（[src/error.ts:L230](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L230)）；随后三个分支——URL 含 `localhost` 则**直接 fetch 不缓存**（源码注释：本地开发时代码更新频繁）；非 http 的相对路径则基于页面 URL 解析后同样不缓存；其余 https 走 `webllm/wasm` 缓存作用域。这意味着把 wasm 与页面放在同一个开发服务器上是官方支持的本地开发姿势。

**④ 官方 URL 前缀与版本常量**。

- [src/config.ts:L326-L335](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L326-L335)：`modelVersion = "v0_2_84/base"` 与 `modelLibURLPrefix` 指向 `binary-mlc-llm-libs` 仓库；注释明确「model version 不必与 npm 版本一致」。

**⑤ `prebuiltAppConfig` 里的三个复用范例**（自定义模型时最好的模仿对象）：

- [src/config.ts:L517-L530](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L517-L530)：`DeepSeek-R1-Distill-Qwen-7B`（Qwen2 架构蒸馏）直接复用 `Qwen2-7B-Instruct` 的 wasm——路线 A 的官方示范。
- [src/config.ts:L683-L698](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L683-L698)：`Hermes-2-Pro-Mistral-7B` 复用 `Mistral-7B-Instruct-v0.3` 的 wasm，同时示范了两个自定义记录常用字段：`required_features: ["shader-f16"]`（q4f16 模型必须声明）与 `overrides` 里同时改 `context_window_size` 和 `sliding_window_size: -1`（关闭滑动窗口）。
- [src/config.ts:L437-L475](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L437-L475)：同一份 Llama-3.1-8B 权重、同一个 wasm，仅凭 `overrides.context_window_size` 不同（1024 vs 4096）注册成 `...-MLC-1k` 与 `...-MLC` 两个 `model_id`——证明 model_id 完全是逻辑名。
- [src/config.ts:L2562-L2581](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L2562-L2581)：embedding 家族反向示范——**同一份权重**配 `batch32` / `batch4` 两个不同 wasm，得到两个记录（回顾 u2-l5：max_batch_size 烧在库里）。

#### 4.2.4 代码实践

**实践目标**：亲手验证「model_lib 指向本地服务器也能跑」，并观察 localhost wasm 不进缓存的行为。

**操作步骤**：

1. 从官方库下载一个小 wasm（浏览器直接访问 `modelLibURLPrefix + modelVersion` 下的 `Llama-3.2-1B-Instruct-q4f32_1_cs1k-webgpu.wasm`，即 [src/config.ts:L358-L370](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L358-L370) 中第一条记录的 `model_lib` 展开值）。
2. 在 wasm 所在目录起一个静态服务器（如 `npx serve` 或 `python3 -m http.server 8000`）。
3. 仿照 [README.md:L411-L441](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/README.md#L411-L441) 写一个 `model_list`：`model` 仍填官方 HF 地址，`model_lib` 填 `http://localhost:8000/Llama-3.2-1B-Instruct-q4f32_1_cs1k-webgpu.wasm`，`model_id` 起个自己的名字。
4. 加载模型对话一轮；然后刷新页面二次加载。
5. 打开 DevTools 的 Application → Cache Storage，检查 `webllm/wasm` 作用域。

**需要观察的现象**：模型能正常加载（wasm 从 localhost 取回）；二次加载时 Network 面板里 wasm 请求依然发出；`webllm/wasm` 缓存作用域中**没有**这个文件。

**预期结果**：与 [src/engine.ts:L310-L312](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L310-L312) 的三分支逻辑一致：`localhost` 命中「不缓存」分支。此实验需要本地支持 WebGPU 的浏览器，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么源码对 localhost 的 wasm「不缓存」？

> **答案**：[src/engine.ts:L310-L312](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L310-L312) 注释写明"do not cache wasm on local host as we might update code frequently"——本地开发时 wasm 常常刚重新编译过，缓存会让旧版本一直被复用。

**练习 2**：`model` 填 `https://huggingface.co/mlc-ai/Llama-3.2-1B-Instruct-q4f32_1-MLC`（无尾斜杠、无分支），引擎实际请求的第一个文件 URL 是什么？

> **答案**：`cleanModelUrl` 补尾斜杠与 `resolve/main/` 后得到 `.../Llama-3.2-1B-Instruct-q4f32_1-MLC/resolve/main/`，再拼上文件名，即请求 `.../resolve/main/mlc-chat-config.json`。

**练习 3**：同一个 `model` URL 最多能注册几个 `model_id`？

> **答案**：没有限制，只要 `model_id` 互不相同。预置表里已有两类示范：同一权重配不同 wasm（snowflake-embed 的 b4/b32），同一权重同一 wasm 仅 overrides 不同（Llama-3.1-8B 的 1k/4k 两条）。

---

### 4.3 AppConfig 自定义注入

#### 4.3.1 概念说明

自定义配置的入口是 `CreateMLCEngine(modelId, { appConfig }, chatOpts)` 的第二个参数。有三个容易踩坑的语义：

1. **整体替换，不是合并**。构造函数里 `engineConfig?.appConfig || prebuiltAppConfig` 是二选一：你传入的 `model_list` 就是全部可用模型，预置列表里的其他模型不再可用（除非你手动把它们拼进来——`examples/simple-chat-upload` 正是这么做的）。
2. **`overrides` 是记录级 `ChatOptions`**，即 `ChatConfig` 的任意子集，写在 `ModelRecord` 里随记录生效；`chatOpts` 是调用级 `ChatOptions`，写在 `reload()`/`CreateMLCEngine()` 第三参数里。二者最终与仓库配置**浅合并、后写者赢**。
3. **覆盖对象字段要整体给**。`overrides: { conv_template: {...} }` 会把整个模板对象换掉，缺字段就残缺；只想微调个别模板字段应该用 `conv_config`（类型就是 `Partial<ConvTemplateConfig>`），它会在构造会话时叠加在 `conv_template` 之上。

另外两点配套认知：`setAppConfig()` 允许运行时更换配置（换完需重新 reload）；`appConfig` 里的 `cacheBackend` 等缓存选项会被 `getCacheOptions` 消费（u4-l1 讲过，查删载要用同一个 appConfig 才能对上缓存键）。

#### 4.3.2 核心流程

自定义元数据的两条合并链：

```text
链 1：ChatConfig 组装（engine.ts reloadInternal）
  HF 仓库 mlc-chat-config.json
      └─ 覆盖 ─→ ModelRecord.overrides      （记录级）
              └─ 覆盖 ─→ reload 的 chatOpts  （调用级，最终生效）
  实现：{ ...JSON.parse(configData), ...overrides, ...chatOpts }   # 浅合并

链 2：对话模板展开（conversation.ts getConversation）
  ChatConfig.conv_template（完整模板，来自 mlc-chat-config.json 或你的整体替换）
      └─ 叠加 ─→ ChatConfig.conv_config（Partial，局部微调）
  实现：new Conversation({ ...conv_template, ...conv_config })
```

#### 4.3.3 源码精读

**① 三参数工厂与配置入口**。

- [src/engine.ts:L84-L107](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L84-L107)：`CreateMLCEngine(modelId, engineConfig?, chatOpts?)` 的文档注释——`modelId` 必须能在 `prebuiltAppConfig` **或** `engineConfig.appConfig` 中找到；`chatOpts` 与 `modelId` 数量需一致。
- [src/config.ts:L121-L135](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L121-L135)：`MLCEngineConfig` 只有四个可选字段：`appConfig`、`initProgressCallback`、`logitProcessorRegistry`、`logLevel`。

**② 「整体替换」的出处**。

- [src/engine.ts:L150-L166](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L150-L166)：构造函数第 158 行 `this.appConfig = engineConfig?.appConfig || prebuiltAppConfig;`——`||` 语义即二选一。
- [src/engine.ts:L172-L174](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L172-L174)：`setAppConfig` 可随时更换，无校验。
- [examples/simple-chat-upload/src/gh-config.js:L1-L6](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/simple-chat-upload/src/gh-config.js#L1-L6)：真实应用里「保留预置模型再追加」的写法——`model_list: prebuiltAppConfig.model_list`。该配置最终在 [examples/simple-chat-upload/src/simple_chat.ts:L318-L328](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/simple-chat-upload/src/simple_chat.ts#L318-L328) 分别传给 `CreateWebWorkerMLCEngine` 与 `new MLCEngine({ appConfig })`。

**③ `ModelRecord.overrides` 与 `ChatOptions` 的类型关系**。

- [src/config.ts:L257-L286](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L257-L286)：`ModelRecord` 的字段文档——`overrides` 被描述为"partial ChatConfig to override mlc-chat-config.json; can be used to change KVCache settings"。
- [src/config.ts:L114-L118](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L114-L118)：`ChatOptions extends Partial<ChatConfig>`——所以 `overrides` 里能写的字段就是 `ChatConfig` 的字段。
- [src/config.ts:L288-L317](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L288-L317)：`AppConfig` = `model_list` + 可选 `cacheBackend` / `opfsAccessMode`（缓存语义见 u4-l1 的 `getCacheOptions`，[src/cache_util.ts:L20-L23](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/cache_util.ts#L20-L23)）。

**④ 浅合并发生的位置**。

- [src/engine.ts:L292-L296](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L292-L296)：`{ ...JSON.parse(...), ...modelRecord.overrides, ...chatOpts }`——三层展开一目了然；也正因为是浅合并，嵌套对象会被整体替换。

**⑤ `conv_template` 与 `conv_config` 的双字段设计**。

- [src/config.ts:L85-L92](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L85-L92)：`ChatConfig` 中 `conv_config?: Partial<ConvTemplateConfig>`（局部微调）与 `conv_template: ConvTemplateConfig`（完整模板）并列。
- [src/conversation.ts:L363-L372](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/conversation.ts#L363-L372)：`getConversation` 用 `{ ...conv_template, ...conv_config }` 完成模板层的二级合并——微调 system 提示词、停止符等应走这条路。

**⑥ 官方示例：同一文件里两种自定义姿势**。

- [examples/get-started/src/get_started.ts:L15-L29](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started/src/get_started.ts#L15-L29)：Option 1——用预置记录 + 第三参数 `chatOpts` 覆盖 `context_window_size: 2048`。
- [examples/get-started/src/get_started.ts:L31-L50](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started/src/get_started.ts#L31-L50)：Option 2（官方注释好的代码块）——自定义 `appConfig`，用 `webllm.modelLibURLPrefix + webllm.modelVersion` 拼 wasm 地址，`overrides.context_window_size: 2048`。本讲主实践就基于它。

#### 4.3.4 代码实践

**实践目标**：不编译任何东西，仅靠自定义 `appConfig` + `overrides` 改变一个模型的行为，并用可观察的现象证明覆盖确实生效。

**操作步骤**：

1. 进入 `examples/get-started/`，`npm install && npm start`（Parcel 会在 8888 端口起服务，回顾 u1-l2）。
2. 打开 [examples/get-started/src/get_started.ts:L31-L50](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started/src/get_started.ts#L31-L50) 的 Option 2 代码块，取消注释并启用（注释掉 Option 1 的 `CreateMLCEngine` 调用），做三处修改：
   - `model_id` 改成 `"My-Llama-3.1-8B"`（证明这只是逻辑名）；
   - `overrides` 增加 `conv_config: { system_message: "You are a pirate. Answer in pirate speak." }`（走 `Partial` 微调通道）；
   - `overrides.context_window_size` 先保持 2048。
3. 把 `selectedModel` 改为 `"My-Llama-3.1-8B"`，刷新页面完成一轮对话，观察回复人设是否符合你的 system_message。
4. 把 `overrides.context_window_size` 改成一个很小的值（如 64），刷新重载后发送一段较长的问题。
5. 再在第三参数 `chatOpts` 里传 `{ context_window_size: 2048 }`，重载后重复第 4 步的长问题。

**需要观察的现象**：

- 第 3 步：模型以海盗口吻回答 → `conv_config` 微调生效。
- 第 4 步：长 prompt 触发 `ContextWindowSizeExceededError`（[src/error.ts:L356](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L356)）→ `overrides` 的窗口覆盖生效（KV cache 按覆盖值分配，回顾 u3-l3/u3-l4 中 filledKVCacheLength 的窗口检查）。
- 第 5 步：同样的长问题不再报错 → 链 1 中 `chatOpts` 覆盖 `overrides`，「后写者赢」得到验证。

**预期结果**：三步现象全部复现即证明三条规则（逻辑名、Partial 微调、三层浅合并）。本实践需要本地 WebGPU 环境与一次约 5GB 的模型下载，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`overrides: { conv_template: { system_message: "xxx" } }` 这样写有什么问题？

> **答案**：链 1 是浅合并，整个 `conv_template` 对象会被替换成只含 `system_message` 的残缺对象，`roles`、`seps`、`stop_str` 等必填字段全部丢失，编码 prompt 时行为异常。正确做法：局部微调写 `conv_config`（链 2 的 Partial 叠加）；确要换整套模板时给出完整的 `ConvTemplateConfig`（所有字段）。

**练习 2**：传了自定义 `appConfig`（只含一条记录）后，还能用 `reload("Llama-3.2-1B-Instruct-q4f32_1-MLC")` 加载预置模型吗？

> **答案**：不能。构造函数里 `engineConfig?.appConfig || prebuiltAppConfig` 是整体替换，`findModelRecord` 在你的 `model_list` 里找不到该 id，抛 `ModelNotFoundError`。想保留预置模型就把 `prebuiltAppConfig.model_list` 展开进自己的列表（`gh-config.js` 的做法）。

**练习 3**：`overrides` 和第三参数 `chatOpts` 都设了 `temperature`，最终用哪个？

> **答案**：`chatOpts` 的。合并式为 `{ ...仓库配置, ...overrides, ...chatOpts }`，后展开的键覆盖先展开的键。

**练习 4**：`required_features: ["shader-f16"]` 写在 `ModelRecord` 里有什么用？

> **答案**：`reload()` 检测 GPU 后会逐项核对（[src/engine.ts:L358-L367](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L358-L367)），设备不支持 `shader-f16` 时抛 `ShaderF16SupportError`，避免加载到一半才失败。自定义 q4f16 模型应照 Hermes 记录的写法声明。

## 5. 综合实践

**任务：给你的页面接入一个「非预置」模型，跑通第一轮对话。** 按环境二选一（或先 A 后 B）。

### 方案 A：零编译演练（任何人都可做）

复用现有官方产物，只自定义 appConfig 的全部可定制字段，把本讲三个模块的知识各用一次：

1. **选记录**：从 [src/config.ts:L354-L370](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L354-L370) 起的 `prebuiltAppConfig.model_list`（共 163 条活跃记录）中挑一条小模型（如 `Llama-3.2-1B-Instruct-q4f32_1-MLC`）。
2. **改身份**：写一个自定义 `appConfig`，`model` 沿用官方 HF 地址，`model_lib` 用 `webllm.modelLibURLPrefix + webllm.modelVersion + "/..."` 拼出（模块 4.2），`model_id` 自命名。
3. **加覆盖**：`overrides` 里同时改 `context_window_size` 与 `conv_config.system_message`（模块 4.3），并按需声明 `required_features`。
4. **验证三件事**：对话成功且人设生效；把窗口改到极小后长 prompt 报 `ContextWindowSizeExceededError`；用 `hasModelInCache("你的 model_id")`（u4-l2）确认缓存键跟随新记录。
5. **可选加固**：按 u4-l3 的方法为 `mlc-chat-config.json` 与 wasm 生成 SRI 哈希填进 `integrity` 字段，篡改一位哈希验证 `IntegrityError` 被抛出。

### 方案 B：完整编译（需要 Python + Wasm 构建环境）

1. 按 4.1.2 的命令序列处理一个小模型（建议 TinyLlama-1.1B 或 Qwen2-0.5B，`--quantization q4f16_1`，`--conv-template` 按模型选择）。
2. 判断路线：若是 Llama/Qwen/Mistral 等已支持架构的同量化变体，直接复用预置 wasm（路线 A，省去 compile 与 Wasm 环境）；否则 `mlc_llm compile --device webgpu` 产出自己的 wasm（路线 B）。
3. 权重目录（含 `mlc-chat-config.json`、`params_shard_*.bin`、tokenizer 文件）上传 HuggingFace 仓库；wasm 上传 GitHub 仓库取 raw 直链，或放本地静态服务器（注意 4.2.4 的不缓存行为；本地托管权重目录时记得 `/resolve/main/` 路径问题，**待确认**）。
4. 注册 `ModelRecord` 并加载对话；对照 `mlc-chat-config.json` 里 `gen_config` 写入的 `conv_template`，确认运行时行为与之一致。
5. 记录编译参数（量化、conv-template、是否 `--prefill_chunk_size`）与最终 `ModelRecord` 的映射关系，作为团队的模型接入清单。

**预期结果**：无论 A/B，你都应该得到一个「不在 `prebuiltAppConfig` 里、却能被 `CreateMLCEngine` 加载并对话」的模型，并且能说清它的 `model` / `model_lib` / `overrides` 各自决定了什么。方案 B 全流程**待本地验证**。

## 6. 本讲小结

- WebLLM 只做「下载 + 实例化」，编译发生在 MLC LLM：`convert_weight`（量化权重）→ `gen_config`（生成 `mlc-chat-config.json` 与 tokenizer）→ 可选 `compile --device webgpu`（wasm 库）；四要素相同即可复用现成库（路线 A），否则自己编译（路线 B）。
- `model_id` 纯粹是 `appConfig.model_list` 里的查找键；同一权重可配不同 wasm（snowflake b4/b32）、同一 wasm 可配不同 overrides（Llama-3.1-8B 的 1k/4k）注册成多条记录。
- URL 规则在 `cleanModelUrl` 与 `reloadInternal`：`model` 自动补 `/resolve/main/` 后派生出 config/tokenizer/权重三类下载；`model_lib` 走三分支——localhost 与相对路径不缓存，https 走 `webllm/wasm` 缓存。
- 自定义 `appConfig` 是**整体替换** `prebuiltAppConfig`；`overrides`（记录级）与 `chatOpts`（调用级）与仓库配置**浅合并、后写者赢**，嵌套对象会被整体替换。
- 微调对话模板用 `conv_config`（`Partial<ConvTemplateConfig>`，在 `getConversation` 中叠加），整套替换才用 `conv_template`；`required_features` 让不满足的设备尽早报错。
- 自定义记录的三件配套：`integrity` 哈希校验（u4-l3）、缓存键跟随 appConfig（u4-l1/u4-l2）、以及可运行的 `setAppConfig` 动态换配置。

## 7. 下一步学习建议

- **第五单元 u5-l1（Web Worker 架构）**：自定义模型往往加载更慢、推理更重，把引擎搬进 Worker 的收益更明显；`examples/simple-chat-upload` 已示范同一 appConfig 在两种引擎下的用法，可作预习材料。
- **u7-l2（多模型管理与动态切换）**：当你的 appConfig 里积累了自己的模型库后，自然会遇到「多个自定义模型共存与切换」的资源管理问题。
- **延伸阅读**：[MLC LLM WebLLM 部署文档](https://llm.mlc.ai/docs/deploy/webllm.html)（本讲编译命令的原始出处）与 `mlc_llm` 的 `conversation_template.py`（全部可选 `--conv-template` 的清单）；官方 wasm 仓库 [binary-mlc-llm-libs](https://github.com/mlc-ai/binary-mlc-llm-libs) 可当作自定义库分发的模板。
- **动手方向**：把方案 B 产出的模型接入 `examples/simple-chat-upload`，替换其 `app-config.js`，得到一个完全运行在「你自己的模型供应链」上的聊天应用。
