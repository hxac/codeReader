# TensorRT 插件架构

## 1. 本讲目标

本讲打开三段式流水线（检查点 → Python 导出 ONNX → C++ 构建 engine → C++ 运行时推理）里一直被当作「黑盒」的一层——**自定义算子在 C++ 端的真正落地点：TensorRT 插件**。

在 u2-l5 里我们讲过，Python 端导出的 ONNX 图含有一批 `trt::` / `trt_edgellm::` 自定义算子域的融合算子（attention、MoE、量化 GEMM、Mamba、GDN 等）。TensorRT 核心库并不认识这些算子，它们必须由一批 **C++ 自定义插件** 来实现，否则 ONNX 解析器在构建期（u4-l1 的第 5 阶段「ONNX 解析」）就会报「未知算子」而失败。

学完本讲，你应该能够：

1. 说清楚一个 EdgeLLM 自定义算子是如何以 **TensorRT 插件** 的形式落地，以及插件的两件套结构：**本体（Plugin）+ 工厂（Creator）**。
2. 解释插件库 `NvInfer_edgellm_plugin` 为什么 **必须是 SHARED（共享库）而不是静态库**，并描述它被宿主进程 `dlopen` 加载、注册、运行的完整链路。
3. 看懂 `AttentionPlugin`、`Int4GroupwiseGemmPlugin`、`Nvfp4MoePlugin` 三类代表性插件各自承担的职责。

本讲只讲「插件这层架构与注册加载机制」，不深入每个算子的 CUDA kernel 实现细节——那是下一讲 u8-l2（自定义 CUDA 算子）的内容。

## 2. 前置知识

- **IPluginV3 / IPluginCreatorV3**：TensorRT 插件的标准接口。EdgeLLM 正在把全部插件迁移到 V3（见 `tensorrt-plugins.md`）。V3 把插件「能做的事」拆成几个「One」子接口：Core（元信息）、Build（形状/格式推导）、Runtime（执行）。一个插件类用**多继承**同时实现它们。
- **TensorRT 插件注册表（plugin registry）**：一个进程级全局表，记录「插件名字 + 版本 → Creator 工厂指针」。构建期解析 ONNX 时，TensorRT 按算子的名字+版本去这张表里查 Creator，由 Creator `createPlugin` 出实例。
- **`dlopen` / `dlsym`**：Linux 动态库加载接口。`dlopen` 把一个 `.so` 映射进进程地址空间并触发其全局对象的静态初始化；`dlsym` 按符号名取函数/变量地址。
- **静态库 vs 共享库**：静态库（`.a`）只是目标文件（`.o`）的归档，链接器只会把「被实际引用」的符号拷进最终可执行文件；共享库（`.so`）是整块独立映像，`dlopen` 时整块映射、所有全局对象一律构造。
- **前置讲义**：u2-l5（自定义算子与 Dynamo 翻译，本讲的 Python 端来源）、u4-l1（构建器八阶段流程，本讲对应其「阶段 1 插件加载」与「阶段 5 ONNX 解析」）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `cpp/plugins/attentionPlugin/attentionPlugin.h` | `AttentionPlugin`（本体）与 `AttentionPluginCreator`（工厂）的类声明，是本讲讲解 V3 接口的主样本 |
| `cpp/plugins/attentionPlugin/attentionPlugin.cpp` | 上述两者的实现，含 `REGISTER_TENSORRT_PLUGIN` 注册、字段表、`enqueue` 执行 |
| `cpp/plugins/int4GroupwiseGemmPlugin/int4GroupwiseGemmPlugin.cpp` | 最简单的插件样本（INT4 仅权重组量化 GEMM/GEMV），适合作为「新增插件」的模板阅读 |
| `cpp/plugins/nvfp4MoePlugin/README.md` | NVFP4 MoE 插件的 ONNX 输入面、对齐约束、与 GeForce 版插件的关系 |
| `cpp/plugins/utils/pluginUtils.h` / `.cpp` | 插件公共工具：导出宏 `EDGELLM_PLUGIN_EXPORT`、C-ABI 入口 `initEdgellmPlugins`、workspace 对齐工具 |
| `cpp/common/trtUtils.h` | `loadEdgellmPluginLib()`：宿主进程 `dlopen` 插件库的代码 |
| `cpp/CMakeLists.txt` | 把 `NvInfer_edgellm_plugin` 建成 **SHARED** 库、其它三个静态库、隐藏符号可见性的构建脚本 |
| `cpp/builder/llmBuilder.cpp` | 构建器在 `build()` 开头调用 `loadEdgellmPluginLib()`，对应八阶段的第一阶段 |
| `docs/source/developer_guide/customization/tensorrt-plugins.md` | 官方插件指南，是本讲代码实践任务的依据 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**插件两件套与注册机制**（4.1）、**CMake 构建目标与共享库加载**（4.2）、**AttentionPlugin 落地样本**（4.3）、**量化类插件 nvfp4Moe / int4GroupwiseGemm**（4.4）。

### 4.1 插件两件套：本体（Plugin）+ 工厂（Creator）

#### 4.1.1 概念说明

TensorRT 插件采用经典的 **工厂模式**，每个算子有「两个类」：

