# u3-l1 LLMChatPipeline 初始化与 tvmjs 运行时

## 1. 本讲目标

学完本讲,你应该能够:

- 说出 `reload()` 末尾那几行代码里,**tvmjs 实例、GPU 设备、权重** 分别是怎么来的:下载 wasm → `tvmjs.instantiate` → `detectGPUDevice` → `initWebGPU`。
- 解释三个 tvmjs 核心概念在管线中的角色:`DLDevice`(设备)、`VirtualMachine`(模型虚拟机)、`PackedFunc`(可调用函数句柄)。
- 按顺序走完 `LLMChatPipeline` 构造函数的 0~5 步:读 metadata → 解析模型 ABI → 绑定 prefill/decode 等函数 → 取权重 → 建 KV cache,并注明每一步的源码行号。
- 解释 `asyncLoadWebGPUPipelines()` 为什么是异步的、为什么排在管线构造**之后**、`reload()` 返回**之前**。
- 会用 `engine.getMaxStorageBufferBindingSize()` 和 `engine.getGPUVendor()` 查询当前浏览器 GPU,并讨论 `maxStorageBufferBindingSize` 对可运行模型规模的限制。

本讲是单元三「核心推理管线」的第一讲。u2-l1 讲过「引擎是管理者,管线才是干活的」——本讲就下钻到干活的那个对象,看它是怎么被「接线」出来的。

## 2. 前置知识

阅读本讲前,你需要具备以下认知(来自前面的讲义):

- **引擎生命周期**(u2-l1):`CreateMLCEngine` 等价于 `new MLCEngine()` + `await reload()`;`reload()` 内部按「查 ModelRecord → 下配置 → 下 wasm → 检测 GPU → 下权重 → 构造管线 → 编译 shader」的顺序完成加载。本讲把这些加粗步骤逐一展开。
- **模型分发三件套**(u1-l4、u1-l1):一个模型 = `model`(HuggingFace 权重仓库)+ `model_lib`(wasm 模型库)+ `overrides`(运行时覆盖)。`model_lib` 那个 wasm 文件正是本讲的主角之一。
- **缓存作用域**(u1-l2):`webllm/config`、`webllm/wasm`、`webllm/model` 三个 CacheStorage 作用域分别缓存配置、wasm、权重。

再补充解释几个本讲大量出现的术语:

- **wasm(WebAssembly)**:一种可在浏览器里以接近原生速度执行的字节码格式。MLC 编译器把模型编译成 TVM 字节码并打进一个 wasm 模块库,即 `ModelRecord.model_lib`。
- **tvmjs**:TVM 的 JavaScript/WebGPU 运行时,npm 包名为 `@mlc-ai/web-runtime`(见 [src/engine.ts:1](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1) 的 `import * as tvmjs from "@mlc-ai/web-runtime"`)。它负责加载 wasm、管理 GPU 缓冲区、编译 shader。web-llm 本身不实现推理,只通过 tvmjs 驱动它。
- **PackedFunc**:TVM 运行时的通用函数抽象——一个可以把参数「打包」传进 TVM 运行时(无论实现在 wasm、JS 还是 GPU 内核里)的可调用对象。拿到一个 `PackedFunc`,就可以像调普通函数一样 `func(a, b, c)`。
- **VirtualMachine(VM)**:TVM 的 relax 虚拟机。wasm 模块库里装的是编译后的模型字节码,VM 负责解释执行;`vm.getFunction("prefill")` 就是从中取出名为 `prefill` 的函数入口。
- **DLDevice**:来自 DLPack 标准的设备描述符(device_type + device_id)。`tvm.webgpu()` 返回一个指向 WebGPU 设备的 `DLDevice`,之后所有张量创建都指定它,张量就驻留在 GPU 显存里。
- **storage buffer**:WebGPU 里的可读写存储缓冲区。TVM 的 WebGPU 后端把张量(权重、KV cache、logits)放在 storage buffer 中供计算着色器读写,浏览器对**单个 storage buffer 的最大尺寸**有限制,即 `maxStorageBufferBindingSize`。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [src/engine.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts) | 引擎层 | `reloadInternal` 中创建 tvmjs 实例、检测 GPU、构造管线、触发 shader 预热的片段;`getMaxStorageBufferBindingSize`/`getGPUVendor` |
| [src/llm_chat.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts) | 推理管线 | `LLMChatPipeline` 构造函数(0~5 步)、VM 函数注册表、ABI 解析、`asyncLoadWebGPUPipelines`、`dispose` |
| [src/types.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts) | 公共类型 | `MLCEngineInterface` 中两个 GPU 查询方法的契约 |
| [examples/get-started/src/get_started.ts](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/examples/get-started/src/get_started.ts) | 官方示例 | 本讲实践任务的代码基底 |
| `node_modules/@mlc-ai/web-runtime`(外部依赖) | tvmjs 运行时 | 不在本仓库内;本讲只依据 web-llm 对它的**调用方式**来描述其行为,不深入其内部实现 |

## 4. 核心概念与源码讲解

