# 缓存管理 API：查询与删除

## 1. 本讲目标

上一讲（u4-l1）我们搞清楚了 WebLLM **往缓存里放了什么、放在哪三个作用域**；本讲解决反过来的一半问题：**作为应用开发者，如何查询和清理这些缓存**。学完本讲你应该能够：

1. 用 `hasModelInCache()` 判断某模型权重是否已下载到本地，并理解它「只查权重、不查 wasm 和 config」的语义边界。
2. 说出一组删除函数（`deleteModelAllInfoInCache` / `deleteModelInCache` / `deleteModelWasmInCache` / `deleteChatConfigInCache`）各自清理哪个作用域中的哪些 URL，并按需精确清理。
3. 讲清 `asyncLoadTokenizer()` 如何在 `reload()` 内部经缓存加载 tokenizer 文件（`fetchWithCache` 的「命中读缓存、未命中走网络并回填」语义），以及它与完整性校验的衔接。

这三个能力合起来，就是 WebLLM Chat 等 应用里「模型管理 / 存储空间清理」页面的全部底层支撑。

## 2. 前置知识

- **缓存三作用域**（上一讲核心结论）：WebLLM 把持久化产物分存于 `webllm/config`（聊天配置 JSON）、`webllm/wasm`（model library 即 wasm 文件）、`webllm/model`（权重分片与 tokenizer 文件）。默认后端是浏览器的 Cache Storage（`cacheType: "cache"`），也支持 indexeddb / opfs / cross-origin。
- **张量缓存 vs 产物缓存两套接口**：权重分片走 tvmjs 的「张量缓存」接口（`hasTensorInCache` / `deleteTensorCache` / `fetchTensorCache`，按 `cacheScope` 区分作用域）；普通文件（JSON、wasm、tokenizer）走「产物缓存」接口（`createArtifactCache` 创建的缓存对象的 `fetchWithCache` / `deleteInCache` 方法）。本讲两大类函数恰好各用一套。
- **ModelRecord 与 URL 规范化**：每个模型在 `AppConfig.model_list` 中有一条 `ModelRecord`，其 `model` 字段指向 HuggingFace 权重仓库。工具函数 `cleanModelUrl` 会把 `https://huggingface.co/USER/MODEL` 规范化为 `https://huggingface.co/USER/MODEL/resolve/main/`——缓存键就是这个规范化 URL，**删缓存和查缓存用的键必须与写入时一致**，这是本讲反复出现的一条暗线。
- **`new URL(relative, base)` 的相对解析**：浏览器标准 URL 语义——若 `base` 不以 `/` 结尾，相对路径会**替换掉 base 的最后一段**。理解它才能看懂本讲源码里 `new URL("tokenizer.json", baseUrl)` 的行为。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `src/cache_util.ts` | 缓存工具唯一实现文件（195 行） | 全部 6 个导出函数 + 3 个私有辅助函数 |
| `src/index.ts` | npm 库入口（barrel file） | L35-L41：面向用户导出的 5 个缓存管理 API |
| `src/engine.ts` | 引擎层 | L387-L397：`reload()` 内部调用 `asyncLoadTokenizer` 的位置 |
| `src/support.ts` | 杂项工具 | `cleanModelUrl`：URL 规范化 |
| `src/error.ts` | 错误类 | `ModelNotFoundError`、`UnsupportedTokenizerFilesError` |
| `tests/cache_util.test.ts` | 本讲对应的单测（318 行） | 用 mock 验证每个函数的调用参数与副作用 |
| `examples/cache-usage/` | 官方示例工程 | 可直接运行的「加载→查询→删除→重载」全流程演示 |

先给一张总览表，把 6 个函数的「作用域 × 动作」矩阵立在脑中，后文逐一展开：

| 函数 | 作用域 | 动作 | 操作的 URL 键 |
| --- | --- | --- | --- |
| `hasModelInCache` | `webllm/model` | 查询 | 规范化后的 `model` URL（权重张量缓存） |
| `deleteModelInCache` | `webllm/model` | 删除 | 权重张量缓存 + `tokenizer.json` + `tokenizer.model` |
| `deleteModelWasmInCache` | `webllm/wasm` | 删除 | `model_lib` URL |
| `deleteChatConfigInCache` | `webllm/config` | 删除 | `mlc-chat-config.json` 的 URL |
| `deleteModelAllInfoInCache` | 以上三个 | 删除 | 上面三行之和 |
| `asyncLoadTokenizer` | `webllm/model` | 读取（必要时下载） | `tokenizer.json` 或 `tokenizer.model` |

