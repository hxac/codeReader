# 模型缓存机制：CacheStorage 与 OPFS

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出一次 `reload()` 过程中浏览器需要持久化哪四类产物，以及它们分别落在 `webllm/config`、`webllm/wasm`、`webllm/model` 三个缓存作用域中的哪一个。
2. 解释 `AppConfig.cacheBackend`（`CacheBackend` 类型）四个取值与 `AppConfig.opfsAccessMode`（`OPFSAccessMode` 类型）三个取值的含义，以及缺省时 WebLLM 如何兜底。
3. 读懂 `cache_util.ts` 中三个核心函数——`getCacheOptions`、`getTensorCacheAccessOptions`、`createScopedArtifactCache`——如何把用户的 `AppConfig` 翻译成底层运行时 `@mlc-ai/web-runtime`（tvmjs）能理解的选项，并理解「作用域隔离」为什么重要。
4. 对比 CacheStorage（Cache API）与 OPFS 两种后端的存储位置、观察方式与适用场景，能为本地的 WebLLM 应用做出有依据的选型。

本讲是第四单元「模型分发：配置、缓存与完整性校验」的第一讲，承接 u1-l4 对 `AppConfig` / `ModelRecord` 的理解：那一讲回答「一个模型由哪些元数据描述、从哪里下载」，本讲回答「下载下来的东西放在浏览器的哪里、怎么管理」。

## 2. 前置知识

### 2.1 为什么必须有缓存

WebLLM 的模型完全跑在浏览器里（回顾 u1-l1），代价是所有产物都要从网络下载到一个典型量级为几百 MB 到几 GB 的模型（例如 `Llama-3.2-1B-Instruct-q4f32_1-MLC` 权重约 1GB 出头，见 `vram_required_MB` 字段）。如果每次打开页面都重新下载，体验不可接受。因此 WebLLM 把所有下载产物写入浏览器提供的**持久化存储**，第二次加载时直接命中本地，不再走网络——这也是你在 u1-l2 中观察到「二次加载显著提速」的原因。

### 2.2 浏览器持久化技术速览

Web 平台有多种「按源（origin）隔离」的持久化机制，本讲涉及四种：

| 技术 | 一句话理解 |
|---|---|
| **Cache API / CacheStorage** | 为 Service Worker 设计的「请求-响应」缓存，按名字分成多个 `Cache` 桶，非常适合按 URL 缓存文件 |
| **IndexedDB** | 浏览器内置的异步键值/对象数据库，通用但抽象层级不同 |
| **OPFS**（Origin Private File System） | 页面源私有的「虚拟文件系统」，可以用文件读写 API（含高性能的 sync access handle）操作，不暴露给用户 |
| **Cross-Origin Storage 扩展** | Chrome 实验性扩展提供的跨源存储，需单独安装浏览器扩展 |

WebLLM 自己**不实现**这些存储，而是把选择权收敛成 `AppConfig.cacheBackend` 字段，再委托给 `@mlc-ai/web-runtime`（tvmjs，回顾 u3-l1：它同时也是 WebGPU 运行时底座）统一封装。这个「web-llm 定策略、web-runtime 做实现」的分工是本讲反复出现的主线。

### 2.3 术语约定

- **产物（artifact）**：一次模型加载需要从网络获取的文件。本讲会精确到四类：`mlc-chat-config.json`（聊天配置）、model_lib 的 `.wasm`（模型库）、tokenizer 文件（`tokenizer.json` 或 `tokenizer.model`）、模型权重（分片 NDArray，即 tensor cache）。
- **作用域（scope）**：缓存的「命名空间」。WebLLM 用三个固定字符串 `webllm/config`、`webllm/wasm`、`webllm/model` 把不同种类的产物分开存放。
- **tvmjs**：`@mlc-ai/web-runtime` 包的命名空间别名，源码里 `import * as tvmjs from "@mlc-ai/web-runtime"`。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [src/cache_util.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/cache_util.ts) | **本讲主角**。定义三个缓存选项函数与作用域类型，并实现面向用户的缓存管理 API（`hasModelInCache`、`delete*InCache`、`asyncLoadTokenizer`） |
| [src/config.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts) | `CacheBackend`、`OPFSAccessMode` 类型定义，`AppConfig` 字段文档，`getCacheBackend` 缺省逻辑，`prebuiltAppConfig` 的默认后端 |
| [src/engine.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts) | 缓存的**消费方**：`reload()` 中创建 config/wasm 两类缓存、加载 tokenizer、拉取权重 tensor cache |
| [src/support.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts) | `cleanModelUrl`：把 HuggingFace 仓库 URL 规范化为 `/resolve/main/` 形式，决定缓存键 |
| [src/utils.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/utils.ts) | `areAppConfigsEqual`：体现「缓存后端属于配置身份」的比较工具 |
| [src/index.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/index.ts) | 库入口，导出五个缓存管理函数（`hasModelInCache` 等）供页面直接调用 |
| [examples/cache-usage/](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/cache-usage/README.md) | 官方缓存示例：切换后端、检查/删除缓存的完整演示，本讲实践的载体 |
| [tests/cache_util.test.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/cache_util.test.ts) | 用 mock 替身验证 `cache_util.ts` 行为的单测，是理解函数输出形状的最好参照 |
| `node_modules/@mlc-ai/web-runtime` | 后端真正实现（不在本仓库）。本讲只讲清 WebLLM 与它的接口契约，不深入其内部 |