### 4.1 tvmjs 运行时绑定:从 wasm 到 GPU 设备

#### 4.1.1 概念说明

浏览器本身不认识「LLM」,也不认识 TVM。要在网页里跑模型,需要先把 `ModelRecord.model_lib` 指向的 wasm 文件下载下来,实例化成一个 tvmjs 运行时实例(`tvmjs.Instance`);再向浏览器申请一个 WebGPU 设备,把它「挂」到这个实例上。此后,所有张量分配、内核调用都通过这个实例进行。

这一步解决的问题是:**给模型准备一个「CPU 侧的解释器 + GPU 侧的算力」组合**。wasm 提供 CPU 侧的 VM 与调度逻辑,WebGPU 设备提供真正的矩阵算力,两者由 tvmjs 桥接。

#### 4.1.2 核心流程

`reloadInternal` 前半段(下钻 u2-l1 留下的悬念):

```text
modelRecord.model_lib (wasm URL)
        │
        ├─ fetch wasm(localhost/同源直连,否则走 webllm/wasm 缓存)
        │
        ├─ tvmjs.instantiate(wasm, createPolyfillWASI(), logger)   → tvm 实例
        │
        ├─ tvm.registerInitProgressCallback(...)   ← 权重下载进度从这里上报
        │
        ├─ tvmjs.detectGPUDevice()
        │       ├─ 失败 → WebGPUNotAvailableError
        │       └─ required_features 逐项检查(如 shader-f16)
        │
        ├─ device.lost 挂钩(显存不足导致设备丢失时卸载引擎)
        │
        └─ tvm.initWebGPU(device)   ← GPU 设备正式绑定到 tvm 实例
                │
                └─ 之后管线里 tvm.webgpu() 即可取回 DLDevice
```

注意顺序:**先有 wasm 实例,再检测 GPU,最后才下载权重**。GPU 检测放在权重下载之前,是为了在设备不支持时尽早失败,避免白下几个 GB 的权重。

#### 4.1.3 源码精读

**① 实例化 tvmjs**。wasm 下载与实例化位于 [src/engine.ts:299-341](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L299-L341),关键三行:

```typescript
const wasm = new Uint8Array(wasmSource);
const tvm = await tvmjs.instantiate(
  wasm.buffer,
  tvmjs.createPolyfillWASI(),
  this.logger,
);
```

这段代码把下载到的 ArrayBuffer 实例化为 tvmjs 运行时实例 `tvm`。`createPolyfillWASI()` 提供浏览器版的 WASI 系统调用桩(wasm 里的 TVM 运行时需要一些「系统调用」,浏览器没有,就用 JS 模拟)。随后 [src/engine.ts:343-345](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L343-L345) 把引擎的 `initProgressCallback` 注册进 tvm 实例——你在 u1-l2 看到的权重下载百分比,就是 tvmjs 在下载时经这个回调上报的。

**② 检测 GPU 并检查特性**。[src/engine.ts:347-367](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L347-L367):

```typescript
const gpuDetectOutput = await tvmjs.detectGPUDevice();
if (gpuDetectOutput == undefined) {
  throw new WebGPUNotAvailableError();
}
...
if (modelRecord.required_features !== undefined) {
  for (const feature of modelRecord.required_features) {
    if (!gpuDetectOutput.device.features.has(feature)) {
      if (feature == "shader-f16") {
        throw new ShaderF16SupportError();
      }
      throw new FeatureSupportError(feature);
    }
  }
}
```

这段代码调用 tvmjs 的 `detectGPUDevice()` 拿到 `device`(WebGPUDevice)与 `adapterInfo`(适配器信息),然后逐项核对 `ModelRecord.required_features`(u1-l1 讲过 `q4f16_1` 模型通常要求 `shader-f16`)。缺特性直接抛错——这就是 u1-l2 说的「缺 shader-f16 会抛错」的精确出处。

**③ 绑定设备与设备丢失处理**。[src/engine.ts:369-385](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L369-L385) 先给 `gpuDetectOutput.device.lost` 挂一个 Promise 回调(显存不足等导致设备丢失时,记录错误并调用 `this.unload()`),然后一行 `tvm.initWebGPU(gpuDetectOutput.device)` 把设备交给 tvm 实例。

**④ 管线侧取回 DLDevice**。进入 `LLMChatPipeline` 构造函数后,[src/llm_chat.ts:223](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L223) 只有一行:

```typescript
this.device = this.tvm.webgpu();
```

这行把绑定在 tvm 实例上的 WebGPU 设备以 `DLDevice` 形式取回,存为字段 `device`([src/llm_chat.ts:66](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L66) 声明类型为 `tvmjs.DLDevice`)。之后管线里所有要落在显存里的张量(如 [src/llm_chat.ts:473-481](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L473-L481) 的采样用张量)都传 `this.device` 创建;`sync()` 也是对它调用([src/llm_chat.ts:2239-2242](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L2239-L2242))。

**⑤ 对用户暴露的 GPU 查询**。引擎层把设备信息包装成两个异步方法,位于 [src/engine.ts:1156-1192](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1156-L1192):