其中前 5 个从 `@mlc-ai/web-llm` 包公开导出，`asyncLoadTokenizer` 是内部函数（仅被 `engine.ts` 和测试使用）。

## 4. 核心概念与源码讲解

### 4.1 hasModelInCache：查询模型是否已缓存

#### 4.1.1 概念说明

「这个模型下载过没有？」是模型管理界面最基础的问题——它决定首屏是显示「立即体验」还是「需下载 800 MB」。WebLLM 把这个问题收口到一个函数：`hasModelInCache(modelId, appConfig?)`。

两个关键语义边界务必先建立：

1. **它只查权重张量缓存**。函数内部只探测 `webllm/model` 作用域中的权重分片，**不检查** wasm 库和聊天配置。这是一个实用主义的取舍：权重通常占模型总体积的 95% 以上（以 Llama-3.2-1B q4f16_1 为例，权重约 0.6 GB，wasm 约 30-60 MB，config 不足 1 KB），「权重在」在绝大多数场景就等价于「模型在」。反过来说，如果用户手动只删了权重（`deleteModelInCache`），`hasModelInCache` 会立刻返回 `false`，哪怕 wasm 和 config 还躺在缓存里。
2. **它以 URL 为键，而不是 modelId**。真正传给缓存层的键是 `ModelRecord.model` 经 `cleanModelUrl` 规范化后的 URL。这带来一个推论：**查询与写入必须使用同一个 `appConfig`**——如果你用自定义 `appConfig`（比如换了模型仓库镜像地址）加载的模型，查询时也要传同一个 `appConfig`，否则键对不上，永远查不到。

#### 4.1.2 核心流程

```text
hasModelInCache(modelId, appConfig?)
  ├─ appConfig 未传？ → 兜底为 prebuiltAppConfig
  ├─ findModelRecord(modelId, appConfig)
  │     └─ 在 model_list 中按 model_id 精确匹配；找不到 → 抛 ModelNotFoundError
  ├─ modelUrl = cleanModelUrl(modelRecord.model)
  │     └─ 补尾斜杠 + 补 "resolve/main/"，得到规范化 URL
  └─ return tvmjs.hasTensorInCache(modelUrl, {
         cacheScope: "webllm/model",
         cacheType,          // 由 appConfig.cacheBackend 归一化而来
         opfsAccessMode?,    // 仅 opfs 后端时有
     })
```

注意第二步：`hasModelInCache` 并非「尽力而为」的查询——`modelId` 不在 `model_list` 里会**直接抛错**而不是返回 `false`。「找不到这个模型的记录」和「这个模型没缓存」是两种性质不同的答案。

#### 4.1.3 源码精读