- **插件本体（Plugin）**：实现 `IPluginV3`，负责「这个算子怎么算」——形状推导、格式校验、`enqueue` 真正执行 CUDA kernel。
- **插件工厂（Creator）**：实现 `IPluginCreatorV3One`，负责「怎么造出这个插件」——对外报出自己的名字+版本、声明它要哪些配置参数（PluginField）、用 `createPlugin` 按参数造实例。

为什么要把「工厂」和「本体」分开？因为 TensorRT 需要在**还没有插件实例**的情况下就能查询「有哪些插件、每个插件要什么参数」。Creator 是「类级别的元信息」，本体是「实例」。Creator 通常是个无状态单例，由 `REGISTER_TENSORRT_PLUGIN` 宏注册到全局 `getPluginRegistry()`。

#### 4.1.2 核心流程

一个插件从「被构建器需要」到「执行」的完整生命周期：

1. 进程 `dlopen` 插件库 → 各 `.cpp` 文件里的 `REGISTER_TENSORRT_PLUGIN(XxxCreator)` 宏展开成全局对象，构造时把 Creator 指针塞进全局注册表（见 4.2）。
2. 构建期：ONNX 解析器遇到一个自定义算子节点（名字+版本），去注册表查 Creator。
3. Creator 的 `createPlugin(name, fieldCollection, phase)`：从 ONNX 节点的属性里解出参数（如 `num_q_heads`），`new` 一个本体实例返回。
4. 本体的 Build 接口被反复调用做形状/格式推导（`getOutputShapes`、`supportsFormatCombination`），TensorRT 据此选 kernel、规划内存。
5. 编译出的 engine 里序列化了插件参数（`getFieldsToSerialize`）。
6. 运行期：反序列化重建插件，调用 Runtime 接口；每步推理调 `enqueue(...)` 执行 CUDA kernel。

```
ONNX 节点 ──按名查──▶ Registry ──▶ Creator
                                       │ createPlugin(fc)
                                       ▼
                                  Plugin 实例
                                  ├─ getOutputShapes / supportsFormatCombination  (Build 期)
                                  ├─ getFieldsToSerialize                        (序列化进 engine)
                                  └─ enqueue(inputs, outputs, workspace, stream) (Runtime 期)
```

#### 4.1.3 源码精读

注册就一行宏。attentionPlugin 在文件级匿名常量里定义名字和版本，然后注册 Creator：