```typescript
async getMaxStorageBufferBindingSize(): Promise<number> {
  const gpuDetectOutput = await tvmjs.detectGPUDevice();
  ...
  const maxStorageBufferBindingSize =
    gpuDetectOutput.device.limits.maxStorageBufferBindingSize;
  const defaultMaxStorageBufferBindingSize = 1 << 30; // 1GB
  if (maxStorageBufferBindingSize < defaultMaxStorageBufferBindingSize) {
    log.warn(`WARNING: the current maxStorageBufferBindingSize ...`);
  }
  return maxStorageBufferBindingSize;
}
```

`getMaxStorageBufferBindingSize()` 返回设备允许的单个 storage buffer 最大字节数,若小于 1GB 会打警告并列出一批仍可运行的小模型;`getGPUVendor()` 返回适配器厂商(如 apple、qualcomm)。两者都声明在 `MLCEngineInterface` 上([src/types.ts:208-218](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/types.ts#L208-L218)),所以 Worker 版引擎同样支持(经 [src/web_worker.ts:306-308](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/web_worker.ts#L306-L308) 的消息转发)。注意它们各自**重新**调用一次 `detectGPUDevice()`,并不依赖已加载的模型。

#### 4.1.4 代码实践

1. **实践目标**:打印当前浏览器的 GPU 厂商与 `maxStorageBufferBindingSize`,并理解该值如何约束可运行的模型。
2. **操作步骤**:复制 `examples/get-started` 为一个新目录(或在其中临时修改),把 `src/get_started.ts` 的 `main()` 改为(**示例代码**):
   ```typescript
   import * as webllm from "@mlc-ai/web-llm";

   async function main() {
     const engine = await webllm.CreateMLCEngine(
       "Llama-3.2-1B-Instruct-q4f32_1-MLC",
       { logLevel: "INFO" },
     );
     const vendor = await engine.getGPUVendor();
     const maxBinding = await engine.getMaxStorageBufferBindingSize();
     console.log("GPU vendor:", vendor);
     console.log(
       "maxStorageBufferBindingSize:",
       maxBinding,
       `(${(maxBinding / (1 << 20)).toFixed(0)} MB)`,
     );
   }
   main();
   ```
   然后 `npm start` 打开页面(需支持 WebGPU 的浏览器)。
3. **需要观察的现象**:控制台输出厂商名与字节数;若该值低于 1GB,还会出现源码里那条 WARNING 及一串「仍可用的小模型」名单。
4. **预期结果**:桌面独显/集显通常上报 1GB 或更高;部分手机或虚拟机可能只有 128MB~256MB。**待本地验证**(具体数值随浏览器与驱动不同而不同)。
5. **讨论**——为什么这个值限制模型规模:权重与 KV cache 都以 WebGPU storage buffer 驻留显存,而单个 storage buffer 不能超过 `maxStorageBufferBindingSize`。以 4bit 量化的 8B 模型为例,权重总量约 \( 8\times10^9 \times 0.5\,\text{byte} = 4\,\text{GB} \),若设备只允许单 buffer 1GB,tvmjs 就必须把权重切成多块或直接放不下——这正是源码警告列表里全是 1B~8B 小模型的原因。选型时可以用 `vram_required_MB`(u1-l1)与该限制互相印证。

#### 4.1.5 小练习与答案

**练习 1**:`tvmjs.instantiate` 与 `tvm.initWebGPU` 各自解决什么问题?为什么缺一不可?

**答案**:`instantiate` 生成 CPU 侧运行时实例——VM、调度逻辑、参数缓存都在 wasm 里;`initWebGPU` 把浏览器申请到的 WebGPU 设备绑定到该实例,使张量能分配到显存、内核能提交到 GPU。只有前者则模型在 CPU 上无算力可用,只有后者则没有可执行的模型字节码。

**练习 2**:为什么 `detectGPUDevice` 放在权重下载(`fetchTensorCache`)**之前**?

**答案**:GPU 检测很便宜(只是请求适配器与设备),而权重下载可能动辄数 GB。先检测可以在设备不支持 WebGPU 或缺少 `shader-f16` 特性时立刻抛 `WebGPUNotAvailableError`/`ShaderF16SupportError`,避免用户白白等待大文件下载。

**练习 3**:`engine.getMaxStorageBufferBindingSize()` 需要等模型加载完才能调用吗?

**答案**:不需要。看 [src/engine.ts:1156-1159](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L1156-L1159),它内部自己调用 `tvmjs.detectGPUDevice()`,与引擎已加载的模型无关;即使 `new MLCEngine()` 后立即调用也能工作(仅要求浏览器支持 WebGPU)。

### 4.2 LLMChatPipeline 构造流程:VirtualMachine、PackedFunc 与 KV cache

#### 4.2.1 概念说明

`LLMChatPipeline` 是真正执行推理的对象(u2-l1:「管线才是干活的」)。它的构造函数([src/llm_chat.ts:179-484](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L179-L484))做的不是计算,而是**接线**:从 wasm 模块里把所有要用的函数句柄(PackedFunc)取出来、按模型元数据决定要创建哪些状态对象(KV cache / RNN 状态)、把权重从 tvm 参数缓存装进显存。构造完成后,管线就有了执行一次 prefill/decode 所需的全部「零部件」。

这里有个关键认知:**函数句柄的获取不等于 shader 的编译**。`vm.getFunction("prefill")` 只是从 VM 里登记的符号表中拿到入口,真正调用时才需要对应的 GPU 内核就绪——这就是 4.3 节 `asyncLoadWebGPUPipelines` 存在的原因。

#### 4.2.2 核心流程

构造函数源码里自带编号注释(`0.`~`5.`,其中「Create cache」被标了两次 5),完整流程:

```text
0. Setting attributes      存配置/tokenizer,建 Conversation,读 tokenizer_info
        │
1. Create VM               tvm.createVirtualMachine(device) → vm
        │                  vm.getFunction("_metadata")() → 模型元数据 JSON
        │                  解析 kv_stateKind;按函数可用性 resolveModelABI
        │
2. Bind VM functions       按解析出的 ABI 绑定 this.prefill / this.decoding
        │                  再绑定 embed、采样族、image_embed(可选)
        │
3. Load parameters         按 metadata.params 的名字列表取权重 → this.params
        │
4. Read comp configs       metadata.prefill_chunk_size → this.prefillChunkSize
        │
5. Consolidate KV settings 校验 context_window_size / sliding_window_size /
        │                  attention_sink_size 三者组合合法性
        │
5. Create cache            取 vm.builtin.* 全局函数;按需 create KV cache /
        │                  RNN 状态 / batch 用张量;resetChat();建采样张量
        │
     endScope
```

其中「ABI 解析」是一个小决策树:模型元数据声明自己的状态类型 `kv_state_kind`(\( \in \{\text{kv\_cache}, \text{rnn\_state}, \text{hybrid}\} \)),再结合 wasm 里实际导出了哪些函数,决定 prefill/decode 用单序列内核还是 batch 内核、要不要建 KV cache / RNN 状态:

```text
kv_state_kind = "kv_cache"  → Transformer:prefill+decode 或 batch_prefill+batch_decode,建 KV cache
kv_state_kind = "rnn_state" → 线性 RNN(如 Mamba 类):建 RNN 状态,无 KV cache
kv_state_kind = "hybrid"    → 混合:强制 batch 内核,KV cache 与 RNN 状态都要
kv_state_kind = "none"      → 聊天管线不支持,直接抛错
```

KV cache 是这里最「贵」的对象,其显存量可以用标准 Transformer 公式估算(示例推导,非源码中的计算):

\[
\text{KV cache 显存} \approx 2 \times n_{\text{layer}} \times n_{\text{kv head}} \times d_{\text{head}} \times L_{\max} \times b
\]

其中因子 2 对应 K 与 V 两份,\( L_{\max} \) 是最大序列长度(即下面源码里的 `maxTotalSeqLen`),\( b \) 是每个元素字节数(f16 为 2)。它与 4.1 的 `maxStorageBufferBindingSize` 相互制约:窗口开得越大,KV cache 越大,越容易撞上单 buffer 上限或显存总量。

#### 4.2.3 源码精读

**① 字段声明:两类函数要分清**。[src/llm_chat.ts:60-87](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L60-L87) 声明了管线的全部「零件」,其中 PackedFunc 分两组:

```typescript
private tvm: tvmjs.Instance;
private device: tvmjs.DLDevice;
private vm: tvmjs.VirtualMachine;
private prefill: tvmjs.PackedFunc;
private decoding: tvmjs.PackedFunc;
...
// Functions related to PagedKVCache
private fclearKVCaches: tvmjs.PackedFunc;
```

- **VM 模块函数**(`prefill`、`decoding`、`embed`、采样族、`image_embed`):来自 `vm.getFunction(name)`,是**这个模型 wasm 库导出的**模型专用函数;
- **运行时内建**(`fclearKVCaches` 等 `fKVCache*`):来自 `tvm.getGlobalFunc("vm.builtin....")`,是 **tvmjs 运行时自带的通用功能**,与具体模型无关。

**② 创建 VirtualMachine 并读元数据**。[src/llm_chat.ts:225-252](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L225-L252):

```typescript
tvm.beginScope();
this.vm = this.tvm.detachFromCurrentScope(
  this.tvm.createVirtualMachine(this.device),
);
...
const fgetMetadata = this.vm.getFunction("_metadata");
const ret_value = fgetMetadata();
const metadata = JSON.parse(ret_value.toString());
this.kvStateKind = this.parseKVStateKind(metadata.kv_state_kind);
```

这段代码在 `device` 上创建 VM。`beginScope`/`detachFromCurrentScope` 是 tvmjs 的**作用域式内存管理**:scope 内创建的 TVM 对象会在 `endScope` 时统一释放,`detachFromCurrentScope` 把对象「搬出」作用域使其存活到字段里——构造函数里反复出现的这个模式,含义都是「这个对象我要长期持有」。接着调用模型自带的 `_metadata` 函数拿到一段 JSON:里面有权重参数名列表(`params`)、`prefill_chunk_size`、`kv_state_kind` 等编译期信息。**模型如何描述自己,管线就如何配置自己**。

**③ VM 函数注册表与 ABI 解析**。构造函数先用 [src/llm_chat.ts:231-246](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L231-L246) 把 14 个候选函数名批量装进注册表。装表的实现在 [src/llm_chat.ts:1290-1306](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1290-L1306):

```typescript
private static loadVMFunctionRegistry(vm, names): VMFunctionRegistry {
  const registry: VMFunctionRegistry = {};
  for (const name of names) {
    try {
      const func = vm.getFunction(name) as unknown;
      if (typeof func === "function") {
        registry[name] = func as tvmjs.PackedFunc;
      }
    } catch {
      // no-op for unexported symbols
    }
  }
  return registry;
}
```

注意 `try/catch`:wasm 里没导出的符号直接跳过,注册表只记录「实际存在」的函数。随后 [src/llm_chat.ts:254-259](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L254-L259) 调用 `resolveModelABI`(决策树实现在 [src/llm_chat.ts:1365-1480](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1365-L1480),约 100 行纯决策逻辑,建议通读),产出一个 `ResolvedModelABI`([src/llm_chat.ts:50-58](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L50-L58))。解析结果会打进一条日志([src/llm_chat.ts:267-275](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L267-L275)),形如 `Resolved model ABI: {"kv_state_kind":"kv_cache","prefill":"prefill","decode":"decode","states":["kv_cache"]}`——**这条日志就是本讲实践要抓的现象之一**。

**④ 按 ABI 绑定 prefill/decoding 与采样族**。[src/llm_chat.ts:277-335](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L277-L335):

```typescript
// 2. Bind VM functions according to the resolved ABI
this.prefill = this.tvm.detachFromCurrentScope(
  LLMChatPipeline.getRequiredVMFunctionByName(
    this.resolvedModelABI.prefillFunctionName,   // "prefill" 或 "batch_prefill"
    vmFunctionRegistry,
  ),
);
this.decoding = this.tvm.detachFromCurrentScope(
  LLMChatPipeline.getRequiredVMFunctionByName(
    this.resolvedModelABI.decodeFunctionName,    // "decode" 或 "batch_decode"
    vmFunctionRegistry,
  ),
);
```

两个要点:第一,**字段名与实际函数名可能不一致**——字段永远叫 `this.prefill`/`this.decoding`,但绑定的可能是 `batch_prefill`/`batch_decode`;第二,必选函数缺失时 `getRequiredVMFunctionByName`([src/llm_chat.ts:1308-1326](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1308-L1326))会抛错并列出全部可用候选名,这是排查「wasm 与代码版本不匹配」的第一现场。同一区块还绑定了 `embed`、`fsoftmaxWithTemperature`、`fsampleWithTopP`、`fapplyPenalty`、`fapplyLogitBias`、`fapplyBitmask`(语法约束,见 u6-l3)、`fargsortProbs`,而 `image_embed` 是**唯一可选**的([src/llm_chat.ts:336-341](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L336-L341)):非视觉模型没有它,只打一条日志。

**⑤ 装载权重**。[src/llm_chat.ts:343-350](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L343-L350):

```typescript
const paramNames: string[] = [];
metadata.params.forEach((param: any) => { paramNames.push(param.name); });
this.params = this.tvm.detachFromCurrentScope(
  this.tvm.getParamsFromCacheByName(paramNames),
);
```

权重在引擎侧由 `tvm.fetchTensorCache(...)` 下载进 tvm 参数缓存([src/engine.ts:394-397](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L394-L397)),管线侧按 metadata 给出的参数名列表一次性取出、组装成 `this.params`(一个 KVStore 式 TVMObject)。**下载与装载是分工的:引擎管下载,管线管装载。**

**⑥ 窗口配置校验与 KV cache 创建**。[src/llm_chat.ts:359-394](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L359-L394) 校验 `context_window_size` 与 `sliding_window_size` 互斥、滑动窗口必须配 `attention_sink_size`,非法组合分别抛 `WindowSizeConfigurationError`/`AttentionSinkSizeError`/`WindowSizeSpecificationError`。然后 [src/llm_chat.ts:396-440](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L396-L440) 先取 6 个 `vm.builtin.*` 运行时内建(清理/增删序列/前后向钩子/滑动窗口开关),再按需创建分页 KV cache:

```typescript
const maxTotalSeqLen =
  this.slidingWindowSize != -1 ? this.slidingWindowSize : this.contextWindowSize;
...
this.kvCache = this.tvm.detachFromCurrentScope(
  createKVCache(
    this.tvm.makeShapeTuple([defaultMaxNumSequence]), // max_num_sequence = 1
    this.tvm.makeShapeTuple([maxTotalSeqLen]),        // max_total_sequence_length
    this.tvm.makeShapeTuple([this.prefillChunkSize]), // prefill_chunk_size
    this.tvm.makeShapeTuple([defaultPageSize]),       // page_size = 16
    ...
  ),
);
```

KV cache 在构造期就按 `maxTotalSeqLen` **一次性分配满**——这就是为什么 u1-l4 说「上下文窗口越大越吃显存」、也是 `device.lost` 多发于 `reload()` 的原因(见 [src/engine.ts:369-374](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L369-L374) 的注释)。RNN 状态([src/llm_chat.ts:442-454](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L442-L454))与 batch ABI 专用的 `prefillLogitPositions` 张量([src/llm_chat.ts:456-460](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L456-L460))同理按需创建。

**⑦ 收尾**。[src/llm_chat.ts:462-483](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L462-L483):调用 `resetChat()`(给 KV cache 注册 0 号序列)、创建 GPU 侧采样用的 `sampleIndicesDevice`/`topPDevice` 小张量,最后 `tvm.endScope()` 释放所有临时对象。

**⑧ 这些句柄后来怎么被用**。以 prefill 的真实调用为例,[src/llm_chat.ts:1528-1537](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1528-L1537):

```typescript
private invokePrefill(allEmbeddings: tvmjs.Tensor, inputDataLen: number): any {
  if (this.resolvedModelABI.prefillABI === "single") {
    return this.prefill(allEmbeddings, this.getSingleStateForABI(), this.params);
  }
  ...
}
```

`this.prefill(输入张量, KV状态, 权重)` —— PackedFunc 像普通函数一样被调用,实现在 wasm+GPU 里。`invokeDecode`([src/llm_chat.ts:1568-1574](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L1568-L1574))同理调用 `this.decoding`。卸载时 `dispose()`([src/llm_chat.ts:486-508](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L486-L508))按相反顺序逐个释放 VM、KV cache、params、tvm 实例乃至 tokenizer。

#### 4.2.4 代码实践

1. **实践目标**:亲手标注管线构造函数中每个关键成员的「来源行号」,并从运行日志验证你的标注。
2. **操作步骤**:
   - 打开 [src/llm_chat.ts:179-484](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L179-L484),仿照下表补全(已给出前三行,其余自己填;这是**源码阅读实践**,不修改源码,把表格抄到自己的笔记里即可):

     | 成员 | 来源 | 源码位置 |
     | --- | --- | --- |
     | `this.device` | `tvm.webgpu()`,设备已在引擎侧 `initWebGPU` 绑定 | llm_chat.ts:223 |
     | `this.vm` | `tvm.createVirtualMachine(this.device)` | llm_chat.ts:226-229 |
     | `this.prefill` | 注册表按 `resolvedModelABI.prefillFunctionName` 取(名字可能是 `prefill` 或 `batch_prefill`) | llm_chat.ts:278-283 |
     | `this.decoding` | (自己填写) | (自己填写) |
     | `this.embed` / 采样族 | (自己填写) | (自己填写) |
     | `this.image_embed` | (自己填写,注意它是可选的) | (自己填写) |
     | `this.params` | (自己填写,提示:与 engine.ts:394-397 呼应) | (自己填写) |
     | `this.kvCache` | (自己填写,记下 `maxTotalSeqLen` 用的是哪个窗口值) | (自己填写) |
     | `fclearKVCaches` 等 | `tvm.getGlobalFunc("vm.builtin....")` 运行时内建 | llm_chat.ts:398-417 |

   - 用 4.1.4 的页面(保留 `logLevel: "INFO"`)加载模型,在控制台抓出这三条管线日志:`token_postproc_method: ...`([llm_chat.ts:220](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L220))、`Resolved model ABI: {...}`([llm_chat.ts:267-275](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L267-L275))、`Using prefillChunkSize: ...` 与 `Using contextWindowSize: ...`([llm_chat.ts:354](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L354)、[llm_chat.ts:378](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L378))。
3. **需要观察的现象**:日志中的 `prefill`/`decode` 字段值,应与你标注的 `prefillFunctionName` 一致;`prefillChunkSize` 应与该模型 wasm 编译时的 `prefill_chunk_size` 一致(u1-l4 讲过的 `cs1k` 文件名后缀即 1k chunk)。
4. **预期结果**:一个普通 Llama 类模型输出 `kv_state_kind:"kv_cache"`、`states:["kv_cache"]`。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**:`vm.getFunction("prefill")` 与 `tvm.getGlobalFunc("vm.builtin.kv_state_clear")` 有什么区别?

**答案**:前者从**模型 wasm 库的 VM 符号表**取函数,是模型专用内核(prefill/decode/embed 等);后者从 **tvmjs 运行时的全局注册表**取内建功能,与模型无关(分页 KV cache 的清理、序列增删等管理操作)。

**练习 2**:为什么 `this.prefill` 字段绑定的函数可能实际叫 `batch_prefill`?这个设计带来什么好处?

**答案**:因为绑定按 `resolvedModelABI.prefillFunctionName` 进行,而 ABI 由「元数据声明的状态类型 + wasm 实际导出的函数」共同决定;混合状态模型强制使用 batch 内核。好处是下游代码(`invokePrefill` 等)只面对稳定的字段名,不必关心底层内核变体,新内核形态(如 hybrid)只需扩展 ABI 解析,不用改全部调用点。

**练习 3**:构造函数里大量出现的 `detachFromCurrentScope` 如果漏写,会发生什么?

**答案**:`beginScope`/`endScope` 之间的 TVM 对象在 `endScope` 时会被统一释放;漏写 detach 意味着 VM、PackedFunc、KV cache 等长期对象在构造函数结束的 `endScope`([llm_chat.ts:483](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L483))处被销毁,后续推理时用到已释放对象而报错。它是 tvmjs「作用域即生命周期」模型下声明所有权的手段。

### 4.3 WebGPU shader 异步预热:asyncLoadWebGPUPipelines

#### 4.3.1 概念说明

wasm 模块库里装的是模型的**字节码与内核描述**,而 GPU 真正执行的是编译后的 **WebGPU compute pipeline**(内含 WGSL 着色器)。创建 pipeline 需要驱动编译着色器,这是个可能耗费数秒的**异步**过程,且由浏览器实现(WebGPU 标准层面也允许实现缓存编译结果)。

`asyncLoadWebGPUPipelines()` 就是把这个模型的全部 GPU 内核一次性提前编译好——「预热」。如果跳过预热,第一次调用 prefill 时同样要现编译,表现为首句响应前的长时间卡顿;WebLLM 选择在加载阶段一次性完成,让 `reload()` 返回后的第一次对话就是全速的。

#### 4.3.2 核心流程

它在 `reloadInternal` 中的位置([src/engine.ts:399-415](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L399-L415)):

```text
new LLMChatPipeline(tvm, tokenizer, config, logitProcessor)   ← 纯"接线",不执行内核
        │
await newPipeline.asyncLoadWebGPUPipelines()                  ← 编译/加载全部 WebGPU 内核
        │
this.loadedModelIdToPipeline.set(modelId, newPipeline)        ← 此刻才算"就绪"并注册
this.loadedModelIdToLock.set(modelId, new CustomLock())
        │
initProgressCallback({ progress: 1, ... "Finish loading on " + gpuLabel })
```

三个设计点:

- **放在构造之后**:构造只取函数句柄,不触发内核执行,两者天然分离;
- **放在注册进 Map 之前**:保证任何拿到该管线的请求都不会碰到未编译的内核——「就绪」的定义包含 shader 就绪;
- **放在 `reload()` 返回之前**:这就是 u1-l2 结论「`CreateMLCEngine` 返回时 shader 已编译完毕」的出处。

#### 4.3.3 源码精读

管线侧实现只有一行转发,位于 [src/llm_chat.ts:715-717](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/llm_chat.ts#L715-L717):

```typescript
async asyncLoadWebGPUPipelines() {
  await this.tvm.asyncLoadWebGPUPipelines(this.vm.getInternalModule());
}
```

`this.vm.getInternalModule()` 取出 VM 持有的内部模块(即 wasm 加载进来的模型模块),交给 tvmjs 去编译其中全部内核;具体编译与缓存策略在 `@mlc-ai/web-runtime` 包内实现(不在本仓库,标注:**待确认**——其内部是否以及如何利用浏览器自身的 pipeline 缓存,取决于 web-runtime 版本与浏览器实现)。

引擎侧的调用点在 [src/engine.ts:399-429](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L399-L429):管线构造与预热完成后,才把管线写入 `loadedModelIdToPipeline`、给它配互斥锁,最后发 `progress: 1` 的完成回调;若期间设备丢失(显存爆掉),会在末尾抛 `DeviceLostError`([src/engine.ts:427-429](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L427-L429))。

顺带一提:u2-l5 讲过的 `EmbeddingPipeline` 走同一个入口([src/engine.ts:402-412](https://github.com/mlc-ai/web-llm/blob/90f67096b68d3b77509c938f2221e4cef03b7d76/src/engine.ts#L402-L412) 按 `model_type` 分流),也同样调用 `asyncLoadWebGPUPipelines`——预热对所有管线一视同仁。

#### 4.3.4 代码实践

1. **实践目标**:体会 shader 预热在加载耗时中的占比,区分「下载耗时」与「预热耗时」。
2. **操作步骤**:在 4.1.4 页面基础上加计时(**示例代码**):
   ```typescript
   const t0 = performance.now();
   const engine = await webllm.CreateMLCEngine(
     "Llama-3.2-1B-Instruct-q4f32_1-MLC",
     { logLevel: "INFO", initProgressCallback: (r) => console.log(r.text) },
   );
   console.log(`首次加载总耗时: ${((performance.now() - t0) / 1000).toFixed(1)}s`);

   await engine.reload("Llama-3.2-1B-Instruct-q4f32_1-MLC"); // 触发二次加载
   ```
   先跑一次记录总耗时;刷新页面再跑,第二次所有产物命中 CacheStorage,剩下的耗时主要就是 wasm 实例化 + 权重装载 + shader 预热。
3. **需要观察的现象**:首次与二次的耗时差(≈下载时间);二次耗时中,进度日志从很早就停在最后一项直到 "Finish loading" 的那段空窗(≈预热时间)。
4. **预期结果**:二次加载明显快于首次,但仍需数秒(预热不可省);不同 GPU/驱动差异很大。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**:为什么 `asyncLoadWebGPUPipelines` 是 `LLMChatPipeline` 的方法,而不是构造函数内部直接 `await`?

**答案**:构造函数是同步的(签名无 async,内部只做接线);shader 编译是异步重活,单独成方法让引擎层能显式 `await`(engine.ts:413),也保持了「构造 = 拿句柄、预热 = 编译内核」的职责分离。

**练习 2**:如果把 `await newPipeline.asyncLoadWebGPUPipelines()` 这一行删掉,最可能出现什么现象?

**答案**:管线照样构造成功、照样被注册,首次 `chatCompletion` 时才触发内核编译,表现为第一次回复前的异常卡顿;具体是否直接报错取决于 web-runtime 对未预热内核的容错(**待确认**),但「加载完即可全速对话」的体验一定会被破坏。

## 5. 综合实践

做一个「模型加载体检报告」页面,把本讲三个模块串起来:

1. **环境体检**(4.1):页面加载即调用 `engine.getGPUVendor()` 与 `engine.getMaxStorageBufferBindingSize()`,把厂商、上限(MB)、是否低于 1GB 显示出来;低于 1GB 时按 engine.ts:1170-1180 的警告名单提示「适合加载小模型」。
2. **加载解剖**(4.2、4.3):`CreateMLCEngine("Llama-3.2-1B-Instruct-q4f32_1-MLC", { logLevel: "INFO" })`,记录:总耗时;控制台中 `Resolved model ABI`、`Using prefillChunkSize`、`Using contextWindowSize` 三条日志;把 `progress: 1` 回调里的 `timeElapsed` 与自己测的总耗时对照。
3. **二次对照**:`engine.reload` 同一模型,记录缓存命中后的耗时,并在页面上输出结论:「非缓存耗时 ≈ 下载,缓存耗时 ≈ 实例化 + 装载 + shader 预热」。
4. **写结论**:用一段话回答——在你这台设备上,限制可运行模型规模的是 `maxStorageBufferBindingSize`、总显存,还是窗口大小?依据是什么?(提示:结合 4.2.2 的 KV cache 公式与 `vram_required_MB`。)

全部数值类结果标注「待本地验证」;完成后的页面就是后续 u7-l3(延迟分解)实验的直接基底。

## 6. 本讲小结

- tvmjs 实例由 `reloadInternal` 创建:下载 `model_lib` wasm → `tvmjs.instantiate(wasm, createPolyfillWASI())` → `detectGPUDevice()`(含 `shader-f16` 等特性检查)→ `tvm.initWebGPU(device)`;管线内 `tvm.webgpu()` 取回 `DLDevice`。
- `LLMChatPipeline` 构造函数是「接线」而非「计算」:建 VirtualMachine → 调 `_metadata` 读模型自述 → `loadVMFunctionRegistry` 装候选函数表 → `resolveModelABI` 按 `kv_state_kind` 与函数可用性决定内核形态与状态对象 → 绑定 prefill/decoding/采样族 → `getParamsFromCacheByName` 装权重 → 按窗口配置创建分页 KV cache。
- 函数句柄分两类:模型 wasm 导出的 VM 函数(`vm.getFunction`)与运行时内建(`tvm.getGlobalFunc("vm.builtin....")`);`this.prefill` 字段背后可能是 `batch_prefill`。
- KV cache 按 `maxTotalSeqLen`(context 或 sliding window)构造期一次分配满,显存约 \( 2 n_{\text{layer}} n_{\text{kv head}} d_{\text{head}} L_{\max} b \),与 `maxStorageBufferBindingSize` 及总显存互相制约。
- `asyncLoadWebGPUPipelines` 在管线构造之后、注册进 Map 之前异步编译全部 WebGPU 内核;`reload()` 返回即代表「句柄 + 权重 + shader」三位一体全部就绪。
- `getMaxStorageBufferBindingSize()`/`getGPUVendor()` 独立探测设备、不依赖已加载模型,可用于加载前的选型体检。

## 7. 下一步学习建议

管线已就绪,下一讲 **u3-l2(Conversation 对话模板与提示词编码)** 讲「prompt 在进入 prefill 之前长什么样」:`Conversation` 类如何用 `conv_template` 把多轮 messages 拼成 token 序列。之后 u3-l3/u3-l4 依次精读 `prefillStep` 与 `decodeStep`,你会在这两讲里反复遇到本讲绑定的 `this.prefill`/`this.decoding` 的真实调用。想延伸阅读运行时本身的读者,可以去看 npm 依赖 `@mlc-ai/web-runtime` 对应的开源仓库(tvmjs 的 WebGPU 后端实现),本讲所有「待确认」的内部细节都在那里。
