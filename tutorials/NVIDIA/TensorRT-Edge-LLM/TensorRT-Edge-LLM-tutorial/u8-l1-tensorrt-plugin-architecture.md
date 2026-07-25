# TensorRT 插件架构

## 1. 本讲目标

本讲打开三段式流水线里一直被当作「黑盒」的一层——**自定义算子的真正落地点：TensorRT 插件**。学完后你应该能够：

1. 说清楚 EdgeLLM 为什么需要自定义插件，以及 Python 端的自定义 ONNX 算子（见 u2-l5）是如何对应到 C++ 插件的。
2. 解释 `NvInfer_edgellm_plugin` 为什么必须是 **SHARED（共享库）** 而不是静态库，并描述它被宿主进程 `dlopen` 加载、注册、运行的完整链路。
3. 掌握 `IPluginV3` 接口契约：一个插件类要实现哪些方法、`Creator` 工厂起什么作用、`REGISTER_TENSORRT_PLUGIN` 宏做了什么。
4. 了解 `AttentionPlugin` / `Int4GroupwiseGemmPlugin` / `Nvfp4MoePlugin` 等典型插件的职责边界。

## 2. 前置知识

在进入本讲前，请确认你已建立以下认知（来自 u1/u2/u4 单元）：

- **三段式流水线**（u1-l2）：检查点 → Python 导出 ONNX → C++ 引擎构建 engine → C++ 运行时推理。
- **ONNX 自定义算子**（u2-l5）：Python 导出端用 `trt` / `trt_edgellm` 两个自定义算子域登记了约 20 个融合算子（attention / Mamba / MoE / 量化 GEMM），并通过 `dynamo_translations.py` 把 FX 算子翻译成 ONNX 节点。这些节点在标准 TensorRT 里**不存在**，必须由插件来「认领」。
- **构建器八阶段**（u4-l1）：`LLMBuilder::build()` 的**第一阶段就是「插件加载」**，第二阶段才解析配置——也就是说，没有插件库被加载，后面解析 ONNX 时遇到自定义节点就会直接失败。
- **RAII 张量**（u5-l6）：插件内部用 `rt::Tensor`（`cpp/common/tensor.h`）封装 GPU/CPU 线性布局张量。

如果你对上面任何一项还不熟悉，建议先回去看对应讲义，本讲会直接使用这些术语。

**直觉先行：什么是插件？** TensorRT 自带一组标准算子（卷积、MatMul、softmax…），但 LLM 推理里有大量「融合算子」（比如把 RoPE + QKᵀ + softmax + KV cache 读写 + attention 输出合成一个核），标准 TensorRT 既不认识、也无法高效实现。插件就是用户自定义的算子实现：你写一个 C++ 类实现 `IPluginV3` 接口，编进一个共享库，TensorRT 在构建期把图里的自定义节点交给你的插件去「形状推导 + 选数据格式」，在运行期把实际 GPU 计算交给你插件里的 `enqueue`。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `cpp/CMakeLists.txt` | 定义插件共享库 `NvInfer_edgellm_plugin` 这个构建目标，是「为什么是 SHARED」的权威出处 |
| `cpp/plugins/attentionPlugin/attentionPlugin.h` | `AttentionPlugin` 与 `AttentionPluginCreator` 的类声明，列全 `IPluginV3` 要实现的虚方法 |
| `cpp/plugins/attentionPlugin/attentionPlugin.cpp` | 插件实现：注册宏、能力分发、形状/格式推导、`enqueue`、Creator 工厂 |
| `cpp/plugins/utils/pluginUtils.{h,cpp}` | 插件库的 C-ABI 入口 `initEdgellmPlugins`、可见性宏、字段解析工具 |
| `cpp/common/trtUtils.h` | `loadEdgellmPluginLib()`：宿主进程 `dlopen` 插件库的代码 |
| `cpp/plugins/nvfp4MoePlugin/README.md` | NVFP4 MoE 插件的职责、ONNX 输入面、SM 适配说明 |
| `cpp/plugins/int4GroupwiseGemmPlugin/int4GroupwiseGemmPlugin.{h,cpp}` | INT4 仅权重组量化 GEMM 插件，结构最简单，适合作为「最小插件」范本 |
| `docs/source/developer_guide/customization/tensorrt-plugins.md` | 官方插件指南，是本讲代码实践任务的依据 |

## 4. 核心概念与源码讲解

本讲拆为四个最小模块：**CMake 插件目标（为什么 SHARED）**、**插件注册机制**、**动态加载与 IPluginV3 契约**、**其他典型插件职责**。前三模块都以 `AttentionPlugin` 为载体，第四模块扩展到 INT4 GEMM 与 NVFP4 MoE。

---

### 4.1 CMake 插件目标：为什么是 SHARED 库

#### 4.1.1 概念说明

回顾 u1-l3：`cpp/CMakeLists.txt` 产出三个**静态库**（`edgellmKernels` / `edgellmCore` / `edgellmBuilder`）加一个**共享库** `NvInfer_edgellm_plugin`。这不是随意选择，而是由 TensorRT 的插件加载模型决定的。