[cpp/plugins/attentionPlugin/attentionPlugin.cpp:L57-L58](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.cpp#L57-L58) — 定义插件名字常量 `"AttentionPlugin"` 和版本 `"1"`。TensorRT 查注册表时用「名字+版本」二元组匹配。

[cpp/plugins/attentionPlugin/attentionPlugin.cpp:L252](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.cpp#L252) — `REGISTER_TENSORRT_PLUGIN(AttentionPluginCreator)`。这是 TensorRT SDK 提供的宏，展开成一个带构造函数的全局对象，构造时把 `AttentionPluginCreator` 单例注册进全局表。整个 `cpp/plugins/` 下有 14 个这样的注册点（每个插件目录一个 Creator）。

本体类用**多继承**同时实现 V3 的几个「One」子接口，这是 V3 的关键写法：

[cpp/plugins/attentionPlugin/attentionPlugin.h:L39-L43](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.h#L39-L43) — `AttentionPlugin` 同时继承 `IPluginV3`、`IPluginV3OneCore`、`IPluginV3OneBuildV2`、`IPluginV3OneRuntime`。TensorRT 通过下面的 `getCapabilityInterface` 问它「要哪个能力接口」，它把自己 `static_cast` 成对应子接口返回：

[cpp/plugins/attentionPlugin/attentionPlugin.cpp:L526-L544](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.cpp#L526-L544) — `getCapabilityInterface`：请求 `kBUILD` 返回 `IPluginV3OneBuildV2*`、请求 `kRUNTIME` 返回 `IPluginV3OneRuntime*`、其余返回 `IPluginV3OneCore*`。这就是 V3「能力拆分」的分发点。

工厂类则实现 `IPluginCreatorV3One`：

[cpp/plugins/attentionPlugin/attentionPlugin.h:L193-L211](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.h#L193-L211) — `AttentionPluginCreator` 声明 `getPluginName/Version`、`getFieldNames`（暴露配置参数）、`createPlugin`（造实例）。它持有静态的 `mFieldCollection` 与 `mPluginAttributes`，所有实例共享一份字段定义。

工厂构造函数里登记这个插件接受的所有配置参数（即 ONNX 节点属性 → PluginField）：

[cpp/plugins/attentionPlugin/attentionPlugin.cpp:L1592-L1613](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.cpp#L1592-L1613) — Creator 构造函数把 `num_q_heads`、`num_kv_heads`、`head_size`、`attention_scale`、`enable_tree_attention`、`enable_fp8_kv_cache`、`enable_vision_block_attention`、`sliding_window_size`、`qkv_scales` 逐个 `emplace_back` 成 `PluginField`，组装成 `mFieldCollection`。这些字段名就是 Python 端 ONNX 自定义算子 schema（u2-l5）里登记的属性名，两端必须对齐。

`createPlugin` 则把 fieldCollection 解出来、`new` 本体：

[cpp/plugins/attentionPlugin/attentionPlugin.cpp:L1640-L1654](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.cpp#L1640-L1654) — `createPlugin` 把 `fc` 透传给本体的「从 fieldCollection 构造」构造函数（见 [attentionPlugin.cpp:L441-L518](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.cpp#L441-L518)），用 `parsePluginScalarField<T>` 逐字段取值。

#### 4.1.4 代码实践

**实践目标**：通过阅读源码，建立「插件 = 本体 + 工厂 + 注册宏」的整体印象，并确认 14 个注册点。

**操作步骤**：

1. 用 grep 列出全部注册点（只读命令，不改源码）：
   ```bash
   grep -rn "REGISTER_TENSORRT_PLUGIN" cpp/plugins/
   ```
2. 预期看到 14 行，分布在 attention、mamba、gatedDeltaNet、各种 MoE、各种 GEMM、allReduce、vitAttention、gemma4AudioAttention、dflashTargetKVCacheUpdate 等目录。
3. 打开 `cpp/plugins/int4GroupwiseGemmPlugin/int4GroupwiseGemmPlugin.cpp`（最简单的插件），对照下面 4.4 节阅读：找它的名字常量、`REGISTER` 宏、Creator 字段表、`enqueue`。

**需要观察的现象 / 预期结果**：每个插件目录都遵循同样的「两件套 + 注册宏」结构；最简单的 int4 GEMM 插件只有约 360 行，是理解插件骨架的最佳模板。运行结果**待本地验证**（只需阅读，不需 GPU）。

#### 4.1.5 小练习与答案

**练习 1**：为什么插件要有「工厂（Creator）」和「本体（Plugin）」两个类，不能合并成一个？

**答案**：TensorRT 需要在**没有插件实例**时就能枚举「有哪些插件、各自要什么参数」（这是 Creator 的 `getPluginName`/`getFieldNames` 职责），只有在真正解析到一个 ONNX 节点、拿到具体参数后，才需要造一个实例（`createPlugin`）。Creator 是「类级元信息/无状态单例」，本体是「带参数的实例」，职责不同故拆开。

**练习 2**：`getCapabilityInterface` 里为什么是 `static_cast` 而不是 `dynamic_cast`？

**答案**：插件类用多继承**同时**实现了 Core/Build/Runtime 三个子接口，`this` 指向的对象本就同时是这三种类型，`static_cast` 在编译期完成、零开销；这里不存在「可能不是该类型」的运行期不确定，`dynamic_cast` 的运行期 RTTI 开销是多余的。

---

### 4.2 CMake 插件目标：为什么必须是 SHARED

#### 4.2.1 概念说明

`cpp/CMakeLists.txt` 一共建了 4 个库目标：3 个静态库（`edgellmKernels`、`edgellmCore`、`edgellmBuilder`）和 1 个**共享库** `NvInfer_edgellm_plugin`。插件库之所以独享「SHARED」待遇，由两个机制共同决定：

1. **运行期动态加载**：TensorRT 宿主进程通过 `dlopen` 在运行期把插件库映射进地址空间，再从全局注册表里按名字查 Creator。`dlopen` 只能作用于 `.so`，无法作用于静态库 `.a`。
2. **静态初始化触发注册**：`REGISTER_TENSORRT_PLUGIN` 宏展开成一个全局对象，其构造函数往注册表里塞 Creator。这段构造代码只有在 `.so` 被 `dlopen` 整块映射时才会执行；若放进静态库，链接器发现「没人直接引用这个 Creator 符号」就会把它丢弃，注册永远不会发生——注册表为空，TensorRT 一个插件都找不到。

此外，插件库用 `-fvisibility=hidden` 隐藏所有内部符号，只让带 `EDGELLM_PLUGIN_EXPORT`（`__attribute__((visibility("default")))`）的 C-ABI 入口 `initEdgellmPlugins` 暴露出来，供宿主 `dlsym`。

#### 4.2.2 核心流程

插件库从被构建到被使用的链路：

```
cmake --build
  └─ NvInfer_edgellm_plugin  (SHARED)  →  build/libNvInfer_edgellm_plugin.so
       │  -fvisibility=hidden：内部符号不可见
       │  唯一可见导出：initEdgellmPlugins (EDGELLM_PLUGIN_EXPORT)
       │  --exclude-libs,ALL：丢弃从静态档案拉进来的符号
       ▼
宿主进程启动（builder / runtime / example）
  └─ loadEdgellmPluginLib()
       ├─ 读 EDGELLM_PLUGIN_PATH（否则默认 build/libNvInfer_edgellm_plugin.so）
       ├─ dlopen(path, RTLD_LAZY | RTLD_NODELETE)
       │     └─ 触发 14 个 REGISTER 宏的全局对象构造 → 填充 getPluginRegistry()
       └─ dlsym("initEdgellmPlugins") → 同步日志级别
```

#### 4.2.3 源码精读

构建脚本里插件库是唯一标 `SHARED` 的目标：

[cpp/CMakeLists.txt:L148](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/CMakeLists.txt#L148) — `add_library(NvInfer_edgellm_plugin SHARED ${PLUGIN_CPP_SRCS})`。对比另外三个目标 [edgellmKernels STATIC（L104）](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/CMakeLists.txt#L104)、[edgellmCore STATIC（L116-L128）](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/CMakeLists.txt#L116-L128)、[edgellmBuilder STATIC（L140）](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/CMakeLists.txt#L140)，只有插件库是共享库。

隐藏内部符号、只留导出入口的两处设置：

[cpp/CMakeLists.txt:L28-L31](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/CMakeLists.txt#L28-L31) — `edgellm_set_hidden_visibility` 把目标的 `CXX_VISIBILITY_PRESET` 设为 `hidden`，注释明说「只让 `EDGELLM_PLUGIN_EXPORT` 入口可见，防止多个库一起加载时的符号冲突」。

[cpp/CMakeLists.txt:L157-L163](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/CMakeLists.txt#L157-L163) — 插件库链接 `edgellmKernels` 等静态档案，并加 `-Wl,--exclude-libs,ALL` 把从静态档案拉进来的符号全部「降回 hidden」，避免它们意外成为导出符号而和别处冲突。

那个唯一对外可见的 C-ABI 入口，靠导出宏声明与定义：

[cpp/plugins/utils/pluginUtils.h:L32](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/utils/pluginUtils.h#L32) — `EDGELLM_PLUGIN_EXPORT` 即 `__attribute__((visibility("default")))`，在 hidden 默认可见性下强制让被标注符号导出。

[cpp/plugins/utils/pluginUtils.h:L37](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/utils/pluginUtils.h#L37) — `extern "C" EDGELLM_PLUGIN_EXPORT bool initEdgellmPlugins(void* logger, char const* libNamespace)`，遵循 TRT 的 `initLibNvInferPlugins` 约定。

[cpp/plugins/utils/pluginUtils.cpp:L26-L37](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/utils/pluginUtils.cpp#L26-L37) — `initEdgellmPlugins` 实现：把宿主传来的 logger `dynamic_cast` 成 `EdgeLLMLogger`，把日志级别同步过来，使插件里的 `LOG_DEBUG` 也遵循应用日志级别。注意它**不做注册**——注册是 `REGISTER` 宏的全局对象在 `dlopen` 时自动完成的，这个函数只同步日志。

宿主端的加载逻辑在公共头里：

[cpp/common/trtUtils.h:L60-L92](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/trtUtils.h#L60-L92) — `loadEdgellmPluginLib()`。要点：
- [L62](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/trtUtils.h#L62) 读环境变量 `EDGELLM_PLUGIN_PATH`，[L71](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/trtUtils.h#L71) 否则默认 `build/libNvInfer_edgellm_plugin.so`。
- [L74-L76](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/trtUtils.h#L74-L76) `dlopen(path, RTLD_LAZY | RTLD_NODELETE)`。注释点明 `RTLD_NODELETE` 的理由：engine 在 `dlclose` 之后仍可能使用插件代码，故库必须在进程生命周期内常驻，否则卸载后再访问会崩溃。
- [L84-L88](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/trtUtils.h#L84-L88) `dlsym("initEdgellmPlugins")` 取出入口并调用，传入宿主 logger。

这个函数在每个会用到插件的入口都被调一次（对应 u4-l1 八阶段的第一阶段「插件加载」）：

[cpp/builder/llmBuilder.cpp:L179-L180](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L179-L180) — `LLMBuilder::build()` 第一行就是 `auto pluginHandles = loadEdgellmPluginLib();`。`pluginHandles` 是个 `unique_ptr<void, DlDeleter>`，持有 `.so` 句柄存活到 `build()` 结束。运行时侧（如 `examples/llm/llm_inference.cpp:L536`、`experimental/pybind/edgellm_pybind.cpp:L112`）同样在构造 runtime 前先加载插件库。

#### 4.2.4 代码实践

**实践目标**：亲手验证「插件库是 SHARED，且它就是被 `dlopen` 的那个 `.so`」。

**操作步骤**：

1. 打开 `cpp/CMakeLists.txt`，确认 [L148](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/CMakeLists.txt#L148) 是 `add_library(NvInfer_edgellm_plugin SHARED ...)`，而 `edgellmKernels`/`edgellmCore`/`edgellmBuilder` 都是 `STATIC`。
2. 用只读 grep 找出全部 `loadEdgellmPluginLib()` 调用点，确认构建器与运行时都依赖它：
   ```bash
   grep -rn "loadEdgellmPluginLib" cpp/ examples/ experimental/
   ```
3. 假设你把 [L148](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/CMakeLists.txt#L148) 的 `SHARED` 改成 `STATIC` 重新构建：构建产物将不再是 `.so`，下游 `loadEdgellmPluginLib` 里 `dlopen("build/libNvInfer_edgellm_plugin.so")` 必然失败，日志会打印 `Cannot open plugin library: ...`。

**需要观察的现象 / 预期结果**：SHARED 版本构建后产出 `build/libNvInfer_edgellm_plugin.so`，正好是 `loadEdgellmPluginLib` 的默认搜索路径。改成 STATIC 则无 `.so` 产出，`dlopen` 失败——这是「静态库不能当插件库」的最直接证据。其余运行结果**待本地验证**（需完整 TensorRT + CUDA 环境）。

#### 4.2.5 小练习与答案

**练习 1**：`REGISTER_TENSORRT_PLUGIN` 宏背后靠什么机制把 Creator 塞进注册表？为什么放进静态库就不生效？

**答案**：宏背后是一个全局对象的构造，靠静态初始化（static initialization）执行。静态库只有被链接器「实际引用」的符号才进可执行文件；TensorRT 宿主不直接引用这些 Creator 对象，于是它们被链接器丢弃、构造不执行、注册表为空。共享库则被 `dlopen` 整块映射，所有全局对象必然构造。

**练习 2**：`dlopen` 为什么要带 `RTLD_NODELETE`？去掉它会在什么时刻出问题？

**答案**：`RTLD_NODELETE` 保证 `dlclose` 时**不真正卸载**库映射。TensorRT 的 engine 在 `loadEdgellmPluginLib` 返回、句柄析构（`dlclose`）之后仍可能调用插件代码（例如 CUDA graph 录制的 kernel 指针、延迟执行的任务）；若库被卸载，这些地址失效，访问即段错误。带 `RTLD_NODELETE` 让库常驻进程整个生命周期。

---

### 4.3 AttentionPlugin：融合注意力的落地样本

#### 4.3.1 概念说明

`AttentionPlugin` 是 EdgeLLM 最核心、也最重的插件：它把「RoPE 位置编码 + KV cache 读写 + MHA/GQA 注意力计算」**融合成一个算子**，对应 Python 端 ONNX 图里的 `trt_edgellm::Attention` 节点（u2-l5）。之所以融合，是因为这三步在标准 TensorRT 拆开实现会引入大量中间张量与显存往返，融合后能直接在 kernel 里就地读写 KV cache、省掉中间落盘。

它要同时服务多种执行模式：prefill（含 chunked prefill）、vanilla decode、tree decode（EAGLE/DFlash 投机解码用的树形注意力）、Gemma4 视觉块注意力，还要支持 FP16 与 FP8 两种 KV cache。这些模式由 `enqueue` 在运行期根据输入张量形状推断（见下）。

#### 4.3.2 核心流程

`AttentionPlugin::enqueue` 的执行框架（不含 kernel 细节）：

1. 从 I/O 描述符构造非拥有（non-owned）的 `rt::Tensor` 视图（Q/K/V、KV cache、context length、RoPE cos/sin、KV cache start idx）——复用 u5-l6 的 Tensor 抽象。
2. 由 Q 的序列长度、KV cache start idx、可选的 position id **推断执行模式**：normal prefill / chunked prefill / vanilla decoding / tree decoding。
3. 在 workspace 里按需切出若干中间缓冲（cu_seqlens、转置 KV、FP8 Q、packed mask 等），workspace 大小由 `getWorkspaceSize` 在构建期上报。
4. 分派到具体 kernel：prefill 走 CuTe DSL FMHA（Blackwell）或 FMHA_v2，headSize=512 走 FFPA；decode 走 XQA。这些 kernel 源码在 `kernelSrcs/`，是下一讲 u8-l2 的主题。

#### 4.3.3 源码精读

执行模式推断是「形状驱动的」，这是同一个插件能服务多种场景的关键：

[cpp/plugins/attentionPlugin/attentionPlugin.cpp:L90-L97](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.cpp#L90-L97) — `AttentionExecutionMode` 枚举：`kNORMAL_PREFILL`、`kCHUNKED_PREFILL`、`kVANILLA_DECODING`、`kTREE_DECODING`。

[cpp/plugins/attentionPlugin/attentionPlugin.cpp:L99-L116](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.cpp#L99-L116) — `deduceModeVanilla`：KV cache start idx 为空 → normal prefill；否则看 Q 序列长度，>1 是 chunked prefill，==1 是 vanilla decoding。

`enqueue` 是 `noexcept` 的（TensorRT 要求），所以用一个 try/catch 包装真正的 `enqueueImpl`，把异常转成失败返回值，避免异常逃逸导致进程终止：

[cpp/plugins/attentionPlugin/attentionPlugin.cpp:L877-L897](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.cpp#L877-L897) — `enqueue` 包裹 `enqueueImpl`，捕获 `std::exception` 与 `...`，记 `LOG_ERROR` 并返回 -1。这是所有插件 `enqueue` 的标准写法。

序列化进 engine 的字段（运行期反序列化靠它重建插件）：

[cpp/plugins/attentionPlugin/attentionPlugin.cpp:L1569-L1586](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.cpp#L1569-L1586) — `getFieldsToSerialize` 把当前实例的 `mNumQHeads`/`mNumKVHeads`/`mHeadSize`/`mAttentionScale`/各开关/`mQkvScales` 打包成 PluginFieldCollection。注意它和 Creator 构造函数 [L1598-L1609](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.cpp#L1598-L1609) 的字段名一一对应——序列化与反序列化共用同一套字段契约。

输入/输出契约（KV cache 是「就地更新」的，past 与 present 绑同一地址，呼应 u5-l3 的 TensorMap 约定）：

[cpp/plugins/attentionPlugin/attentionPlugin.cpp:L66-L84](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.cpp#L66-L84) — 定义 7 个必需输入（Q/K/V/KVCache/ContextLength/RopeCosSin/KVCacheStartIdx）、可选输入（tree attention mask/pos、vision block ids）、2 个输出（attention 结果 + KVCache）的下标常量。

[cpp/plugins/attentionPlugin/attentionPlugin.cpp:L861-L871](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.cpp#L861-L871) — `getAliasedInput` 返回 -1：注释解释本应声明「输出 KVCache 别名输入 KVCache」，但声明后会让 Myelin（TensorRT 优化器）多做一次冗余的逐层 KV 拷贝，故主动放弃别名声明，靠运行期把 past/present 绑同一地址实现就地读写。这是 EdgeLLM 与 TensorRT 优化器之间一个有据可查的「WAR（workaround）」。

#### 4.3.4 代码实践

**实践目标**：确认 attention 插件的「配置参数 → 序列化字段 → Python ONNX schema」三处字段名一致，理解它们是同一条契约。

**操作步骤**：

1. 在 [attentionPlugin.cpp:L1598-L1609](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/attentionPlugin/attentionPlugin.cpp#L1598-L1609) 抄下 Creator 登记的字段名（`num_q_heads`、`num_kv_heads`、`head_size` …）。
2. 对照官方文档 [tensorrt-plugins.md 的 AttentionPlugin「Configuration Parameters」](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/developer_guide/customization/tensorrt-plugins.md) 看是否吻合（注意文档里写的是面向用户的逻辑名，代码里是序列化用的下划线名）。
3.（可选，承接 u2-l5）在 `tensorrt_edgellm/onnx/onnx_custom_schemas.py` 里找 attention 算子的 schema，确认它声明的属性名与这里的字段名对得上。

**需要观察的现象 / 预期结果**：三处字段名一致，说明「Python 导出写入的 ONNX 属性 → 构建期 Creator `createPlugin` 读取 → 序列化进 engine → 运行期反序列化」是一条名字对齐的完整链路。运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `enqueue` 要是 `noexcept` 并自己 try/catch？

**答案**：TensorRT 插件接口约定 `enqueue` 不抛异常（它是 `noexcept` 的）。若让异常逃逸，C++ 运行期会调用 `std::terminate` 直接结束进程——一次 kernel 缺失或形状不匹配就会让整个推理服务崩溃。EdgeLLM 的做法是用 try/catch 把异常转成 `LOG_ERROR` + 返回 -1（失败码），让 TensorRT 把这次执行标记为失败而非杀进程。

**练习 2**：`AttentionPlugin` 为什么把 RoPE、KV cache 读写、注意力计算融合成一个算子，而不是拆成三个标准 ONNX 算子？

**答案**：拆开会引入大量中间张量（roped Q/K、读写出的 KV），且每步都要单独的 kernel 启动与显存往返；融合后 RoPE 可以在写 KV cache 的同一 kernel 里完成、注意力直接就地读 cache，省掉中间落盘与多次 global memory 往返，对 attention 这种访存密集型算子收益极大。这也是它必须做成自定义插件（而非纯标准算子组合）的根本原因。

---

### 4.4 nvfp4Moe / int4GroupwiseGemm：量化类插件

#### 4.4.1 概念说明

除了 attention，另一大类插件是**量化矩阵乘**：

- **`Int4GroupwiseGemmPlugin`**：INT4 仅权重组量化 GEMM/GEMV，对称量化、组大小 128，FP16 累加。服务 AWQ/GPTQ 量化的线性层（u3-l3 里的 int4 权重经 repacking 后喂给它）。
- **`Nvfp4MoePlugin`**：NVFP4（E2M1 浮点 4-bit）混合专家（MoE）插件，面向 SM100/SM101/SM110（Blackwell 数据中心/Thor）。它有一个孪生 `NvFP4MoEPluginGeforce` 面向 SM120/SM121（消费级 Blackwell/Spark GB10），两者 ONNX 接口相同但权重磁盘布局不同，Python 导出端按目标架构选插件与对应的 repack。

这些插件共同特点：**输入权重是量化的、特殊布局的**，标准 TensorRT GEMM 算子无法消费，故必须自写 kernel 并包成插件。权重的具体布局（AWQ 列打包、GPTQ 行打包、NVFP4 两级缩放等）由 Python 端 repacking（u2-l4 / u3-l3）保证与插件期望一致。

#### 4.4.2 核心流程

以 Int4GroupwiseGemm 为例的执行流：

1. 构建期：Creator 暴露 `gemm_n`/`gemm_k`/`group_size` 三个参数；`supportsFormatCombination` 校验输入是 `[M,K]` FP16 激活 + `[N/2,K]` INT8（装 INT4 权重）+ `[K/group,N]` FP16 缩放，输出 `[?,N]` FP16。
2. 运行期 `enqueue`：从输入形状算出 M（token 数），按 `N` 是否对齐 CTA_N=128 决定走 GEMM 还是 GEMV kernel，调用 `gemm_forward_cuda_new`。

NVFP4 MoE 更复杂：11 个输入（router_logits、hidden_states、fc1/fc2 的量化权重+block scale+alpha、全局 scale、down scale、score 修正偏置），1 个输出；`configurePlugin` 强制对齐约束（hidden_size 整除 128、num_experts ∈ {128,256}、top_k ≤ 8 等）。

#### 4.4.3 源码精读

Int4GroupwiseGemm 是最干净的插件模板，字段极简：

[cpp/plugins/int4GroupwiseGemmPlugin/int4GroupwiseGemmPlugin.cpp:L308-L320](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/int4GroupwiseGemmPlugin/int4GroupwiseGemmPlugin.cpp#L308-L320) — Creator 构造函数只登记 `gemm_n`、`gemm_k`、`group_size` 三个 `PluginField`。对比 attention 的 9 个字段，这是「最小插件」该有的样子。

[cpp/plugins/int4GroupwiseGemmPlugin/int4GroupwiseGemmPlugin.cpp:L262-L276](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/int4GroupwiseGemmPlugin/int4GroupwiseGemmPlugin.cpp#L262-L276) — `enqueue` 的分派：`useGemv` 在「M 很小」或「N 不整除 128」时为真，走 GEMV kernel（按 ≤6 行切块）；否则走标准 GEMM kernel `gemm_forward_cuda_new`。注释说明 GEMV 模板只实例化了 M=1..6，故需切块——这是量化 kernel 在小 batch 下的已知局限。

[cpp/plugins/int4GroupwiseGemmPlugin/int4GroupwiseGemmPlugin.cpp:L174-L228](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/int4GroupwiseGemmPlugin/int4GroupwiseGemmPlugin.cpp#L174-L228) — `supportsFormatCombination`：逐 `pos` 校验每个输入/输出的 dtype、format、dims。这是插件告诉 TensorRT「我能接受什么格式」的唯一渠道，TensorRT 据此决定能否把上游/下游张量连到本插件。

NVFP4 MoE 的接口面与对齐约束（在 README 里描述得最清楚）：

[cpp/plugins/nvfp4MoePlugin/README.md:L33-L48](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/nvfp4MoePlugin/README.md#L33-L48) — 支持形状表：`io_dtype`=FP16、`SM`∈{100,101,110}、`activation_type`∈{swiglu,relu2}、`routing_mode`∈{softmax top-k, sigmoid grouped top-k}、`num_experts`∈{128,256}、`top_k`∈(0,8]，并要求 `hidden_size%128==0`、`moe_inter_size%64==0`。`configurePlugin` 会在构建期强制这些约束，违反时报清晰错误。

[cpp/plugins/nvfp4MoePlugin/README.md:L7-L24](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/nvfp4MoePlugin/README.md#L7-L24) — 说明 `Nvfp4MoePlugin`（SM100/101/110）与 `NvFP4MoEPluginGeforce`（SM120/121）共享同一 11 输入/1 输出的 ONNX 面与属性集，但消费**不同的磁盘权重布局**（FC1 一个是 64 行 up/gate 交织、一个是 up/gate 拼接），由 Python 导出端按目标架构选插件并配 repack。这正是 u3-l3 讲的「量化权重布局与 repack」在插件侧的落点。

#### 4.4.4 代码实践

**实践目标**：以 Int4GroupwiseGemm 为模板，列出「新增一个自定义插件要实现哪些 TRT 接口方法」——这也是本讲总实践任务的后半部分。

**操作步骤**：

1. 打开 [int4GroupwiseGemmPlugin.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/int4GroupwiseGemmPlugin/int4GroupwiseGemmPlugin.cpp)，把它当作「最小插件骨架」，逐个核对本讲「综合实践」里列出的接口方法清单是否都有实现。
2. 对照 [tensorrt-plugins.md 的 Int4GroupwiseGemmPlugin 一节（L73-L106）](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/developer_guide/customization/tensorrt-plugins.md) 的「Integration Workflow」（量化 → 导出 → 构建 → 运行四阶段），确认插件如何贯穿流水线。

**需要观察的现象 / 预期结果**：一个最小插件需要实现的接口方法清单（详见综合实践答案）。运行结果**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`Nvfp4MoePlugin` 与 `NvFP4MoEPluginGeforce` 为什么是两个插件而不是一个带参数的插件？

**答案**：两者面向不同 SM 架构（数据中心 Blackwell vs 消费级 Blackwell），底层 AOT kernel 模块与它消费的**权重磁盘布局**都不同（FC1 的 up/gate 交织 vs 拼接）。插件本身只是 kernel 的薄包装，kernel 不同就只能拆成两个插件；Python 导出端按目标架构二选一，并配对应的 repack。

**练习 2**：Int4GroupwiseGemm 的 `enqueue` 在什么情况下走 GEMV 而不是 GEMM？为什么？

**答案**：当 `M`（token 数）很小（≤ `kGemvMaxM`）或 `N` 不整除 CTA_N=128 时走 GEMV。因为 GEMM kernel 要求 N 对齐 128 的 CDA tile；非对齐的小 N（如 GDN 的 32 维 in_proj）只能走 GEMV，而 GEMV 模板只实例化了 M=1..6，故还要把 M 切成 ≤6 行的块循环处理（代码注释里的已知待优化点）。

---

## 5. 综合实践

**任务**：本讲代码实践任务的两问合起来做一个完整练习——

1. **解释 `NvInfer_edgellm_plugin` 为什么必须是 SHARED 库**（不要只答「因为要 dlopen」，要把「注册机制 + 链接器行为」两层都讲清楚）。
2. **参照 `tensorrt-plugins.md` 文档与 `int4GroupwiseGemmPlugin` 源码，列出新增一个自定义插件需要实现的 TRT 接口方法**，并按「本体（Plugin）/ 工厂（Creator）」两类分组。

### 操作步骤

1. 复习 4.2.1 与 4.2.5 练习 1，组织「SHARED 而非 STATIC」的两层理由。
2. 通读 [int4GroupwiseGemmPlugin.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/plugins/int4GroupwiseGemmPlugin/int4GroupwiseGemmPlugin.cpp)，把其中实现的每个 `override` 方法记下来。
3. 给你的清单分组：哪些属于 `IPluginV3`/`IPluginV3OneCore`/`IPluginV3OneBuild`/`IPluginV3OneRuntime`（本体），哪些属于 `IPluginCreatorV3One`（工厂）。

### 参考答案

**第 1 问**：必须 SHARED，原因有二。① TensorRT 宿主在运行期用 `dlopen` 加载插件库、靠全局注册表按名查 Creator，`dlopen` 只能作用于 `.so`。② `REGISTER_TENSORRT_PLUGIN` 靠全局对象静态初始化往注册表注册；若放进静态库，链接器因「无人直接引用 Creator 符号」会丢弃它，注册不发生；共享库被 `dlopen` 整块映射，全局对象必然构造。二者叠加，插件库必须是 SHARED。

**第 2 问**（对照 int4GroupwiseGemmPlugin 与 attentionPlugin 的实际实现）：

**本体（Plugin，继承 IPluginV3 + IPluginV3OneCore + IPluginV3OneBuildV2 + IPluginV3OneRuntime）**：
- `IPluginV3`：`getCapabilityInterface`（分发到 Core/Build/Runtime）、`clone`（多上下文时复制实例）。
- `IPluginV3OneCore`：`getPluginName`、`getPluginVersion`、`getPluginNamespace`（`setPluginNamespace`）。
- `IPluginV3OneBuildV2`：`getNbOutputs`、`getOutputDataTypes`、`getOutputShapes`（形状推导）、`supportsFormatCombination`（格式校验）、`configurePlugin`、`getWorkspaceSize`、`getAliasedInput`（声明输出别名哪个输入，KV cache 就地更新用）。
- `IPluginV3OneRuntime`：`enqueue`（真正执行 kernel，须 `noexcept` 并自己 try/catch）、`onShapeChange`、`attachToContext`（多上下文挂载）、`getFieldsToSerialize`（把参数序列化进 engine）。

**工厂（Creator，继承 IPluginCreatorV3One）**：
- `getPluginName`、`getPluginVersion`（与本体一致）、`getFieldNames`（暴露 PluginField 列表）、`getPluginNamespace`（`setPluginNamespace`）、`createPlugin(name, fieldCollection, phase)`（按参数造实例）。

最后还要在某个 `.cpp` 里写一行 `REGISTER_TENSORRT_PLUGIN(YourCreator)`，确保它在插件库 `dlopen` 时被注册。

> 以上方法清单依据 int4GroupwiseGemmPlugin.cpp 与 attentionPlugin.{h,cpp} 的真实实现整理，与 [tensorrt-plugins.md](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/developer_guide/customization/tensorrt-plugins.md) 的 Integration Workflow 一致。各方法的完整调用时机**待本地验证**（需搭建 TensorRT 插件开发环境）。

## 6. 本讲小结

- EdgeLLM 的自定义 ONNX 算子（u2-l5）在 C++ 端落地为 **TensorRT 插件**，采用「**本体（Plugin）+ 工厂（Creator）**」两件套：Creator 报名字/参数并造实例，本体做形状推导与 `enqueue` 执行。
- 插件用 V3 接口，靠**多继承** `IPluginV3OneCore/BuildV2/Runtime` + `getCapabilityInterface` 的 `static_cast` 分发能力；全仓 14 个 Creator 各用一行 `REGISTER_TENSORRT_PLUGIN` 注册。
- 插件库 `NvInfer_edgellm_plugin` 必须是 **SHARED**——因为 TRT 运行期靠 `dlopen` 触发 `REGISTER` 宏的静态初始化来填充全局注册表，静态库的注册代码会被链接器丢弃。
- 宿主在构建器/运行时入口调 `loadEdgellmPluginLib()`：`dlopen(..., RTLD_NODELETE)` 加载库（库须常驻进程以免卸载后访问崩溃），再 `dlsym("initEdgellmPlugins")` 同步日志级别；路径可用 `EDGELLM_PLUGIN_PATH` 覆盖。
- `AttentionPlugin` 把 RoPE + KV cache 读写 + MHA/GQA 融成一个算子，靠形状推断在 prefill/chunked/vanilla/tree/vision 多种模式间分派；`Int4GroupwiseGemmPlugin` 是最小插件模板；`Nvfp4MoePlugin` 与其 GeForce 孪生按 SM 架构分工、共享 ONNX 面但消费不同权重布局。
- 插件 `enqueue` 一律 `noexcept` 并自包 try/catch，把异常转成失败码而非终止进程；KV cache 靠「past/present 绑同一地址 + 放弃别名声明」实现就地读写。

## 7. 下一步学习建议

- **u8-l2 自定义 CUDA 算子（FMHA/MoE/Mamba）**：本讲只到「插件壳」，真正的计算在 `kernelSrcs/` 与 `cpp/kernels/` 的 CUDA kernel 里，下一讲拆开 FMHA（context/decode）、MoE（topk/align/per-expert/nvfp4）、按 SM 架构构建等细节。
- **回看 u2-l5 / u3-l3**：本讲是 u2-l5 的 Python ONNX 自定义算子在 C++ 的对端，重读 dynamo_translations 与 repacking 能把「Python schema → 量化权重布局 → 插件期望布局」整条契约串起来。
- **阅读 `tensorrt-plugins.md` 与 `customization-guide.md`**：前者是本讲代码实践的依据，后者给出「接入新算子/新模型」的扩展边界，是 u9-l4「接入新模型架构」的前置。
- **想动手时**：以 `int4GroupwiseGemmPlugin` 为模板复制一份，改名字、改 `enqueue` 里的 kernel 调用，跑一遍 export → build → inference（u1-l5）验证你的新插件能被注册、解析、执行。