## 4. 核心概念与源码讲解

### 4.1 缓存总览：一次 reload 要缓存哪些产物

#### 4.1.1 概念说明

回顾 u2-l1 的 reload 流程，引擎在「查到 ModelRecord 之后、构造管线之前」要下载四类产物。这四类产物**并非挤在一个缓存里**，而是按种类分成三个作用域：

| 产物 | 作用域 | 缓存键（URL） |
|---|---|---|
| `mlc-chat-config.json`（聊天配置） | `webllm/config` | `<model>/resolve/main/mlc-chat-config.json` |
| model_lib 的 `.wasm`（模型库） | `webllm/wasm` | `ModelRecord.model_lib` 原样 |
| tokenizer 文件 | `webllm/model` | `<model>/resolve/main/tokenizer.json`（或 `tokenizer.model`） |
| 模型权重分片（tensor cache） | `webllm/model` | 以 `cleanModelUrl(model)` 为根目录的权重文件 |

这样拆分的直接收益是**管理粒度**：删除一个模型时可以分门别类地清理（u4-l2 的主题），查询「权重在不在」也不会被无关文件干扰。

#### 4.1.2 核心流程

以 `reload(modelId)` 为轴，缓存相关步骤如下：

```text
reload(modelId)
  ├─ 查 ModelRecord（model、model_lib 等 URL 从这里来）
  ├─ configCache = createArtifactCache("webllm/config", 选项)
  │    └─ fetchWithCache("<model>/…/mlc-chat-config.json")   # 命中则不联网
  ├─ wasmCache = createArtifactCache("webllm/wasm", 选项)
  │    └─ fetchWithCache(model_lib)                           # localhost/同源例外，见 4.1.3
  ├─ asyncLoadTokenizer(...)                                  # 内部用 "webllm/model" 作用域
  └─ tvm.fetchTensorCache(modelUrl, device, {cacheScope:"webllm/model", …})
       └─ 管线随后用 tvm.getParamsFromCacheByName(...) 从缓存取权重
```

三个作用域的类型定义只有一个联合类型，是全部作用域的「单一事实来源」：

#### 4.1.3 源码精读

作用域类型在 `cache_util.ts` 顶部，三行说完整个分类体系：