TensorRT 在**运行期**（不是编译期）才去解析引擎里的插件节点——它会从一个全局注册表里按「插件名 + 版本」找到对应的 `IPluginCreator`，让它把序列化字节重建出插件实例。这个全局注册表是进程级的，靠 `REGISTER_TENSORRT_PLUGIN` 宏在**库被加载到进程地址空间时**填充。静态库的符号只有在被链接器「实际引用」时才会进入可执行文件，而 TensorRT 宿主并不知道你有哪些插件、无法引用它们，于是注册代码会被丢弃——注册表为空，引擎反序列化失败。**共享库则整个被 `dlopen` 映射进进程**，其中的静态初始化代码（`REGISTER` 宏背后就是全局对象的构造）必然执行，注册表自然被填满。

一句话总结：**共享库 = 「整块加载、必然初始化」；静态库 = 「按需链接、可能被丢」**。插件必须用前者。

#### 4.1.2 核心流程

插件库的构建流程：

1. `file(GLOB_RECURSE PLUGIN_CPP_SRCS "plugins/*.cpp")` 收集 `cpp/plugins/` 下所有 `.cpp`（每个插件一个文件，全部进同一个库）。
2. `add_library(NvInfer_edgellm_plugin SHARED ...)` 声明为 **SHARED** 共享库。
3. 链接 `edgellmKernels` 等静态库（插件要把算子实现拉进来）。
4. `edgellm_set_hidden_visibility` 把默认符号可见性设为 hidden，**只保留 `EDGELLM_PLUGIN_EXPORT` 标记的 C-ABI 入口可见**——防止多个库一起加载时同名符号冲突。
5. `target_link_options(... "-Wl,--exclude-libs,ALL")` 进一步把从静态档案里拽进来的符号也隐藏掉。

#### 4.1.3 源码精读

先看共享库目标的声明与可见性处理（关键注释说明了设计动机）：