函数主体（[src/cache_util.ts:L69-L82](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/cache_util.ts#L69-L82)）：appConfig 缺省兜底 `prebuiltAppConfig`，查记录、清洗 URL 后把探测委托给 tvmjs 的 `hasTensorInCache`，选项由上一讲讲过的 `getTensorCacheAccessOptions` 组装（注入 `cacheScope: "webllm/model"` 与后端选项）。

私有辅助 `findModelRecord`（[src/cache_util.ts:L59-L67](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/cache_util.ts#L59-L67)）：在 `model_list` 里按 `model_id` 精确匹配。这是本讲全部 5 个管理函数共用的第一步——**所有缓存管理 API 都以 ModelRecord 为元数据来源**，记录不存在就无从谈起缓存键。

URL 规范化 `cleanModelUrl`（[src/support.ts:L91-L97](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/support.ts#L91-L97)）：先保证尾部斜杠，再在缺少 `resolve/.../` 时补上 HuggingFace 的 `resolve/main/` 分支。`https://huggingface.co/mlc-ai/demo-model` 由此变成 `https://huggingface.co/mlc-ai/demo-model/resolve/main/`——这就是写入权重缓存时用的键。

错误类 `ModelNotFoundError`（[src/error.ts:L1-L8](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L1-L8)）：提示信息明确指向 `model_list` 配置，属于「配置非法」类错误（错误体系详见 u7-l1）。

单测对这段行为有精确断言（[tests/cache_util.test.ts:L80-L91](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/cache_util.test.ts#L80-L91)）：mock 的 `hasTensorInCache` 被调用时，第一个参数正是规范化后的 URL，第二个参数是含 `cacheScope: "webllm/model"` 与 `cacheType: "cache"` 的选项对象——测试在这里把「函数只是 tvmjs 帮手的薄封装」这一事实钉死了。

#### 4.1.4 代码实践

实践目标：亲眼确认 `hasModelInCache` 只反映权重缓存，且与 Cache Storage 面板一一对应。

操作步骤（本机需支持 WebGPU 的浏览器）：

1. 进入 `examples/cache-usage/`，`npm install` 后 `npm start`（Parcel 在 8889 端口起服务）。
2. 打开页面，等待 `Llama-3.2-1B-Instruct-q4f16_1-MLC` 下载并完成首次对话（示例源码见 [examples/cache-usage/src/cache_usage.ts:L31-L57](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/cache-usage/src/cache_usage.ts#L31-L57)）。
3. 打开 DevTools → Application → Cache Storage，展开 `webllm/model` 作用域，确认里面有一批以 `params_shard_*` 命名的权重分片条目。
4. 在页面 Console 中执行：

   ```js
   const appConfig = webllm.prebuiltAppConfig;
   console.log(await webllm.hasModelInCache("Llama-3.2-1B-Instruct-q4f16_1-MLC", appConfig)); // true
   console.log(await webllm.hasModelInCache("Llama-3.2-3B-Instruct-q4f16_1-MLC", appConfig)); // false（从未加载过）
   ```

需要观察的现象：第一条为 `true` 且与面板中权重分片的存在一致；第二条为 `false`（查询一个未下载的模型是安全的，因为它在 `model_list` 里有记录）。

预期结果：`hasModelInCache` 的返回值与 `webllm/model` 作用域中权重分片的有无完全同步。首次加载与二次加载（命中缓存）的耗时差异可回看 u1-l2 的记录方法。

#### 4.1.5 小练习与答案

**练习 1**：调用 `hasModelInCache("not-a-real-model")` 会返回 `false` 吗？

答案：不会。它根本不返回，而是抛出 `ModelNotFoundError`——因为该 id 不在 `appConfig.model_list` 中，`findModelRecord`（cache_util.ts L59-L67）直接 throw。`false` 表示「记录存在但权重未缓存」，两者语义不同。

**练习 2**：为什么说「查询和加载必须传同一个 `appConfig`」？举例说明会出错的场景。

答案：缓存键是 `ModelRecord.model` 规范化后的 URL，而不是 modelId。若加载时用自定义 `appConfig`（例如把 `model` 指向镜像站 `https://mirror.example.com/mlc-ai/Llama-3.2-1B...`），查询时却用缺省 `prebuiltAppConfig`（指向 huggingface.co），两次计算出的键不同，`hasTensorInCache` 探测的将是另一个 URL，返回 `false`，尽管权重明明已缓存。

**练习 3**：`hasModelInCache` 返回 `true`，能推出「wasm 已缓存」吗？

答案：不能。函数只探测 `webllm/model` 作用域的权重张量缓存，不触碰 `webllm/wasm` 与 `webllm/config`。完全可能出现「权重在、wasm 不在」的组合（例如用户手动清过 wasm，或浏览器逐出策略只删了部分条目），此时 reload 仍需重新下载 wasm。

### 4.2 delete 族函数：三作用域的精确清理

#### 4.2.1 概念说明

删除侧一共 4 个导出函数，呈「1 个组合 + 3 个正交单件」结构：

- `deleteModelInCache`：清 `webllm/model`（权重张量 + 两个 tokenizer 文件）；
- `deleteModelWasmInCache`：清 `webllm/wasm`（model library）；
- `deleteChatConfigInCache`：清 `webllm/config`（`mlc-chat-config.json`）；
- `deleteModelAllInfoInCache`：按「model → wasm → config」顺序把上面三个全做一遍。

为什么拆成三个单件？因为三类产物的**更新频率和体积完全不同**：权重 GB 级、基本不变；wasm 随 WebLLM 版本升级而变；config 极小、模型微调后可能变。提供正交的单件删除，应用就能做出「只清 wasm 以便升级后重新拉取」「只清 config 以刷新元数据」这类细粒度操作，而不必每次都重下几个 GB 的权重。

三个必须建立的心智边界：

1. **删缓存 ≠ 卸载模型**。这些函数只动磁盘（Cache Storage / OPFS），完全不碰已加载进显存的 pipeline。要让引擎释放显存，仍需 `engine.unload()`（见 u2-l1）。删除缓存后引擎照常能对话——直到下次 `reload` 才会发现缓存空了而重新下载。
2. **删除以 URL 为键**。与查询同理：删除用的 `appConfig` 必须与加载时一致，否则删错（删不到）目标。
3. **cross-origin 后端不支持删除**。官方示例明确写了这一限制（[examples/cache-usage/src/cache_usage.ts:L66-L69](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/cache-usage/src/cache_usage.ts#L66-L69)）：`cacheBackend: "cross-origin"` 时直接跳过删除分支。

#### 4.2.2 核心流程

`deleteModelAllInfoInCache` 的组合逻辑（伪代码）：

```text
deleteModelAllInfoInCache(modelId, appConfig?)
  ├─ appConfig 兜底 prebuiltAppConfig
  ├─ await deleteModelInCache(modelId, appConfig)      # webllm/model：权重 + tokenizer×2
  ├─ await deleteModelWasmInCache(modelId, appConfig)  # webllm/wasm：model_lib
  └─ await deleteChatConfigInCache(modelId, appConfig) # webllm/config：mlc-chat-config.json
```

`deleteModelInCache`（最复杂的一个）：

```text
deleteModelInCache(modelId, appConfig?)
  ├─ findModelRecord → cleanModelUrl → modelUrl
  ├─ tvmjs.deleteTensorCache(modelUrl, {cacheScope: "webllm/model", ...})
  │     # 走"张量缓存"接口，删除该 URL 下的全部权重分片
  └─ modelCache = createScopedArtifactCache("webllm/model", appConfig)  # 走"产物缓存"接口
      ├─ modelCache.deleteInCache(URL("tokenizer.model", modelUrl))
      └─ modelCache.deleteInCache(URL("tokenizer.json",  modelUrl))
```

注意它**同时删除两个 tokenizer URL**，不区分模型实际使用哪一个——对不存在的键执行删除是无害的 no-op，一次把两种可能都清掉，代码反而更稳。这也呼应了 u4-l1 的论断：tokenizer 文件与权重同住 `webllm/model` 作用域，所以「删模型」必须把它俩一起管。

#### 4.2.3 源码精读

组合入口（[src/cache_util.ts:L84-L98](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/cache_util.ts#L84-L98)）：三行顺序 await，依次清 model、wasm、config 三个作用域——语义就是「把该模型的一切持久化痕迹抹掉」。

权重与 tokenizer 的清理（[src/cache_util.ts:L100-L117](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/cache_util.ts#L100-L117)）：前半段用 `tvmjs.deleteTensorCache` 删权重张量缓存（注意它接收的是与 `hasTensorInCache` 完全相同的参数对，查与删天然对齐）；后半段用 `createScopedArtifactCache` 建产物缓存对象，再对两个 tokenizer URL 逐个 `deleteInCache`。一个函数里同时出现两套缓存接口，是观察 u4-l1「张量/产物双轨」设计最好的标本。

聊天配置的清理（[src/cache_util.ts:L119-L132](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/cache_util.ts#L119-L132)）：作用域换成 `webllm/config`，删除的 URL 是 `modelUrl + "mlc-chat-config.json"`（`new URL("mlc-chat-config.json", modelUrl).href`，`modelUrl` 以 `/` 结尾故解析正确）。

wasm 的清理（[src/cache_util.ts:L134-L145](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/cache_util.ts#L134-L145))：作用域 `webllm/wasm`，键是 `modelRecord.model_lib`——**wasm 不经 `cleanModelUrl`**，因为 `model_lib` 本身就是完整文件 URL（通常指向 `https://raw.githubusercontent.com/mlc-ai/binary-mlc-llm-libs/...`），没有 HuggingFace 仓库语义。

公开导出清单（[src/index.ts:L35-L41](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/index.ts#L35-L41)）：库入口只导出这 5 个缓存管理函数（4 个删除 + 1 个查询），`getCacheOptions`、`asyncLoadTokenizer` 等辅助函数留在包内部——公共 API 面被刻意收窄。

单测对删除行为的断言（[tests/cache_util.test.ts:L110-L141](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/cache_util.test.ts#L110-L141)）：测试用 mock 的 `BaseCache` 把每次 `deleteInCache` 记录到 `state.deletes` 数组，然后断言 indexeddb 后端下 `deleteTensorCache` 收到正确的 URL+选项、且 `deletes` 数组恰好包含两个 tokenizer 条目。读这个用例可以不打开浏览器就验证删除参数。

#### 4.2.4 代码实践

实践目标：分别执行三个单件删除函数，在 DevTools 中观察三个作用域逐个变空，理解「精确清理」。

操作步骤：

1. 前置状态：u4.1.4 的实践已完成，模型三个作用域均有缓存。
2. 在页面 Console 依次执行（每次执行后刷新 Application 面板观察对应作用域）：

   ```js
   const id = "Llama-3.2-1B-Instruct-q4f16_1-MLC";
   const appConfig = webllm.prebuiltAppConfig;

   await webllm.deleteModelWasmInCache(id, appConfig);   // 观察 webllm/wasm 变空
   await webllm.deleteChatConfigInCache(id, appConfig);  // 观察 webllm/config 变空
   await webllm.hasModelInCache(id, appConfig);          // 仍为 true！权重还在
   await webllm.deleteModelInCache(id, appConfig);       // 观察 webllm/model 变空
   await webllm.hasModelInCache(id, appConfig);          // 现在为 false
   ```

3. 执行 `await webllm.CreateMLCEngine(id, { appConfig })`，观察 Network 面板。

需要观察的现象：删 wasm/config 后 `hasModelInCache` 仍返回 `true`（印证「只查权重」）；三步删完后 Cache Storage 中三个 `webllm/*` 作用域全部清空；重新 CreateMLCEngine 时 Network 面板再次出现大体积下载请求。

预期结果：作用域清空与函数调用一一对应；重新加载耗时回到首次下载水平。若你的 `cacheBackend` 设为 `cross-origin`，删除将不生效（该后端不支持删除，参照示例源码 L66-L69 的守卫）。

#### 4.2.5 小练习与答案

**练习 1**：只用一条语句让某模型「下次 reload 时重新下载权重，但复用已有 wasm」，该怎么写？

答案：只调 `deleteModelInCache(modelId, appConfig)`。它清掉权重张量与 tokenizer，不动 `webllm/wasm`。reload 时权重 miss 走网络，wasm 命中缓存直接复用。

**练习 2**：`deleteModelInCache` 为什么要同时删 `tokenizer.json` 和 `tokenizer.model` 两个 URL？删一个行不行？

答案：函数并不查看模型的 `tokenizer_files` 配置来决定用哪个，而是两个都删。这是因为对缓存中不存在的键做 `deleteInCache` 是无害操作；反过来若只删「猜的那一个」，一旦猜错（比如模型实际用 sentencepiece 的 `tokenizer.model`），就会留下孤儿缓存条目。宁可多删（最多两个小文件），不可漏删。

**练习 3**：调用 `deleteModelAllInfoInCache` 后，当前正在运行的引擎会立刻报错吗？

答案：不会。删除只影响持久化缓存，已加载进显存的权重、KV cache 与 pipeline 不受影响，对话可继续。差异要到下次 `reload` 才显现——那时缓存 miss，模型重新下载。若想同时释放显存，删除前应先 `await engine.unload()`（u2-l1）。

### 4.3 asyncLoadTokenizer：tokenizer 的缓存加载

#### 4.3.1 概念说明

`asyncLoadTokenizer` 回答的是「读」的问题：`reload()` 期间 tokenizer 文件从哪来？答案和权重一样——**先查缓存，miss 才走网络，拿到后回填缓存**。这由 tvmjs 产物缓存对象的 `fetchWithCache(url, format)` 一站式完成：语义等价于「带缓存的 fetch」。因此 tokenizer 的「缓存管理」其实没有独立 API——它在 `webllm/model` 作用域内，查询无入口（不值得单独查），删除则随 `deleteModelInCache` 一起完成。这也解释了 4.2 中「删模型必删两个 tokenizer URL」的另一半原因。

该函数的挑选逻辑体现了一条优先级：

1. **`tokenizer.json` 优先**（fast tokenizer 格式，自包含全部词表映射），用 `Tokenizer.fromJSON` 构造；
2. 否则回退 **`tokenizer.model`**（SentencePiece 原生格式），用 `Tokenizer.fromSentencePiece`，并打日志提醒：`added_tokens.json`、`tokenizer_config.json` 等伴生文件**会被忽略**，建议用 MLC 重新编译以获得 `tokenizer.json`；
3. 两者都没有 → 抛 `UnsupportedTokenizerFilesError`。

另外它与 u4-l3 将讲的完整性校验挂钩：若 `ModelRecord.integrity.tokenizer` 里登记了该文件的哈希，加载后会先 `verifyIntegrity` 再交付；校验失败且策略为 error 时抛 `IntegrityError`，tokenzier 不会被使用。

#### 4.3.2 核心流程

```text
asyncLoadTokenizer(baseUrl, config, appConfig, logger, integrity?)
  ├─ modelCache = createScopedArtifactCache("webllm/model", appConfig)
  ├─ config.tokenizer_files 含 "tokenizer.json"？
  │    ├─ 是 → modelCache.fetchWithCache(URL("tokenizer.json", baseUrl), "arraybuffer")
  │    │        → (可选) verifyIntegrity → Tokenizer.fromJSON(data) → return
  │    └─ 否 → 含 "tokenizer.model"？
  │         ├─ 是 → logger(建议改用 tokenizer.json 的提示)
  │         │        → fetchWithCache(...) → (可选) verifyIntegrity
  │         │        → Tokenizer.fromSentencePiece(data) → return
  │         └─ 否 → throw UnsupportedTokenizerFilesError(config.tokenizer_files)
```

调用时机：位于 `reload()` 主流程中 **GPU 初始化之后、权重下载（`fetchTensorCache`）之前**——tokenizer 很小（几 MB），先取它能让后续失败尽早发生；权重下载才是大头（[src/engine.ts:L387-L397](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L387-L397) 两行调用的先后顺序清晰可见）。

一个值得单独点出的细节是 `new URL(relative, baseUrl)` 的语义：若 `baseUrl` 以 `/` 结尾，`tokenizer.json` 拼接在其后；若**不以 `/` 结尾**，相对路径会替换掉 base 的最后一段。`engine.ts` 传入的是 `cleanModelUrl` 规范化后的 URL（必有尾斜杠），所以安全；但测试直接传了无尾斜杠的仓库 URL，于是 fetch 的地址变成了上一级目录下的 `tokenizer.json`（见 4.3.3 最后一条引用）。如果你在自己的代码里直接调用这个内部函数，务必先 `cleanModelUrl`。

#### 4.3.3 源码精读

函数主体（[src/cache_util.ts:L156-L194](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/cache_util.ts#L156-L194)）：先建 `webllm/model` 作用域的产物缓存；`tokenizer.json` 分支用 `fetchWithCache(url, "arraybuffer")` 取二进制并 `Tokenizer.fromJSON`；`tokenizer.model` 分支先 `logger` 输出那段著名的建议文案，再 `fromSentencePiece`；两个分支都先过 `maybeVerifyTokenizerIntegrity`；最后兜底抛错。JSDoc 注释写明了四个参数的推荐来源。

完整性校验的薄封装（[src/cache_util.ts:L47-L57](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/cache_util.ts#L47-L57)）：从 `integrity.tokenizer` 这个「文件名 → 哈希」映射里查当前文件名，查到才调 `verifyIntegrity`，查不到静默跳过——「配置了才校验」的开关语义。哈希格式与失败策略详见下一讲 u4-l3。

引擎侧调用点（[src/engine.ts:L387-L393](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L387-L393)）：`reload()` 中 `tvm.initWebGPU` 之后立即加载 tokenizer，五个实参依次是规范化 modelUrl、三层合并后的 ChatConfig、appConfig、logger、`modelRecord.integrity`——与函数签名的 JSDoc 一一对应。紧接着的 L394-L397 才开始 `fetchTensorCache` 下载权重。

错误类（[src/error.ts:L317-L322](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/error.ts#L317-L322)）：`UnsupportedTokenizerFilesError` 把收到的 `tokenizer_files` 原样带进错误消息，方便定位是哪份配置出了问题。

单测的断言（[tests/cache_util.test.ts:L143-L170](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/cache_util.test.ts#L143-L170)）：验证了「json 优先、sentencepiece 回退」的分支顺序，同时留下了那个 URL 解析的证据——mock 记录的 fetch 地址是 `https://huggingface.co/mlc-ai/tokenizer.json`（测试传入的 baseUrl 无尾斜杠，最后一段 `demo-model` 被替换掉了）。后续 L172-L317 的一组用例则覆盖了 integrity 的有/无、命中/未命中/失败五种组合。

#### 4.3.4 代码实践

实践目标：不打开浏览器，靠 mock 单测验证 `asyncLoadTokenizer` 的行为契约。

操作步骤：

1. 在仓库根目录执行 `npm install`（若未装），然后只跑这一个测试文件：`npx jest tests/cache_util.test.ts --coverage=false`。
2. 打开 [tests/cache_util.test.ts:L15-L54](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/tests/cache_util.test.ts#L15-L54)，读懂 mock 的构造：`jest.mock("@mlc-ai/web-runtime", ...)` 返回一个带 `__cacheState` 的假模块，`BaseCache.fetchWithCache` 把每次调用记入 `state.fetches`。
3. 对照 L143-L170 的用例，在纸面上推演：传入 `tokenizer_files: ["tokenizer.json"]` 时，`state.fetches[0]` 应记录什么？`Tokenizer.fromSentencePiece` 会被调用吗？

需要观察的现象：全部用例通过（绿）；`fetches` 数组里记录的 cache 作用域均为 `webllm/model`、format 均为 `arraybuffer`。

预期结果：mock 层验证了「json 优先、sp 回退、integrity 可选」三条契约。若要在真实浏览器里验证缓存行为，可在首次加载完成后断网，再 `engine.reload(modelId)`——tokenizer 与权重都会命中缓存，页面无需网络即可恢复对话（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `asyncLoadTokenizer` 没有出现在 `src/index.ts` 的导出里，用户却依然能控制 tokenizer 的缓存？

答案：它是引擎内部函数（engine.ts 直接 import 使用），属于实现细节，故不进公共 API 面。用户的控制途径有二：删除侧由 `deleteModelInCache` 顺带清理两个 tokenizer URL（4.2）；读取侧由 `reload()` 内部自动走 `fetchWithCache`，无需用户干预。测试通过直接 `import "../src/cache_util"` 访问它，这是仓内测试的特权。

**练习 2**：某模型的 `mlc-chat-config.json` 里 `tokenizer_files` 为 `["tokenizer.model", "tokenizer.json"]`（两个都有），实际会加载哪个？

答案：加载 `tokenizer.json`。函数用 `includes("tokenizer.json")` 先判 json 分支，命中即返回，根本不会看 `tokenizer.model`——顺序即优先级，json 自包含词表所以更可靠。

**练习 3**：`maybeVerifyTokenizerIntegrity` 在什么情况下会跳过校验？跳过是安全问题吗？

答案：`integrity` 参数缺省、或 `integrity.tokenizer` 映射中没有当前文件名的哈希时跳过（cache_util.ts L53-L56 的 `if (hash)` 判断）。跳过本身不是漏洞——完整性校验是**可选的** opt-in 机制（默认 `prebuiltAppConfig` 中多数记录未配置 integrity），配置了才校验；想对自定义模型启用，需在 ModelRecord 上填 `integrity.tokenizer`，详见下一讲。

## 5. 综合实践

**任务：做一个「模型管理面板」页面**——把本讲三个模块串成一个真实可用的小工具，这也是 WebLLM Chat「已下载模型管理」功能的雏形。

功能要求：

1. 列出 3-4 个候选模型（建议混搭大小，如 `Llama-3.2-1B-Instruct-q4f16_1-MLC`、`Llama-3.2-3B-Instruct-q4f16_1-MLC`、`Qwen2.5-1.5B-Instruct-q4f16_1-MLC`，均取自 `webllm.prebuiltAppConfig.model_list`）。
2. 页面加载时逐个调用 `hasModelInCache(id, appConfig)`，用 ✅/❌ 渲染缓存状态列。
3. 每行一个「删除全部缓存」按钮，点击后调用 `deleteModelAllInfoInCache(id, appConfig)`，随后**重新查询**该行状态，观察 ❌ 的翻转。
4. 一个「加载模型」按钮：`await webllm.CreateMLCEngine(id, { appConfig, initProgressCallback })`，加载完成后再查一次状态（应回到 ✅）。

参考骨架（示例代码，基于 [examples/cache-usage/src/cache_usage.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/cache-usage/src/cache_usage.ts) 改写，可直接放进该示例的 `src/cache_usage.ts`）：

```ts
// 示例代码：模型管理面板核心逻辑
const appConfig = webllm.prebuiltAppConfig;
const candidates = [
  "Llama-3.2-1B-Instruct-q4f16_1-MLC",
  "Llama-3.2-3B-Instruct-q4f16_1-MLC",
  "Qwen2.5-1.5B-Instruct-q4f16_1-MLC",
];

async function refreshRow(id: string) {
  const cached = await webllm.hasModelInCache(id, appConfig); // 只查权重
  document.getElementById(`status-${id}`)!.innerText = cached ? "✅ 已缓存" : "❌ 未缓存";
}

for (const id of candidates) {
  renderRow(id); // 渲染行：状态列 + 删除按钮 + 加载按钮
  document.getElementById(`del-${id}`)!.onclick = async () => {
    await webllm.deleteModelAllInfoInCache(id, appConfig); // config/wasm/权重三清
    await refreshRow(id);
  };
  await refreshRow(id);
}
```

验收清单（每条都对应本讲一个知识点）：

- 删除后状态列翻转为 ❌，且 DevTools → Application → Cache Storage 中 `webllm/model`、`webllm/wasm`、`webllm/config` 三个作用域里该模型的条目全部消失（对应 4.2）。
- 删除后点击「加载模型」，Network 面板重新出现对 huggingface 的权重分片下载，`initProgressCallback` 再次报告下载进度；加载完成后状态列回到 ✅（对应 4.1 的键一致性与 4.3 的 fetchWithCache 回填）。
- 若引擎已加载该模型，删除其缓存不影响当前对话；但再点「加载模型」（内部 reload）会重新下载（对应 4.2 练习 3 的「删缓存 ≠ 卸载」）。

注意：请使用支持 WebGPU 的浏览器（Chrome/Edge 较新版本）；显存不足 3B 模型时加载可能失败，这与缓存无关（选型参考 u1-l1 的 `vram_required_MB`）。

## 6. 本讲小结

- `hasModelInCache` 是「模型在不在本地」的唯一公开查询口：只探测 `webllm/model` 作用域的**权重张量**缓存，键是 `cleanModelUrl` 规范化后的 URL；`modelId` 不在 `model_list` 时抛 `ModelNotFoundError` 而非返回 `false`。
- 删除函数呈「1 组合 + 3 单件」：`deleteModelInCache`（权重+两个 tokenizer，`webllm/model`）、`deleteModelWasmInCache`（`model_lib`，`webllm/wasm`）、`deleteChatConfigInCache`（`mlc-chat-config.json`，`webllm/config`），`deleteModelAllInfoInCache` 顺序组合三者；cross-origin 后端不支持删除。
- 删缓存只动磁盘不动显存，已加载引擎照常工作，差异到下次 `reload` 才显现；查、删、载必须使用同一个 `appConfig` 才能对上缓存键。
- `asyncLoadTokenizer` 是 `reload()` 内部读取 tokenizer 的通道：`fetchWithCache` 命中缓存读本地、miss 走网络并回填；`tokenizer.json` 严格优先于 `tokenizer.model`（后者会打「伴生文件被忽略」警告），两者皆无抛 `UnsupportedTokenizerFilesError`；配了 `integrity.tokenizer` 哈希则先校验后交付。
- 测试 `tests/cache_util.test.ts` 通过 mock tvmjs 把每个函数的调用参数与副作用钉成契约，是无需浏览器的验证手段，也留下了 `new URL` 相对解析语义的有趣佐证。

## 7. 下一步学习建议

下一讲 **u4-l3 模型完整性校验（SRI 子资源完整性）** 将顺着本讲埋下的两条线展开：`maybeVerifyTokenizerIntegrity` 背后的 `verifyIntegrity` 如何流式计算 sha256、SRI 字符串（`sha256-<base64>`）如何解析、校验失败时 `IntegrityError` 与 `onFailure` 策略如何协同。之后 u4-l4 会把本单元四讲合拢成「接入自定义模型」的完整实战。

延伸阅读建议：对照 tvmjs（`@mlc-ai/web-runtime`）中 `ArtifactCacheTemplate` 的接口定义，理解 `fetchWithCache`/`deleteInCache` 在 Cache API 与 OPFS 两种后端下的不同实现；并回顾 `examples/cache-usage` 示例（本讲多次引用），它是这一组 API 的官方用法范本。