[src/cache_util.ts:L14-L18](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/cache_util.ts#L14-L18) —— 定义 `CacheScope`（三个合法作用域）和 `CacheOptions`（从 tvmjs 的 `TensorCacheAccessOptions` 中挑出 `cacheType`、`opfsAccessMode` 两个字段构成 WebLLM 侧的选项形状）。

引擎侧消费第一个作用域：拉取聊天配置。

[src/engine.ts:L271-L283](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L271-L283) —— 用 `tvmjs.createArtifactCache("webllm/config", getCacheOptions(...))` 创建 config 缓存，再 `fetchWithCache(configUrl, "arraybuffer", signal)` 取 `mlc-chat-config.json`；`signal` 让 reload 被 AbortController 中断时下载也随之取消（承接 u2-l1 的中断机制）。

第二个作用域：wasm 模型库。这里有一个值得注意的例外分支：

[src/engine.ts:L300-L324](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L300-L324) —— 创建 `"webllm/wasm"` 缓存后，`fetchWasmSource` 内部做了三路分发：URL 含 `localhost` 时**直接 fetch 不缓存**（本地开发时 wasm 常更新，缓存反而碍事）；URL 不以 `http` 开头（同源相对路径）时同样不缓存，交给浏览器常规 HTTP 缓存；只有远程 http(s) 的 `model_lib` 才走 `wasmCache.fetchWithCache`。这解释了为什么示例 README 建议改 core 源码调试时用 `file:../..` 依赖——本地路径的 wasm 永远取最新。

第三个作用域：tokenizer 与权重。

[src/engine.ts:L387-L397](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L387-L397) —— `asyncLoadTokenizer`（内部用 `"webllm/model"` 作用域缓存 tokenizer 文件）之后，调用 `tvm.fetchTensorCache(modelUrl, tvm.webgpu(), {...getTensorCacheAccessOptions("webllm/model", this.appConfig), signal})` 把权重分片拉进 tensor cache。注意这里用的是**带 `cacheScope` 的完整选项**，与 config/wasm 两处用的「裸选项」不同，区别在 4.3 详解。

缓存键的规范化：

[src/support.ts:L91-L97](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L91-L97) —— `cleanModelUrl` 给 HuggingFace 仓库 URL 补尾部斜杠并追加 `resolve/main/`，保证 `https://huggingface.co/mlc-ai/X` 和 `https://huggingface.co/mlc-ai/X/` 规范化成同一个缓存键，避免同模型存两份。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到三个作用域与其中缓存的文件 URL。
2. **操作步骤**：
   - 进入 `examples/cache-usage/`，执行 `npm install && npm start`（Parcel 会在 **8889** 端口起服务，见 [examples/cache-usage/package.json:L5-L7](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/cache-usage/package.json#L5-L7)）。
   - 打开支持 WebGPU 的浏览器访问该页面，等待示例完整跑完（console 会依次输出加载、`hasModelInCache: true`、删除、`false`、重新下载）。
   - **注意**：示例第 4 步会自动删除模型（见 4.3.4），想在「已缓存」状态下观察，请先把 [examples/cache-usage/src/cache_usage.ts:L71-L80](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/cache-usage/src/cache_usage.ts#L71-L80) 的删除段注释掉再刷新（修改示例文件属于练习行为，不涉及源码）。
   - 打开 DevTools → Application → **Cache Storage**，展开左侧树。
3. **需要观察的现象**：三个名字恰好为 `webllm/config`、`webllm/wasm`、`webllm/model` 的 Cache 桶；`webllm/config` 里有一条 `…/resolve/main/mlc-chat-config.json`；`webllm/wasm` 里是 `.wasm`；`webllm/model` 里是 tokenizer 与权重分片。
4. **预期结果**：文件 URL 与 4.1.1 表格逐一对应；再次刷新页面时 Network 面板中这些请求显示命中缓存（Chrome 标注为 disk cache 或不出现请求），加载明显变快。具体 UI 措辞随浏览器版本不同，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 tokenizer 和权重放在同一个作用域 `webllm/model`，而不是单独给 tokenizer 开一个 `webllm/tokenizer`？
**答案**：两者都源自同一个权重仓库 URL（`ModelRecord.model` 规范化后的目录），生命周期一致——「模型文件在不在本地」对二者是同一个问题。`hasModelInCache` 判断权重、`deleteModelInCache` 同时清权重与 tokenizer，共用作用域让「按模型仓库整体管理」天然成立；而 config/wasm 的来源 URL 不同（后者是 GitHub 上的 model_lib），故单独隔离。

**练习 2**：把 `model_lib` 指向 `http://localhost:8000/my.wasm` 会发生什么？
**答案**：走 [src/engine.ts:L310-L312](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L310-L312) 的第一分支：直接 `fetch` 且**不写入任何缓存**，每次 reload 都重新下载。这是刻意为之，本地调试时 wasm 频繁变更，缓存旧版本反而出错。

### 4.2 getCacheOptions：缓存后端选择的统一入口

#### 4.2.1 概念说明

WebLLM 支持四种缓存后端，但用户配置只有一个字段 `AppConfig.cacheBackend`。`getCacheOptions` 是**唯一**把该字段翻译成 tvmjs 选项（`cacheType`）的地方——所有需要触碰缓存的代码（engine 的三处、cache_util 的全部函数）都经过它，因此「换后端」只需要改一个字符串，不用改任何调用点。它是典型的「配置归一化」函数：小，但处于关键收口位置。

注意一个命名映射：WebLLM 叫 `cacheBackend`，tvmjs 叫 `cacheType`，指同一个东西。

#### 4.2.2 核心流程

```text
getCacheOptions(appConfig)
  1. cacheType = getCacheBackend(appConfig)
       └─ appConfig.cacheBackend 显式给出 → 用它
       └─ 否则 → "cache"（Cache API）
  2. 若 appConfig.opfsAccessMode !== undefined：
       附上 opfsAccessMode（仅 opfs 后端真正消费它）
  3. 返回 { cacheType, opfsAccessMode? }
```

四个后端取值与三个 OPFS 模式取值的语义表：

| `CacheBackend` 取值 | 对应浏览器技术 | 备注 |
|---|---|---|
| `"cache"` | Cache API（CacheStorage） | **默认值**；config.ts 注释明言「目前测试最充分」 |
| `"indexeddb"` | IndexedDB | 通用键值存储后端 |
| `"opfs"` | Origin Private File System | 需配 `opfsAccessMode`；环境不支持时报 OPFS 可用性错误 |
| `"cross-origin"` | Chrome Cross-Origin Storage 扩展 | 实验性；未装扩展时自动回退默认缓存；不支持程序化删除（README 说明） |

| `OPFSAccessMode` 取值 | 含义 |
|---|---|
| `"async"` | 使用 OPFS 异步文件 API（默认） |
| `"sync"` | 强制要求 OPFS sync access handle |
| `"auto"` | 环境支持时用 sync handle，否则回退 async |

#### 4.2.3 源码精读

类型与字段文档（取值含义的权威出处）：

[src/config.ts:L288-L317](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L288-L317) —— `CacheBackend`、`OPFSAccessMode` 两个字符串字面量联合类型，以及携带 `cacheBackend?`、`opfsAccessMode?` 两个可选字段的 `AppConfig` 接口；注释逐项列出四个后端与三种 OPFS 模式的语义。

缺省兜底：

[src/config.ts:L319-L324](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L319-L324) —— `getCacheBackend`：显式值优先，否则返回 `"cache"`。注意兜底不依赖 `prebuiltAppConfig`，是硬编码的。

预置配置的默认：

[src/config.ts:L354-L356](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L354-L356) —— `prebuiltAppConfig` 显式写了 `cacheBackend: "cache"`，与缺省值一致；所以不传 `appConfig` 时最终生效的就是 Cache API。

本尊出场，共 9 行：

[src/cache_util.ts:L20-L28](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/cache_util.ts#L20-L28) —— `getCacheOptions` 组装 `{ cacheType: getCacheBackend(appConfig) }`，并仅在 `opfsAccessMode` 已定义时附上它。`CacheOptions` 类型（L15-18 用 `Pick` 从 tvmjs 的 `TensorCacheAccessOptions` 摘出两字段）保证了 WebLLM 与 web-runtime 的字段名永远对齐——如果上游改名，这里编译期就会报错。

README 的用户侧文档与之一致，可对照阅读：

[README.md:L157-L181](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/README.md#L157-L181) —— 「Cache Backend Policy」章节：四个后端说明、`{ ...prebuiltAppConfig, cacheBackend: "cross-origin" }` 的写法示例、以及 opfs 报错与 cross-origin 不支持删除的注意事项。

#### 4.2.4 代码实践

1. **实践目标**：不运行浏览器，仅靠单测确认 `getCacheOptions` 的输出形状。
2. **操作步骤**：
   - 先笔答：输入 `{ model_list: [...], cacheBackend: "opfs", opfsAccessMode: "auto" }`，`getCacheOptions` 返回什么？输入不带这两个字段的 `AppConfig` 呢？
   - 打开 [tests/cache_util.test.ts:L93-L108](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/cache_util.test.ts#L93-L108)，与你的笔答对照——该用例断言前者的输出恰为 `{ cacheType: "opfs", opfsAccessMode: "auto" }`。
   - 可选在仓库根目录执行 `npm install && npx jest tests/cache_util.test.ts` 实际跑一遍（本讲义写作环境未安装依赖，**待本地验证**）。
3. **需要观察的现象**：测试中 `opfsAccessMode` 未定义时断言里就没有这个键（例如 L84-L90 的 `hasModelInCache` 用例只断言 `{ cacheScope, cacheType }`），印证「条件附加」行为。
4. **预期结果**：笔答与测试断言一致，即证明你已掌握缺省规则与条件转发规则。

#### 4.2.5 小练习与答案

**练习 1**：`{ ...prebuiltAppConfig, cacheBackend: "opfs" }` 与 `prebuiltAppConfig` 本身，在 `getCacheOptions` 眼里差在哪？
**答案**：前者 `getCacheBackend` 返回 `"opfs"`，后者返回 `"cache"`（显式写的 `"cache"`，即使不写也会兜底成 `"cache"`）。两者都不带 `opfsAccessMode`，故 opfs 那份走默认的 async 文件 API。

**练习 2**：为什么 `getCacheOptions` 用 `Pick<tvmjs.TensorCacheAccessOptions, "cacheType" | "opfsAccessMode">` 而不是自己声明一个 `{ cacheType: string }`？
**答案**：让 WebLLM 侧类型直接「借用」web-runtime 的类型定义。tvmjs 若调整字段名或类型，`cache_util.ts` 会在编译期失配报错，而不是运行时静默传错参数。这是跨包契约的类型级钉子。

### 4.3 getTensorCacheAccessOptions 与 createScopedArtifactCache：作用域隔离

#### 4.3.1 概念说明

后端选好之后，还差「放哪个命名空间」。WebLLM 有两类底层缓存接口，分别用两种方式携带作用域：

- **产物缓存（artifact cache）**：面向「一个 URL 对应一个文件」的普通下载，接口是 `tvmjs.createArtifactCache(scope, options)`，作用域作为**第一个位置参数**传入。返回的对象（类型 `ArtifactCacheTemplate`）只有两个核心方法：`fetchWithCache(url, format, signal?)`（缓存优先取文件）与 `deleteInCache(url)`（删除单个文件）。config、wasm、tokenizer 走这类。
- **张量缓存（tensor cache）**：面向模型权重分片，理解 NDArray 布局、支持按名字取参数（`getParamsFromCacheByName`）。它的 API（`tvm.fetchTensorCache` / `tvmjs.hasTensorInCache` / `tvmjs.deleteTensorCache`）不接收独立的作用域参数，作用域必须**混进选项对象**里，以 `cacheScope` 字段传递。

`cache_util.ts` 用两个一行函数分别封装这两种携带方式，`createScopedArtifactCache` 再把「建产物缓存」收敛成单一出口。这就是本讲的第二个关键词——**作用域隔离**：三类产物互不混放，管理 API 才能精确打击。

#### 4.3.2 核心流程

```text
# 张量缓存的选项（作用域进对象）
getTensorCacheAccessOptions(scope, appConfig)
  = { cacheScope: scope, ...getCacheOptions(appConfig) }
  → 供 tvm.fetchTensorCache / tvmjs.hasTensorInCache / tvmjs.deleteTensorCache 使用

# 产物缓存的创建（作用域进位置参数）
createScopedArtifactCache(scope, appConfig)        # 模块私有函数
  = tvmjs.createArtifactCache(scope, getCacheOptions(appConfig))
  → 返回 { fetchWithCache, deleteInCache }
```

两个函数在 `cache_util.ts` 内的四个调用点：

| 调用点 | 作用域 | 用途 |
|---|---|---|
| `hasModelInCache` | `webllm/model` | 询问「权重是否已在张量缓存」 |
| `deleteModelInCache` | `webllm/model` | 删权重张量 + 两个 tokenizer 文件 |
| `deleteChatConfigInCache` | `webllm/config` | 删 `mlc-chat-config.json` |
| `deleteModelWasmInCache` | `webllm/wasm` | 删 model_lib wasm |
| `asyncLoadTokenizer` | `webllm/model` | 缓存优先地取 tokenizer 文件 |

（`engine.ts` 直接调 `tvmjs.createArtifactCache` 而不走这个私有包装——两处代码等价，只是 engine 不 import 私有函数。）

#### 4.3.3 源码精读

两个封装函数，合计不到 15 行：

[src/cache_util.ts:L30-L45](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/cache_util.ts#L30-L45) —— `getTensorCacheAccessOptions` 把 `cacheScope` 放在展开 `getCacheOptions` 之前（若 appConfig 未来出现同名字段也不会意外覆盖作用域）；`createScopedArtifactCache` 是模块私有的薄封装，签名即文档：`(scope, appConfig) => ArtifactCacheTemplate`。

查询 API 如何消费作用域：

[src/cache_util.ts:L69-L82](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/cache_util.ts#L69-L82) —— `hasModelInCache`：`appConfig` 缺省回退 `prebuiltAppConfig` → `findModelRecord` 查记录（查不到抛 `ModelNotFoundError`）→ `cleanModelUrl` 规范化 URL → `tvmjs.hasTensorInCache(modelUrl, getTensorCacheAccessOptions("webllm/model", appConfig))`。注意它**只查权重**，不含 wasm/config。

删除按作用域分兵三路：

[src/cache_util.ts:L84-L98](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/cache_util.ts#L84-L98) —— `deleteModelAllInfoInCache` 只是依次调用下面三个删除函数，把一个模型的全部痕迹清掉；

[src/cache_util.ts:L100-L117](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/cache_util.ts#L100-L117) —— `deleteModelInCache` 先用 `tvmjs.deleteTensorCache`（张量路径，带 `cacheScope` 的选项）删权重，再用同一个 `"webllm/model"` 产物缓存把 `tokenizer.model` 与 `tokenizer.json` 两个 URL 都删掉（模型可能只有其中之一，删不存在的键是无害的）；

[src/cache_util.ts:L119-L145](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/cache_util.ts#L119-L145) —— `deleteChatConfigInCache` 与 `deleteModelWasmInCache` 分别用 `"webllm/config"`、`"webllm/wasm"` 作用域删各自那一个文件。

tokenizer 的缓存优先加载：

[src/cache_util.ts:L156-L174](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/cache_util.ts#L156-L174) —— `asyncLoadTokenizer` 优先走 `tokenizer.json`（`modelCache.fetchWithCache(url, "arraybuffer")`，缓存命中即不联网），否则回退 `tokenizer.model`（SentencePiece，并打日志建议改用前者），两者都可为空则抛 `UnsupportedTokenizerFilesError`。下载后还会按 `integrity.tokenizer` 里的哈希做完整性校验（u4-l3 主题，此处只知其存在即可）。

单测如何「无浏览器」验证这一切：

[tests/cache_util.test.ts:L15-L45](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/cache_util.test.ts#L15-L45) —— 用 `jest.mock` 把 `@mlc-ai/web-runtime` 整体替换成一个记录调用的假实现（`createArtifactCache` 返回带 `fetchWithCache`/`deleteInCache` 的 `BaseCache`，把每次调用推进 `state.fetches`/`state.deletes` 数组）。于是「传了什么 scope、什么选项、删了什么 URL」都变成可断言的数据，例如 L110-L141 断言 `deleteModelInCache` 在 indexeddb 后端下恰好删除两个 tokenizer URL。

#### 4.3.4 代码实践

1. **实践目标**：跑通官方示例的「检查 → 删除 → 再下载」闭环，把 console 输出与源码一一对应。
2. **操作步骤**：
   - 运行 `examples/cache-usage`（步骤见 4.1.4），保持删除段**未注释**。
   - 观察 console 中依次出现的：`hasModelInCache: true`（[examples/cache-usage/src/cache_usage.ts:L52-L57](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/cache-usage/src/cache_usage.ts#L52-L57)）、`Reload model start/end` 明显变快（L59-L64，命中缓存）、删除后 `hasModelInCache: false`（L71-L80）、再次 reload 重新下载（L82-L87）。
3. **需要观察的现象**：删除前后在 DevTools → Application → Cache Storage 里对比 `webllm/model`、`webllm/wasm`、`webllm/config` 三个桶的内容变化。
4. **预期结果**：`deleteModelAllInfoInCache` 之后三个桶中被清空或条目消失（Cache API 删掉空桶后可能不再显示，属正常现象，**待本地验证**）；最后一次 reload 又把三类产物重新填回。
5. 若你所在环境无法运行 WebGPU 页面，可改为源码阅读实践：参照 L15-L45 的 mock 思路，笔算 `deleteModelAllInfoInCache("demo-model", indexedConfig)` 会往 `deletes` 数组推进哪四条记录（答案：model 作用域的 tokenizer.model、tokenizer.json，wasm 作用域的 model.wasm，config 作用域的 mlc-chat-config.json——张量删除走 `deleteTensorCache` 不进该数组）。

#### 4.3.5 小练习与答案

**练习 1**：`hasModelInCache` 返回 `false`，能否断定这个模型「完全没缓存过」？
**答案**：不能。它只查 `webllm/model` 作用域的**权重张量**。理论上存在权重被单独删除（`deleteModelInCache`）而 wasm/config 仍在缓存里的状态；反之权重在而 config 不在也可能出现。要「全部删除」请用 `deleteModelAllInfoInCache`。

**练习 2**：为什么张量缓存的 API 把 `cacheScope` 放进选项对象，而产物缓存把 scope 作为位置参数？
**答案**：接口归属不同。`createArtifactCache(scope, options)` 是「先命名空间后配置」的工厂函数，scope 是构造参数；而 `hasTensorInCache(url, options)`、`fetchTensorCache(url, device, options)` 这类函数参数已经很多（URL、device、signal 等），把 scope 收进统一的选项对象让调用形状保持 `资源 + 配置` 两段式。WebLLM 用 `getTensorCacheAccessOptions` 一个函数统一了这类调用 的 scope 注入。

**练习 3**：`cross-origin` 后端下运行 cache-usage 示例，为何脚本提前 `return`？
**答案**：见 [examples/cache-usage/src/cache_usage.ts:L66-L69](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/cache-usage/src/cache_usage.ts#L66-L69)：cross-origin 后端不支持程序化的张量缓存删除（由扩展管理），删除段被跳过。

### 4.4 CacheStorage 与 OPFS：后端横向对比与选型

#### 4.4.1 概念说明

有了前三讲铺垫，现在正面回答本讲标题：**CacheStorage 与 OPFS 各是什么、怎么选**。

- **CacheStorage（`"cache"` 后端）**：按「缓存名 → 请求/响应」组织，天生以 URL 为键，与 WebLLM「一切产物都是 URL」的模型严丝合缝；DevTools 有专门面板，调试直观。它是 WebLLM 的默认与测试最充分的后端。
- **OPFS（`"opfs"` 后端）**：源私有的虚拟文件系统。吸引力在于**文件级读写路径**：普通异步文件句柄之外，还提供 sync access handle（同步读写句柄），省去异步队列开销，对几百 MB 的权重写入/读出更友好。代价是环境要求更高——不支持 OPFS 的环境会直接报可用性错误（README 明示）。

`opfsAccessMode` 三个取值控制 OPFS 内部用哪种句柄：`"async"`（默认，普通异步文件 API）、`"sync"`（强制要求同步句柄，环境不满足即失败）、`"auto"`（能用同步就用同步，否则回退异步）。这套模式是近期（#832，本 HEAD 可见的最新特性提交之一）随 web-runtime 升级引入的，属于较新的能力。

另外两个后端作为背景知识：`"indexeddb"` 提供通用 KV 式存储；`"cross-origin"` 面向「跨源共享模型缓存」的实验场景，需装 Chrome 扩展且不支持程序化删除。

#### 4.4.2 核心流程

后端选择的传递链与一个「配置身份」的细节：

```text
用户 appConfig.cacheBackend / opfsAccessMode
  → getCacheOptions() 归一为 { cacheType, opfsAccessMode? }     （4.2）
  → 逐调用点流入 createArtifactCache / fetchTensorCache / has / delete
  → web-runtime 按 cacheType 分派到 CacheStorage / IndexedDB / OPFS / 扩展

areAppConfigsEqual(config1, config2):
  getCacheBackend 不同 → 不等
  两者都是 "opfs" 且 (opfsAccessMode ?? "async") 不同 → 不等
```

也就是说，**缓存后端（以及 opfs 的访问模式）是 AppConfig 身份的一部分**：两份配置后端不同就被视为不同配置。这个比较工具在 `src/utils.ts` 中定义（当前仓库内无其他调用方，属于公共工具函数），但它表达的约束值得记住——切换后端意味着切换一整套存储位置，旧后端里的缓存不会自动迁移。

#### 4.4.3 源码精读

OPFS 模式的权威注释：

[src/config.ts:L302-L311](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/config.ts#L302-L311) —— `opfsAccessMode` 字段文档（三模式语义、仅 opfs 后端消费）与 `OPFSAccessMode` 类型；L308 的 `@note` 同时给出「Cache API 是当前测试最充分后端」的官方立场。

配置比较中的缓存身份：

[src/utils.ts:L79-L88](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/utils.ts#L79-L88) —— `areAppConfigsEqual` 先比 `getCacheBackend`，再在双方都是 `"opfs"` 时用 `?? "async"` 补默认后比较 `opfsAccessMode`——注意 `??` 的兜底与 `getCacheOptions` 的「undefined 就不传」策略口径一致（都视未设置为 async）。

示例与文档中的 OPFS 注意事项：

[examples/cache-usage/README.md:L10-L11](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/cache-usage/README.md#L10-L11) —— 无 OPFS 支持的环境会报错；`opfsAccessMode` 的 `"auto"`/`"sync"` 用法说明。

[examples/cache-usage/README.md:L16-L18](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/cache-usage/README.md#L16-L18) —— DevTools 查看位置：产物分别在 `IndexedDB`、`Cache storage` 或 OPFS（源的私有文件系统）下；cross-origin 时由扩展展示。

示例里的后端开关，一行切换：

[examples/cache-usage/src/cache_usage.ts:L15-L29](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/cache-usage/src/cache_usage.ts#L15-L29) —— 直接改 `appConfig.cacheBackend` 这一个字符串，后续代码零改动（L17 的注释原话：CHANGE THIS TO SEE THE EFFECTS OF EACH, CODE BELOW DOES NOT NEED TO CHANGE），并用一串 if 往 console 打印当前后端名——这正是 4.2 所说「收口一处、全局生效」的直观体现。

需要说明边界：`"cache"`/`"indexeddb"`/`"opfs"`/`"cross-origin"` 各自内部如何落盘（OPFS 里目录结构长什么样、IndexedDB 的库表设计）属于 `@mlc-ai/web-runtime` 的实现，不在本仓库；本讲的结论到「WebLLM 传出了什么选项、DevTools 里在哪看」为止，深入请移步 web-runtime 源码。

#### 4.4.4 代码实践

1. **实践目标**：体验 OPFS 后端并观察其与 CacheStorage 的存放差异。
2. **操作步骤**：
   - 修改 [examples/cache-usage/src/cache_usage.ts:L18](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/cache-usage/src/cache_usage.ts#L18) 为 `appConfig.cacheBackend = "opfs";`，可再追加一行 `appConfig.opfsAccessMode = "auto";`（**示例代码**，仅为练习改动）。
   - 刷新前先在 DevTools → Application 左侧找到 **Storage**，执行「Clear site data」，确保 Cache API 的旧缓存不干扰判断。
   - 重新加载页面，观察 console 打印的 `Using Origin Private File System`。
3. **需要观察的现象**：Cache Storage 面板不再出现（或不再新增）`webllm/*` 三个桶；改看 Application → Storage → **Origin private file system**，出现 web-runtime 管理的文件结构；OPFS 下模型的占用计入源的存储配额。
4. **预期结果**：切换后端后模型重新完整下载（缓存不跨后端迁移，呼应 4.4.2 的配置身份）；二次加载同样提速。OPFS 面板中的具体目录命名由 web-runtime 决定，**待本地验证**。
5. 若浏览器无 OPFS 支持，观察并记录抛出的可用性错误（README L10 预告的行为），这本身就是有价值的实验结果。

#### 4.4.5 小练习与答案

**练习 1**：同源下先用 `"cache"` 后端装好模型，再切成 `"opfs"`，会复用已有缓存吗？
**答案**：不会。两类后端的物理存储互相独立，切过去后 `hasTensorInCache` 查的是 OPFS，找不到就只能重新下载。所以切换后端应当配合「清空站点数据或删除旧缓存」来做实验，避免误判。

**练习 2**：生产环境选型时，`"cache"` 与 `"opfs"` 各自的决策要点是什么？
**答案**：`"cache"`：默认、测试最充分、DevTools 可视化直观、环境兼容面最广——没有特殊理由就选它。`"opfs"`：追求大文件读写性能（尤其 sync access handle 可用时）、或应用其余部分已统一使用 OPFS 管理存储时；需接受环境不支持即报错的风险，并通过 `opfsAccessMode: "auto"` 做能力协商。

**练习 3**：`areAppConfigsEqual` 中为何单独对 `"opfs"` 比较 `opfsAccessMode`，而不是无条件比较该字段？
**答案**：`opfsAccessMode` 只在 opfs 后端下被消费（config.ts L302-L303 注释）；其他后端下这个字段是无关配置，两份 `"cache"` 后端的配置即使 `opfsAccessMode` 写得不同也不应被判为不等。条件比较让「语义上影响行为的字段」才参与身份判断。

## 5. 综合实践

把本讲全部内容串成一个**双后端对比实验**，产出一份对比笔记（这是本讲规格中规定的实践任务）。

**任务**：用同一个模型（示例默认 `Llama-3.2-1B-Instruct-q4f16_1-MLC`）分别以 `"cache"` 与 `"opfs"` 后端完整加载，对比存储位置、读取方式与加载耗时。

**步骤**：

1. 准备：`cd examples/cache-usage && npm install && npm start`，浏览器打开 `http://localhost:8889/`。
2. **第一轮（CacheStorage）**：保持 `cacheBackend = "cache"`，先注释掉示例 L71-L87 的删除与第三次 reload 段（便于观察已缓存状态），刷新页面等加载完成。记录：
   - DevTools → Application → Cache Storage 中三个桶的名称与条目 URL；
   - 首次加载耗时（`init-label` 显示的 `timeElapsed`）。
3. 清场：DevTools → Application → Storage → Clear site data（或恢复删除段跑一遍）。
4. **第二轮（OPFS）**：改 `cacheBackend = "opfs"`（可加 `opfsAccessMode: "auto"`），重复加载与观察，这次看 **Origin private file system**。
5. 二次加载验证：两轮各自再刷新一次页面，记录命中缓存的加载耗时。
6. 整理成对比笔记，建议表格模板：

| 维度 | `"cache"`（CacheStorage） | `"opfs"`（OPFS） |
|---|---|---|
| DevTools 查看位置 | | |
| 三个作用域如何呈现（桶名/目录） | | |
| 首次加载耗时 | | |
| 二次加载耗时（命中缓存） | | |
| 删除模型后存储变化 | | |
| 环境要求/踩坑 | | |

**预期结果**：两种后端都能完成「下载 → 命中 → 删除 → 重下」闭环；耗时上二次加载远快于首次；存储位置按 4.4.4 的观察呈现。所有具体数字与 OPFS 目录结构**待本地验证**——这正是笔记的价值：记下你机器上的真实数据。

**加分项**：`cross-origin` 后端（需装扩展）与 `indexeddb` 后端各跑一轮，把表格扩成四列；并在笔记末尾用一段话回答「如果我要做一个给多名用户分发的 WebLLM 应用，默认应该选哪个后端、为什么」。

## 6. 本讲小结

- 一次 `reload()` 涉及四类网络产物（config、wasm、tokenizer、权重），被拆进 `webllm/config`、`webllm/wasm`、`webllm/model` 三个缓存作用域，换来精确的管理粒度。
- `getCacheOptions` 是后端选择的唯一收口：`AppConfig.cacheBackend` 归一为 tvmjs 的 `cacheType`（缺省 `"cache"`），`opfsAccessMode` 仅在显式设置时转发（缺省按 async 处理）。
- `getTensorCacheAccessOptions` 给张量缓存选项注入 `cacheScope`；`createScopedArtifactCache` 把作用域作为位置参数传给 `createArtifactCache`——两条通道，同一个「作用域隔离」思想。
- 面向用户的缓存管理 API（`hasModelInCache` 只查权重；`deleteModelAllInfoInCache` = 权重 + tokenizer + wasm + config）全部构筑在这三个函数之上。
- wasm 在 `localhost` 或同源相对路径时刻意**不缓存**，服务本地调试场景。
- 选型结论：默认用测试最充分的 `"cache"`；追求大文件读写性能或统一 OPFS 存储时用 `"opfs"`（配 `"auto"` 模式协商能力）；后端切换不迁移缓存，且缓存后端属于 AppConfig 身份的一部分。

## 7. 下一步学习建议

- **下一讲（u4-l2）**：缓存管理 API 的用户视角详解——`deleteModelInCache` / `deleteModelWasmInCache` / `deleteChatConfigInCache` 的分项删除、`asyncLoadTokenizer` 的缓存优先策略，并动手做一个「模型管理面板」页面。
- **u4-l3**：缓存下载之后的信任问题——`integrity.ts` 的 SRI 完整性校验如何覆盖 config/wasm/tokenizer 三类产物（本讲在 `asyncLoadTokenizer` 中已见其挂载点）。
- **延伸阅读**：`@mlc-ai/web-runtime` 仓库中 `createArtifactCache` 与 OPFS sync access handle 的实现，验证本讲「web-llm 定策略、web-runtime 做实现」的分工边界；以及 MDN 对 [OPFS](https://developer.mozilla.org/en-US/docs/Web/API/File_System_API/Origin_private_file_system) 与 [存储配额与淘汰策略](https://developer.mozilla.org/en-US/docs/Web/API/Storage_API/Storage_quotas_and_eviction_criteria) 的说明，理解浏览器可能在存储压力下驱逐缓存。