[cpp/CMakeLists.txt:L146-L163](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/CMakeLists.txt#L146-L163) — 把 `plugins/*.cpp` 全部编进**共享库** `NvInfer_edgellm_plugin`，设版本号、输出目录，链接内核静态库与 CUDA 运行时，最后两行 `edgellm_set_hidden_visibility` 与 `-Wl,--exclude-libs,ALL` 把内部符号藏起来，只留导出入口。

隐藏可见性的辅助函数定义在文件顶部：

[cpp/CMakeLists.txt:L28-L31](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/CMakeLists.txt#L28-L31) — `edgellm_set_hidden_visibility` 给目标设 `CXX_VISIBILITY_PRESET hidden` 与 `VISIBILITY_INLINES_HIDDEN ON`，注释写明「only EDGELLM_PLUGIN_EXPORT entry points stay visible」，正是为了「防止多库一起加载时的重复符号碰撞」。

`AGENTS.md` 也把这条设计原则列为开发者须知，可见其重要性：

[AGENTS.md:L98](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/AGENTS.md#L98) — 「`NvInfer_edgellm_plugin` is shared (not static) because TRT loads plugins dynamically.」

#### 4.1.4 代码实践

**实践目标**：亲手确认插件库的产物类型与符号可见性策略。

**操作步骤**：

1. 在 `cpp/CMakeLists.txt` 里定位 `add_library(NvInfer_edgellm_plugin SHARED ...)` 这一行，确认关键字是 `SHARED`。
2. 在同文件找到 `edgellm_set_hidden_visibility(NvInfer_edgellm_plugin)`，对照其定义（L28-L31）理解它把默认可见性设成了什么。
3. 假设你把它改成 `STATIC` 重新构建一个最小示例程序：程序 `dlopen` 这个 `.a`（实际上静态库无法直接 `dlopen`，这一步会失败）——记录失败现象，这正是「静态库不能当插件库」的最直接证据。

**需要观察的现象 / 预期结果**：SHARED 版本构建后会生成 `build/libNvInfer_edgellm_plugin.so`（文件名正是 `loadEdgellmPluginLib` 的默认搜索路径，见 4.3.3）；若强行改成 STATIC，构建产物不再是 `.so`，下游 `dlopen` 必然找不到库。其余运行结果**待本地验证**（需要完整 TensorRT + CUDA 环境）。

#### 4.1.5 小练习与答案

**练习 1**：如果同时构建了 `edgellmBuilder`（静态库）和 `NvInfer_edgellm_plugin`（共享库），为什么后者要再叠一层 `-Wl,--exclude-libs,ALL`？
**答案**：插件库链接了 `edgellmKernels` 等静态档案，这些档案里的符号默认会被「提升」进共享库的导出表；`--exclude-libs,ALL` 把它们重新藏起来，只留显式 `EDGELLM_PLUGIN_EXPORT` 的入口，避免与别的库里的同名内核符号冲突。

**练习 2**：`REGISTER_TENSORRT_PLUGIN` 宏填充的「全局注册表」在静态库里为什么会失效？
**答案**：宏背后是全局对象的构造，靠静态初始化执行。静态库只有被链接器「实际引用」的符号才进可执行文件；TensorRT 宿主不直接引用这些插件对象，于是它们被丢弃、构造不执行、注册表为空。共享库则整块 `dlopen` 映射，构造必然执行。

---

### 4.2 插件注册机制：REGISTER_TENSORRT_PLUGIN 与 Creator

#### 4.2.1 概念说明

每个插件实际上是**一对类**：

- **插件本体**（如 `AttentionPlugin`）：实现计算，继承 `IPluginV3` 及其「能力子接口」。
- **Creator**（如 `AttentionPluginCreator`）：实现 `IPluginCreatorV3One`，是**工厂**。它负责两件事——声明本插件接受哪些**配置字段**（`PluginField`），以及 `createPlugin()` 用这些字段造出一个插件实例。

为什么要把「工厂」和「本体」分开？因为 TensorRT 需要在**没有插件实例**的情况下就能查询「有哪些插件、每个插件要什么参数」。Creator 是「类级别的元信息」，本体是「实例」。Creator 通常是个无状态单例，由 `REGISTER_TENSORRT_PLUGIN` 宏注册到全局 `getPluginRegistry()`。

#### 4.2.2 核心流程

注册与创建的链路：

1. 进程 `dlopen` 插件库 → 各 `.cpp` 文件里的 `REGISTER_TENSORRT_PLUGIN(XxxCreator)` 宏展开成全局对象，构造时把 Creator 指针塞进全局注册表。
2. 构建期，TensorRT 解析 ONNX，遇到一个自定义节点（名字如 `AttentionPlugin`），到注册表里按「名字 + 版本」查 Creator。
3. 调 Creator 的 `getFieldNames()` 拿到该插件期望的字段清单，用 ONNX 节点属性填出一个 `PluginFieldCollection`。
4. 调 Creator 的 `createPlugin(name, fc, phase)` → 内部 `new AttentionPlugin(name, fc)` → 字段被解析为成员变量。
5. 得到的插件实例交给 TensorRT 做形状/格式推导与后续编译。

#### 4.2.3 源码精读

插件名与版本是两个常量，二者共同构成注册表里的「主键」（同名不同版算不同插件）：

[cpp/plugins/attentionPlugin/attentionPlugin.cpp:L57-L58](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.cpp#L57-L58) — `kATTENTION_PLUGIN_NAME{"AttentionPlugin"}`、`kATTENTION_PLUGIN_VERSION{"1"}`，注册表与 ONNX 节点都靠它俩对齐。

注册宏出现在每个插件文件的末尾，是「这个插件被纳入库」的标志：

[cpp/plugins/attentionPlugin/attentionPlugin.cpp:L252](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.cpp#L252) — `REGISTER_TENSORRT_PLUGIN(AttentionPluginCreator);` 把 Creator 注册进全局表。

Creator 构造函数登记本插件接受的全部配置字段（这就是 Python 导出端在 ONNX 节点里写入的属性的「接收端」）：

[cpp/plugins/attentionPlugin/attentionPlugin.cpp:L1592-L1613](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.cpp#L1592-L1613) — 用 `mPluginAttributes.emplace_back(PluginField(...))` 依次声明 `num_q_heads`、`num_kv_heads`、`head_size`、`attention_scale`、`enable_tree_attention`、`enable_fp8_kv_cache`、`enable_vision_block_attention`、`sliding_window_size`、`qkv_scales` 九个字段及其类型；注意可选字段第 4 参数传 `0`（表示 length=0 的可选属性）。这里还用 `static std::mutex` 保护，因为 Creator 可能被多线程并发构造。

`createPlugin` 就是把字段集合交给「字段构造函数」：

[cpp/plugins/attentionPlugin/attentionPlugin.cpp:L1640-L1654](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.cpp#L1640-L1654) — `createPlugin` 调用 `new AttentionPlugin(std::string(name), fc)`，即头文件里那个「从 `PluginFieldCollection` 构造」的第二个构造函数，并设上命名空间；用 try/catch 把异常转成 `nullptr` 返回，因为 `noexcept` 接口里抛异常会终止进程。

#### 4.2.4 代码实践

**实践目标**：盘点当前库里到底注册了哪些插件，理解「一个文件 = 一个插件 = 一对类」。

**操作步骤**：

1. 用搜索工具在 `cpp/plugins/` 下查找所有 `REGISTER_TENSORRT_PLUGIN(` 调用（本讲作者已为你列出结果，你也可以自己复现）。
2. 对每条命中，找到对应的 `...PluginCreator` 类与其 `createPlugin`，确认它 `new` 的是哪个本体类。
3. 挑一个最简单的插件（推荐 `int4GroupwiseGemmPlugin`），看它的 Creator 登记了哪些字段，与 `tensorrt-plugins.md` 文档里的「Configuration Parameters」对照。

**预期结果**：你会得到约 14 条 `REGISTER_TENSORRT_PLUGIN` 命中，覆盖 `AttentionPluginCreator`、`Int4GroupwiseGemmPluginCreator`、`Nvfp4MoePluginCreator`、`NvFP4MoEPluginGeforceCreator`、`Fp16MoePluginCreator`、`Int4MoePluginCreator`、`MambaPluginCreator`、`CausalConv1dPluginCreator`、`GatedDeltaNetPluginCreator`、`ViTAttentionPluginCreator`、`Gemma4AudioAttentionPluginCreator`、`DFlashTargetKVCacheUpdatePluginCreator`、`AllReducePluginCreator`、`FusedNvfp4GemmAllReducePluginCreator`。可见插件覆盖了 attention / MoE / 量化 GEMM / Mamba 类 SSM / 多模态 ViT / 通信 all-reduce 等全部「标准 TRT 无法高效实现」的算子族。

#### 4.2.5 小练习与答案

**练习**：`AttentionPluginCreator` 登记了 `qkv_scales` 字段但 length 标为 `0`（可选），而 `num_q_heads` 标为 `1`（必填）。请解释这个 length 参数的含义，并说明它在「FP8 KV 缓存关闭」时为什么可以缺省。
**答案**：`PluginField` 的第 4 参数是 `length`——标量必填字段传 `1`（正好一个元素），可选/变长字段传 `0` 表示「可不提供」。`num_q_heads` 是模型结构常量，必须给；`qkv_scales` 仅在 `enable_fp8_kv_cache=1` 时需要，关闭 FP8 KV 缓存时插件用默认 `{1,1,1}`（见头文件 `mQkvScales` 初值），因此可缺省。

---

### 4.3 动态加载与 IPluginV3 契约

#### 4.3.1 概念说明

光注册还不够，TensorRT 宿主还得**主动加载**插件库。EdgeLLM 的做法是：构建器/运行时在启动时调用 `loadEdgellmPluginLib()`，它 `dlopen` 那个 `.so`、`dlsym` 出一个 C 函数 `initEdgellmPlugins` 来同步日志级别。这一步对应构建器八阶段（u4-l1）的**第一阶段「插件加载」**。

插件本体则要实现 `IPluginV3`。与旧的 `IPluginV2DynamicExt` 相比，V3 把能力拆成几个「One」子接口（Core / Build / Runtime），插件类用**多继承**同时实现它们，并通过 `getCapabilityInterface` 把自己按需 `static_cast` 成对应子接口交还给 TensorRT。官方文档（`tensorrt-plugins.md` L11）明确写着「TensorRT Edge-LLM is migrating all plugins to V3」。

#### 4.3.2 核心流程

一次插件节点从加载到执行的完整时序：

```
dlopen(libNvInfer_edgellm_plugin.so)   ← 进程级，RTLD_NODELETE
        │  触发所有 REGISTER 宏的全局对象构造 → 填充 getPluginRegistry()
        ▼
dlsym("initEdgellmPlugins") → 同步 logger 级别
        │
        ▼
[构建期] TRT 解析 ONNX 节点 "AttentionPlugin"
        │  registry->getPluginCreator(name, version) → AttentionPluginCreator
        ▼
creator->createPlugin(name, fc, phase) → new AttentionPlugin(name, fc)
        │
        ▼  TensorRT 询问能力子接口：
getCapabilityInterface(kBUILD)   → IPluginV3OneBuildV2*  做形状/格式推导
getOutputShapes / supportsFormatCombination / getWorkspaceSize ...
        │
        ▼  [运行期] 每个 batch：
getCapabilityInterface(kRUNTIME) → IPluginV3OneRuntime*
enqueue(...)  → enqueueImpl → dispatch 到 FMHA / XQA / CuTe DSL 内核
```

#### 4.3.3 源码精读

宿主加载插件库的代码（注意两个关键标志）：

[cpp/common/trtUtils.h:L55-L92](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/trtUtils.h#L55-L92) — `loadEdgellmPluginLib()`：从环境变量 `EDGELLM_PLUGIN_PATH` 取路径（缺省 `build/libNvInfer_edgellm_plugin.so`），用 `dlopen(..., RTLD_LAZY | RTLD_NODELETE)` 打开；注释解释 `RTLD_NODELETE` 的原因——「TensorRT 引擎在 `dlclose` 之后仍会使用插件代码，故库必须在整个进程生命周期内保持映射，避免拆除时崩溃」。随后 `dlsym("initEdgellmPlugins")` 同步日志级别。

加载入口的 C-ABI 函数与导出标记（解释了 4.1 里「只留一个入口可见」具体留的是谁）：

[cpp/plugins/utils/pluginUtils.h:L30-L37](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/utils/pluginUtils.h#L30-L37) — `EDGELLM_PLUGIN_EXPORT` 宏 = `__attribute__((visibility("default")))`，注释说明「libraries are built with -fvisibility=hidden, so symbols dlsym'd by the host must stay visible」；`initEdgellmPlugins(void*, char const*)` 就是那个唯一被 `dlsym` 的导出函数，遵循 TRT `initLibNvInferPlugins` 的约定。

[cpp/plugins/utils/pluginUtils.cpp:L26-L37](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/utils/pluginUtils.cpp#L26-L37) — `initEdgellmPlugins` 把宿主传入的 `ILogger*` 动转成 `EdgeLLMLogger*`，若成功就把全局 `gLogger` 的级别同步成应用级别——这样插件的 `LOG_DEBUG` 才会尊重应用的日志阈值。

构建器在第一阶段调用它（即 u4-l1 八阶段的「插件加载」）：

[cpp/builder/llmBuilder.cpp:L180](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L180) — `LLMBuilder::build()` 开头的 `auto pluginHandles = loadEdgellmPluginLib();`，返回的 `unique_ptr` 在整个 `build()` 期间持有库句柄不释放。`visualBuilder.cpp`、`audioBuilder.cpp`、`actionBuilder.cpp` 也各自调用同一函数。

接下来看 `IPluginV3` 契约本身。类用四重继承把能力拼起来：

[cpp/plugins/attentionPlugin/attentionPlugin.h:L39-L42](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.h#L39-L42) — `class AttentionPlugin : public IPluginV3, public IPluginV3OneCore, public IPluginV3OneBuildV2, public IPluginV3OneRuntime`。Core（名字/版本/命名空间）、Build（形状/格式/工作区）、Runtime（enqueue/attachToContext）三组能力各是一个子接口。注意是较新的 `IPluginV3OneBuildV2`，而 `Int4GroupwiseGemmPlugin` 还用 `IPluginV3OneBuild`——这是 V3 迁移进行中的痕迹。

`getCapabilityInterface` 是 V3 的「能力路由器」，按 TensorRT 询问的阶段返回对应子接口指针：

[cpp/plugins/attentionPlugin/attentionPlugin.cpp:L526-L544](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.cpp#L526-L544) — 询问 `kBUILD` 返回 `static_cast<IPluginV3OneBuildV2*>(this)`，`kRUNTIME` 返回 `IPluginV3OneRuntime*`，否则返回 `IPluginV3OneCore*`；用 try/catch 包裹以满足 `noexcept`。

`clone()` 在 TensorRT 内部复制图时被调用，必须能从自身参数再造一个等价实例：

[cpp/plugins/attentionPlugin/attentionPlugin.cpp:L546-L559](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.cpp#L546-L559) — `clone()` 用「全参数构造函数」`new` 一个新插件并复制命名空间。

形状推导（构建期）告诉 TensorRT 输出张量的形状：

[cpp/plugins/attentionPlugin/attentionPlugin.cpp:L613-L641](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.cpp#L613-L641) — `getOutputShapes`：输出 0（attention 结果）形状 `[B,S,Hq,D]` 由输入 Q 的前两维 + `mNumQHeads`/`mHeadSize` 常量拼接；输出 1（KV cache）形状与输入 KV cache 完全一致 `[B,2,Hkv,Smax,D]`。

格式校验（构建期）逐个 `pos` 检查每个输入输出的 dtype/format/维数是否被支持：

[cpp/plugins/attentionPlugin/attentionPlugin.cpp:L643-L658](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.cpp#L643-L658) — `supportsFormatCombination` 注释列全了 7 个必需输入 + 2 个可选输入 + 2 个输出的约定（Q/K/V 为 `[B,S,H*D]` FP16 线性布局，KV cache 为 `[B,2,Hkv,Smax,D]` FP16 或 FP8…）。它通过一组 `checkXxx` lambda 逐位置返回布尔。

工作区大小（构建期）按最坏路径预估显存：

[cpp/plugins/attentionPlugin/attentionPlugin.cpp:L847-L859](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.cpp#L847-L859) — `getWorkspaceSize` 从 Q 输入的 `max` 形状取 `maxBatchSize`/`maxSeqLen`、从 KV cache 取 `maxKVCacheCapacity`，交给 `getAttentionWorkspaceSize` 累加（见 L190-L244 的 workspace 布局表，含 cuSeqLens、转置 KV、FP8 Q、vision mask 等槽位）。

`getAliasedInput` 揭示了一个有趣的实现细节——KV cache 是**原地读写**的：

[cpp/plugins/attentionPlugin/attentionPlugin.cpp:L861-L871](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.cpp#L861-L871) — 注释坦白「正确做法应返回 `kIN_KV_CACHE_IDX` 声明 output 别名 input」，但因为声明别名会让 Myelin（TRT 优化器）多保留一次逐层 KV 拷贝（性能回退），所以**故意返回 -1 不声明别名**；原地读写仍能工作，是因为运行时把 past/present KV 绑定到同一地址（回顾 u5-l3「past/present 同址绑定」）。这是一个「为性能偏离标准用法」的真实工程取舍。

运行期入口 `enqueue` 把 TRT 的裸指针包装成 `rt::Tensor` 再分发到具体内核：

[cpp/plugins/attentionPlugin/attentionPlugin.cpp:L899-L960](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.cpp#L899-L960) — `enqueueImpl` 从 `inputDesc`/`outputDesc` 取形状、从 `inputs`/`outputs` 取设备指针，构造非拥有的 `rt::Tensor`（回顾 u5-l6 的 non-owned 张量）；根据 `kvCacheStartIdx` 长度与 seqLen 推断执行模式（`kNORMAL_PREFILL` / `kCHUNKED_PREFILL` / `kVANILLA_DECODING` / `kTREE_DECODING`），再分发到 FMHA（prefill）或 XQA（decode）内核。`enqueue` 本身用 try/catch 包裹 `enqueueImpl`，把异常转成错误码——因为 `enqueue` 是 `noexcept`，直接抛会终止进程。

序列化字段（构建期产物落盘时被调用）：

[cpp/plugins/attentionPlugin/attentionPlugin.cpp:L1569-L1581](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.cpp#L1569-L1581) — `getFieldsToSerialize` 把九个成员变量重新打包成 `PluginField`，供 TRT 写进 engine；运行期 `createPlugin` 再读回来，重建出等价实例。注意它与 Creator 构造函数（L1592）登记的字段是一一对应的——这是「字段契约」闭环。

#### 4.3.4 代码实践

**实践目标**：跟踪一次「库被加载」的真实调用，并理解环境变量如何覆盖默认路径。

**操作步骤**：

1. 在 `cpp/` 下搜索 `loadEdgellmPluginLib` 的全部调用点，确认 `llmBuilder` / `visualBuilder` / `audioBuilder` / `actionBuilder` 四个构建器都在 `build()` 开头加载插件库。
2. 读 `loadEdgellmPluginLib`（trtUtils.h L60-L92），找到默认路径 `build/libNvInfer_edgellm_plugin.so` 与覆盖它的环境变量 `EDGELLM_PLUGIN_PATH`。
3. 假设你把插件库安装到了非默认目录，写出一行 `export` 命令让构建器能找到它。

**预期结果**：四条调用点都已列出（见 4.3.3）。覆盖命令为：

```bash
export EDGELLM_PLUGIN_PATH=/opt/edgellm/lib/libNvInfer_edgellm_plugin.so
```

设置后构建器日志会打印 `EDGELLM_PLUGIN_PATH: /opt/edgellm/lib/...`（见 trtUtils.h L66）。**待本地验证**：实际运行需 GPU + TensorRT。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `dlopen` 要带 `RTLD_NODELETE`？去掉它会在什么时刻出问题？
**答案**：TensorRT 引擎对象的生命周期可能比「库句柄的 `unique_ptr`」长——一旦句柄析构触发 `dlclose`，引擎里仍指向插件代码的函数指针就悬空了，下次执行 `enqueue` 会崩溃。`RTLD_NODELETE` 让库在整个进程生命周期内保持映射，`dlclose` 不真正卸载，规避 use-after-free。

**练习 2**：`enqueue` 是 `noexcept` 的，但插件内部可能抛异常（如 `ELLM_CHECK` 失败）。设计上如何调和？
**答案**：提供一个非 `noexcept` 的 `enqueueImpl` 真正干活，`enqueue` 用 try/catch 包住它，捕获到异常时返回非零错误码（而不是让异常逃逸）。这样既保留了清晰的错误信息（`LOG_ERROR`），又满足 `noexcept` 契约不致进程终止。

---

### 4.4 其他典型插件职责：int4GroupwiseGemm 与 nvfp4Moe

#### 4.4.1 概念说明

除了 attention，本仓库还有两类典型插件值得认识：

- **INT4 仅权重 GEMM**：对应量化里的 `int4_awq` / `int4_gptq`（u3）。把权重压成 4-bit、激活仍 FP16（W4A16），靠专用 GEMM/GEMV 内核在 Orin 等边缘平台上省显存。
- **NVFP4 MoE**：对应混合专家层的 NVFP4（E2M1）4-bit 浮点权重（u3-l3）。MoE 层路由 + 多专家 GEMM 是显存与算力双重热点，需要专用插件。

它们的结构与 attention 完全一致（一对类 + REGISTER 宏 + IPluginV3），只是 `enqueue` 里的内核不同。

#### 4.4.2 核心流程（插件与流水线的衔接）

回顾 `tensorrt-plugins.md` 的「Integration Workflow」，插件贯穿三段式流水线：

1. **量化阶段**（u3）：`tensorrt-edgellm-quantize` 把权重存成 INT4 组量化或 NVFP4 格式。
2. **导出阶段**（u2-l5）：`tensorrt_edgellm` 在 ONNX 图里发出自定义节点（如 `Int4GroupwiseGemmPlugin`、`Nvfp4MoePlugin`），节点属性 = Creator 接受的字段。
3. **构建阶段**（u4）：构建器 `dlopen` 插件库（4.3），TRT 用注册表里的 Creator 把节点变成插件实例，做形状/格式推导，编进 engine。
4. **运行阶段**（u5）：每个 batch 调 `enqueue`，插件把指针交给底层 CUDA/CuTe DSL 内核执行。

#### 4.4.3 源码精读

INT4 GEMM 插件结构最简单，是理解「最小插件」的好范本：

[cpp/plugins/int4GroupwiseGemmPlugin/int4GroupwiseGemmPlugin.h:L34-L37](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/int4GroupwiseGemmPlugin/int4GroupwiseGemmPlugin.h#L34-L37) — `class Int4GroupwiseGemmPlugin : public IPluginV3, public IPluginV3OneCore, public IPluginV3OneBuild, public IPluginV3OneRuntime`。注意它用 `IPluginV3OneBuild`（非 V2），是 V3 迁移尚未统一的证据。

[cpp/plugins/int4GroupwiseGemmPlugin/int4GroupwiseGemmPlugin.cpp:L38-L47](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/int4GroupwiseGemmPlugin/int4GroupwiseGemmPlugin.cpp#L38-L47) — 名字 `Int4GroupwiseGemmPlugin`、版本 `1`，以及 `REGISTER_TENSORRT_PLUGIN(Int4GroupwiseGemmPluginCreator)`。结构与 attention 完全同构：常量 → 静态字段 → REGISTER 宏。

它的语义在文档里有明确说明（W4A16、组大小 128、对称量化、FP16 累加）：

[docs/source/developer_guide/customization/tensorrt-plugins.md:L73-L94](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/developer_guide/customization/tensorrt-plugins.md#L73-L94) — `Int4GroupwiseGemmPlugin` 实现 `A[M,K]×B[K,N]` 语义，B 为 INT4 仅权重组量化（group size 128），对称量化不支持零点，FP16 累加；输入为激活、swizzled INT4 权重（pack 进 INT8）、组缩放因子，输出为 GEMM 结果。

NVFP4 MoE 插件更复杂——它甚至按 GPU 架构分裂成两个插件：

[cpp/plugins/nvfp4MoePlugin/README.md:L1-L24](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/nvfp4MoePlugin/README.md#L1-L24) — `Nvfp4MoePlugin` 面向 **SM100/101/110**（数据中心 Blackwell / Thor），走「分解的 split FC1/FC2」内核；它的兄弟 `NvFP4MoEPluginGeforce` 面向 **SM120/121**（消费级 Blackwell / Spark GB10），走「融合 CuTeDSL」内核。两者 **ONNX 输入面完全相同（11 入 1 出）**，只是 FC1 权重的磁盘打包布局不同（64 行 up/gate interleave vs. plain concat），由 Python 导出端按目标架构选择插件与匹配的 repack。

它的支持形状与对齐契约（`configurePlugin` 会强制校验）：

[cpp/plugins/nvfp4MoePlugin/README.md:L26-L48](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/nvfp4MoePlugin/README.md#L26-L48) — 限定 `E∈{128,256}`、`top_k≤8`、`H%128==0`、`I%64==0`、`FC1_N%128==0`；FP16 dtype；softmax top-k（Qwen3）或 sigmoid grouped top-k（Nemotron-H）两种路由。这些约束在 `configurePlugin` 里强制，违反时报清晰错误。

11 入 1 出的 ONNX 接口面（这是 Python 导出端 `dynamo_translations.py` 必须产出的节点形状）：

[cpp/plugins/nvfp4MoePlugin/README.md:L58-L80](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/nvfp4MoePlugin/README.md#L58-L80) — 输入含 `router_logits`、`hidden_states`、`fc1_qweights`（int8，64 行 interleave）、逐块 scale、`fc1_alpha`、`fc2_*`、`input_global_scale`、`down_input_scale`、`e_score_correction_bias`，输出为 `[B,S,H]` FP16。块 scale 用 CuTeDSL NVFP4 连续物理布局 `[E,m_tiles,k_tiles,32,4,4]`。

#### 4.4.4 代码实践

**实践目标**：把「Python 自定义算子 → ONNX 节点 → C++ 插件」这条契约链走一遍。

**操作步骤**：

1. 在 `tensorrt_edgellm/onnx/onnx_custom_schemas.py`（u2-l5）里找到 `Int4GroupwiseGemmPlugin` 或 `Nvfp4MoePlugin` 的 schema 定义，记下它的算子名与属性。
2. 对照本讲 4.4.3 的 Creator 字段表与 README ONNX 接口面，确认 Python 端登记的属性 ⊆ C++ Creator 接受的字段。
3. 写一段说明：当目标架构是 SM110（Thor）时，导出端会选 `Nvfp4MoePlugin` 还是 `NvFP4MoEPluginGeforce`？为什么权重 repack 也要随之改变？

**预期结果**：SM110 选 `Nvfp4MoePlugin`（split FC1/FC2 路径），FC1 按 64 行 up/gate interleave 打包；SM120/121 才选 Geforce 版，FC1 按 plain concat 打包。这正说明「插件不仅是算子实现，还编码了架构特定的权重布局契约」——这也是 u3-l3 讲的 repacking 与本讲的插件是同一个契约的两端。

#### 4.4.5 小练习与答案

**练习 1**：为什么 NVFP4 MoE 要按架构拆成两个插件，而不是一个插件内部 `if (sm == 110)` 分支？
**答案**：两者消费的**磁盘权重布局不同**（interleave vs concat），而权重布局在 Python 导出/repack 阶段就定死了、写进了检查点与 ONNX。若用一个插件内部分支，导出端就无法知道该按哪种布局 repack；拆成两个插件、由导出端按架构选择，把「布局选择」前移到导出期，运行期插件只认自己那种布局，逻辑更清晰、错误更早暴露。

**练习 2**：`Int4GroupwiseGemmPlugin` 的 group size 只支持 128（见文档 L85），这与 u3 量化的哪个格式对应？
**答案**：对应 `int4_awq` / `int4_gptq`（组量化、group size 128）。量化阶段把权重按 128 个 INT4 元素共享一个缩放因子存盘，导出时 repack 成插件期望的 swizzled INT8 布局，运行期由本插件做 W4A16 GEMM。

---

## 5. 综合实践

**任务**：为「新增一个自定义插件」写出完整的落地清单，并解释每一步对应的本讲知识点。

假设你要新增一个 `Fp8GroupwiseGemmPlugin`（假设性的 FP8 仅权重 GEMM）。请按下列步骤产出一份设计草案（纸面练习，不要求能编译）：

1. **新建文件**：在 `cpp/plugins/fp8GroupwiseGemmPlugin/` 下新建 `fp8GroupwiseGemmPlugin.{h,cpp}`，仿照 `int4GroupwiseGemmPlugin` 的结构：定义 `class Fp8GroupwiseGemmPlugin : public IPluginV3, IPluginV3OneCore, IPluginV3OneBuild(V2), IPluginV3OneRuntime` 与对应 Creator。
2. **实现必要方法**：至少实现 `getPluginName/Version`、`getCapabilityInterface`、`clone`、`getNbOutputs`、`getOutputShapes`、`supportsFormatCombination`、`getWorkspaceSize`、`enqueue`（内部分发到你写的 FP8 GEMM 内核），以及 Creator 的 `getFieldNames` / `createPlugin`。
3. **注册**：文件末尾写 `REGISTER_TENSORRT_PLUGIN(Fp8GroupwiseGemmPluginCreator);`。因为 `cpp/CMakeLists.txt` 用 `GLOB_RECURSE plugins/*.cpp`，**无需改 CMake** 即可被自动纳入 `NvInfer_edgellm_plugin` 共享库——验证这一点（回顾 4.1.2）。
4. **Python 契约**：在 `onnx_custom_schemas.py` 登记 schema、在 `dynamo_translations.py` 加翻译规则（回顾 u2-l5），让导出端能发出 `Fp8GroupwiseGemmPlugin` 节点，且节点属性与 Creator 的 `getFieldNames` 一致。
5. **验证路径**：按 `tensorrt-plugins.md` 的 Integration Workflow，走 export → build → inference 三步（回顾 u1-l5）确认插件被正确加载与执行；单元测试可仿照 `unittests/` 下的内核测试。

**关键检查点**（用一句话回答）：

- 为什么这个新插件不用改 CMake？→ 因为 `file(GLOB_RECURSE PLUGIN_CPP_SRCS "plugins/*.cpp")` 自动收集。
- 为什么它必须编进共享库而不是静态库？→ TRT 运行期 `dlopen` 才能触发 `REGISTER` 宏的静态初始化。
- 怎样保证只有必要符号暴露？→ `edgellm_set_hidden_visibility` + `-Wl,--exclude-libs,ALL`，只留 `EDGELLM_PLUGIN_EXPORT` 入口。
- 新插件如何被 TensorRT 找到？→ 名字+版本进 `getPluginRegistry()`，构建期解析 ONNX 时按名查 Creator。

> 本任务为「源码阅读 + 设计型实践」，无需 GPU 即可完成草案；实际编译运行**待本地验证**。

## 6. 本讲小结

- EdgeLLM 把 Python 端的自定义 ONNX 算子（u2-l5）落地为 C++ **TensorRT 插件**，插件库 `NvInfer_edgellm_plugin` 必须是 **SHARED** 共享库——因为 TRT 运行期靠 `dlopen` 触发 `REGISTER_TENSORRT_PLUGIN` 的静态初始化来填充全局注册表，静态库的注册代码会被链接器丢弃。
- 每个插件 = **一对类**：本体（实现 `IPluginV3` + Core/Build/Runtime 子接口）与 Creator（`IPluginCreatorV3One` 工厂，声明字段并 `createPlugin`）。
- 宿主在构建器第一阶段 `loadEdgellmPluginLib()` 用 `dlopen(..., RTLD_NODELETE)` 加载库、`dlsym("initEdgellmPlugins")` 同步日志；路径可用 `EDGELLM_PLUGIN_PATH` 覆盖。
- `IPluginV3` 契约分构建期（`getOutputShapes`/`supportsFormatCombination`/`getWorkspaceSize`/`getFieldsToSerialize`）与运行期（`enqueue`）两组；`getCapabilityInterface` 按阶段路由子接口，`enqueue` 用 try/catch 包 `enqueueImpl` 以满足 `noexcept`。
- 可见性策略（`-fvisibility=hidden` + `--exclude-libs,ALL`）只留 `EDGELLM_PLUGIN_EXPORT` 入口，防多库符号冲突；`getAliasedInput` 故意不声明 KV cache 别名以避开 Myelin 性能回退，是「为性能偏离标准用法」的真实取舍。
- 典型插件覆盖 attention / INT4 GEMM / NVFP4 MoE / Mamba / ViT 等；NVFP4 MoE 按架构拆成 `Nvfp4MoePlugin`（SM100/101/110）与 `NvFP4MoEPluginGeforce`（SM120/121），二者 ONNX 接口面相同但权重磁盘布局不同，由导出端按架构选择。

## 7. 下一步学习建议

- **u8-l2 自定义 CUDA 算子（FMHA/MoE/Mamba）**：本讲的 `enqueue` 只是「分发器」，真正干活的是 `cpp/kernels/` 与 `kernelSrcs/` 下的 CUDA/CuTe DSL 内核（如 `ContextFMHARunner`、`decoderXQARunner`、`cuteDslNvfp4MoeSm110Runner`）。下一讲打开这些内核的黑盒。
- **回顾 u2-l5 与 u3-l3**：把「Python schema → dynamo 翻译 → ONNX 节点 → C++ 插件字段」与「量化 repack 布局 → 插件期望布局」两端对照阅读，能彻底打通权重的全生命周期。
- **阅读 `tensorrt-plugins.md` 与 `customization-guide.md`**：前者是本讲代码实践的依据，后者给出「接入新模型/新算子」的扩展边界，是 u9-l4「接入新模型架构」的前置。
- **想动手**：仿照 4.4 的 INT4 GEMM 范本，从 `REGISTER_TENSORRT_PLUGIN` 出发通读一个最简单插件的完整 `.cpp`，对照本讲的接口表逐方法理解，是掌握 V3 插件最快的路径。
