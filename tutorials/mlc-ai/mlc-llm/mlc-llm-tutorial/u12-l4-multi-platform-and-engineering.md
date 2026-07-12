# 多端部署与工程化（Android/iOS/WebLLM、bench、tests）

> 这是「扩展部署与工程化」单元（U12）的收官讲，也是整本手册的最后一篇。
> 前三篇（u12-l1 多 GPU、u12-l2 微服务路由、u12-l3 分离式推理）讲的都是「服务器侧如何把推理跑得更快、更省、更可扩展」；
> 本讲把视野拉到「工程全局」——同一个模型如何被打包部署到手机、Mac、浏览器，以及项目用什么基准测试与测试体系来保障编译器与引擎的质量。

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 MLC LLM 支持的移动端（Android/iOS/Mac Catalyst）与 Web 端（WebLLM/WebGPU）部署方式，以及它们各自需要什么「产物」。
- 跟踪 `mlc_llm package` 这条命令的完整流水线：从 `mlc-package-config.json` 到 `dist/` 下的静态库与 `mlc-app-config.json`。
- 掌握 `python -m mlc_llm.bench` 基准测试入口的关键参数，知道它支持哪些数据集与后端。
- 认识 `tests/` 目录如何按子系统组织测试，理解 pytest 的 marker 分类与 C++ 单元测试的构建方式。

## 2. 前置知识

本讲默认你已经读过分层入口（u2-l1）、`package` 命令的命令行层（u2-l3）以及 REST 服务器（u11-l2）。下面两个概念是本讲的基石：

- **三类模型产物**（见 u1-l4）：① MLC 权重（`params_shard_*.bin` + `tensor-cache.json`，跨平台共享）；② 模型库（`.so/.tar/.wasm/.a`，**平台专用**，由「架构 + 量化 + 元数据 + 平台」四要素决定）；③ `mlc-chat-config.json`（编译期与运行期共享的契约）。本讲的核心就是「如何把②模型库与①权重，按各平台的外壳（Android App / iOS App / 浏览器）封装好」。
- **JIT 兜底机制**（见 u1-l4）：`package` 默认不会要求你提前 `compile`，它内部直接调 `jit.jit()`，按配置内容算缓存键命中即复用、否则子进程跑标准 `compile`，行为受 `MLC_JIT_POLICY` 控制。

此外，移动端/Web 端推理本质上仍是同一套 C++ 引擎与编译产物，只是换了「外壳语言」（Java/Kotlin、Swift、TypeScript）与「计算后端」（移动 GPU、WebGPU）。所以本讲不涉及新的推理算法，只讲「打包 + 工程化」这一层。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [python/mlc_llm/cli/package.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/package.py) | `package` 子命令的**命令行层**：解析 argv，转交接口层。 |
| [python/mlc_llm/interface/package.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/package.py) | `package` 子命令的**接口层**：构建模型库、校验、按设备分发构建 binding。 |
| [android/MLCChat/mlc-package-config.json](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/android/MLCChat/mlc-package-config.json) | Android App 的打包配置（模型列表）。 |
| [android/README.md](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/android/README.md) | Android 目录入口（指向文档）。 |
| [docs/deploy/android.rst](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/deploy/android.rst) | Android 构建与部署官方教程。 |
| [docs/deploy/ios.rst](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/deploy/ios.rst) | iOS / Mac Catalyst 构建与部署官方教程（含 Swift API）。 |
| [docs/deploy/webllm.rst](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/deploy/webllm.rst) | WebLLM（浏览器/WebGPU）部署官方教程。 |
| [python/mlc_llm/bench/__main__.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/bench/__main__.py) | 基准测试总入口：解析参数、可选拉起 MLC server、跑流水线、出 CSV。 |
| [python/mlc_llm/bench/api_endpoint.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/bench/api_endpoint.py) | bench 的「后端」：定义 `SUPPORTED_BACKENDS` 与各 API 端点类。 |
| [python/mlc_llm/bench/dataset.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/bench/dataset.py) | bench 的「数据集」：定义 `SUPPORTED_DATASET` 与各数据集类。 |
| [tests/README.md](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/tests/README.md) | 测试总览：以 pytest 为主、C++ 经 TVM FFI 暴露给 Python 测。 |
| [tests/python/conftest.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/tests/python/conftest.py) | pytest marker（测试分类）注册。 |
| [CMakeLists.txt](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/CMakeLists.txt) | 顶层构建脚本，含 C++ 单元测试开关 `BUILD_CPP_TEST`。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**① 移动端与 Web 部署全景**、**② `package` 打包流水线**、**③ bench 基准测试**、**④ tests 测试体系**。后两者合起来回答「工程化如何保障质量」。

### 4.1 移动端与 Web 部署全景（Android/iOS/WebLLM）

#### 4.1.1 概念说明

MLC LLM 的「多端」并不是「每端重写一遍引擎」，而是「同一份模型库产物，套上不同语言的外壳、对接不同的计算后端」。三类典型端如下：

- **Android**：外壳语言是 Java/Kotlin，计算走移动 GPU（Adreno/Mali 等）。运行时是 **mlc4j**（MLC LLM 的 Java 库），核心是一个 `libtvm4j_runtime_packed.so`（承载模型执行逻辑）加一个约 60KB 的 `tvm4j_core.jar`（Java binding）。Android 项目的 `dist/lib/mlc4j` 是一个 **gradle 子工程**，你的 App 通过 `include ':mlc4j'` 引用它。
- **iOS / Mac Catalyst**：外壳语言是 Swift，计算走 Metal。提供 `MLCSwift` Swift 包，对外暴露与 OpenAI 风格一致的 `MLCEngine`（`engine.chat.completions.create(...)`）。产物是若干 `.a` 静态库（`libmlc_llm.a`、`libmodel_iphone.a`、`libtvm_runtime.a`、`libtokenizers_cpp.a`、`libsentencepiece.a`）。`iphone` 与 `macabi`（Mac Catalyst，即「为 iPad 设计的 Mac」应用）共用同一套 `ios/prepare_libs.sh`，后者只是多加 `--catalyst` 标志。
- **Web（WebLLM）**：外壳语言是 TypeScript，计算走 **WebGPU**。WebLLM 是一个独立的 npm 包（`@mlc-ai/web-llm`），模型库产物后缀是 `.wasm`（编译时用 `--device webgpu`）。在浏览器里，每个模型注册为一个 `ModelRecord`，含三个关键字段：`model`（HF 权重 URL）、`model_lib`（`.wasm` 库 URL）、`required_features`（如 `shader-f16`）。

一句话总结：**「模型权重」跨所有平台共享，「模型库」按平台专用，「外壳」按平台换语言**。这与 u1-l4 介绍的「三类产物」完全对齐。

#### 4.1.2 核心流程

无论哪个端，部署的整体流程都是「三段式」：

1. **准备权重与配置**：用 `convert_weight` + `gen_config` 生成 MLC 权重目录（含 `mlc-chat-config.json`），上传到 HuggingFace。这一步与平台无关。
2. **编译模型库**：用 `compile --device <平台>` 把模型编译成平台专用库。移动端由 `package` 命令内部 JIT 触发；Web 端则单独跑 `compile --device webgpu`。
3. **打包/外壳集成**：移动端把库与配置塞进 App 工程（`mlc_llm package` → gradle/Xcode）；Web 端把 `.wasm` 挂到一个 `ModelRecord`。

模型库的「四要素」在 WebLLM 文档里被明确列出（[docs/deploy/webllm.rst:230-238](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/deploy/webllm.rst)）：架构、量化、元数据（影响内存规划）、平台。任何一个要素不同，都需要一个独立的模型库。

#### 4.1.3 源码精读

**Android 的产物结构**——`mlc_llm package` 跑完后，`./dist/` 下应出现如下结构（节选自文档）：

```
dist/lib/mlc4j/
├── build.gradle
├── output/
│   ├── arm64-v8a/libtvm4j_runtime_packed.so   # 移动 GPU 执行逻辑
│   └── tvm4j_core.jar                          # ~60KB Java binding
└── src/main/assets/mlc-app-config.json         # App 配置
```

参见 [docs/deploy/android.rst:150-173](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/deploy/android.rst)，其中说明了 `libtvm4j_runtime_packed.so` 与 `tvm4j_core.jar` 的分工，以及 `dist/lib/mlc4j` 作为 gradle 子工程的引用方式。

**Android 打包配置**是一份真实的 `mlc-package-config.json`，列出了 App 默认打包的六个模型（Phi-3.5-mini、Qwen3-0.6B/1.7B、gemma-2-2b、Llama-3.2-3B、Mistral-7B）：

```jsonc
{
    "device": "android",
    "model_list": [
        {
            "model": "HF://mlc-ai/Phi-3.5-mini-instruct-q4f16_0-MLC",
            "estimated_vram_bytes": 4250586449,
            "model_id": "Phi-3.5-mini-instruct-q4f16_0-MLC",
            "overrides": { "prefill_chunk_size": 128 }
        },
        ...
    ]
}
```

见 [android/MLCChat/mlc-package-config.json:1-50](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/android/MLCChat/mlc-package-config.json)。注意每个模型都带 `estimated_vram_bytes`（运行期显存预估）和可选的 `overrides`（如把 `prefill_chunk_size` 调小以压低临时显存）。

**iOS Swift API**——MLCSwift 提供与 Python `MLCEngine` 同名同形的接口：

```swift
let engine = MLCEngine()
await engine.reload(modelPath: modelPath, modelLib: modelLib)
for await res in await engine.chat.completions.create(messages: [...]) {
    print(res.choices[0].delta.content!.asText())
}
```

见 [docs/deploy/ios.rst:388-410](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/deploy/ios.rst)。注意：Python、Swift、JS 三端对外都是「OpenAI 兼容 API」，这正是 u1-l1 说的「统一抽象 MLCEngine」在多端的落地。

**WebLLM 的 ModelRecord**——浏览器侧用一份 `AppConfig.model_list` 注册模型，`model` 指权重 URL、`model_lib` 指编译好的 `.wasm` URL：

```typescript
const appConfig: webllm.AppConfig = {
  model_list: [{
    model: "https://huggingface.co/...",
    model_id: "RedPajama-INCITE-Instruct-3B-v1",
    model_lib: "https://.../RedPajama-...-webgpu.wasm",
    required_features: ["shader-f16"],
  }],
};
const engine = await webllm.CreateMLCEngine("RedPajama-...", { appConfig });
```

见 [docs/deploy/webllm.rst:349-366](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/deploy/webllm.rst)。`required_features: ["shader-f16"]` 表示该模型依赖 WebGPU 的 fp16 扩展，浏览器不支持时会被拒绝加载。

**一个真实坑（工程经验）**：Adreno GPU 上，权重布局带 `_1` 后缀的模型会在 prefill 阶段造成约 20–50 秒的系统 UI 卡顿，`_0` 布局则无此问题，临时缓解是改用 `_0`。见 [docs/deploy/android.rst:360-367](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/deploy/android.rst)。`_0`/`_1` 对应权重是否做过一次转置（即 u5-l2 讲的 `linear_weight_layout`）。

#### 4.1.4 代码实践

- **实践目标**：建立一个「同一模型、多端产物」的直觉。
- **操作步骤**：
  1. 打开 [android/MLCChat/mlc-package-config.json](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/android/MLCChat/mlc-package-config.json)，挑一个模型（如 `Llama-3.2-3B-Instruct-q4f16_0-MLC`）。
  2. 在 [docs/deploy/webllm.rst](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/deploy/webllm.rst) 找到「编译 WebGPU 模型库」的命令（约 288–296 行），观察 `--device webgpu` 与产物 `.wasm`。
  3. 在 [docs/deploy/ios.rst](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/deploy/ios.rst) 找到 iOS 产物清单（约 85–95 行），列出五个 `.a` 库。
- **需要观察的现象**：三个平台用的是**同一个 HF 权重仓**（`HF://mlc-ai/...`），但模型库**后缀不同**（Android/iOS 用 `.tar`/`.a`，Web 用 `.wasm`）。
- **预期结果**：你能用一句话说出「权重共享、库分平台、外壳换语言」。
- 本实践为纯源码阅读型，无需运行；如需本地验证构建，参考各 `.rst` 的「Step 1 安装依赖」章节（**待本地验证**：完整 Android/iOS 构建需真机与各平台工具链）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 WebLLM 的 `ModelRecord` 需要 `required_features: ["shader-f16"]`，而 Android/iOS 不需要类似字段？
> **答案**：WebGPU 的能力集（feature）由浏览器与显卡动态决定，不同设备差异大，必须在加载前声明依赖并校验；Android/iOS 的 GPU 能力在编译期（NDK/Metal toolchain）就确定了，`--device` 已经把能力烘进模型库，运行期无需再声明。

**练习 2**：`iphone` 与 `macabi` 两种设备在打包时复用了同一个脚本，它们的区别是什么？
> **答案**：两者共用 `ios/prepare_libs.sh`，`macabi` 额外传 `--catalyst`（并可指定 `--deployment-target`、`--arch`）来构建 Mac Catalyst（在 Apple Silicon Mac 上跑「为 iPad 设计」的应用）的产物；普通 `iphone` 走默认 iOS 构建。

---

### 4.2 `package` 打包流水线

#### 4.2.1 概念说明

`mlc_llm package` 是移动端部署的「一键打包」命令：读一份 `mlc-package-config.json`，产出 `dist/` 下可直接接入 Android Studio / Xcode 的库与配置。它把 u2-l3 讲的「构建/校验/binding」三步串成一个编排函数。

它遵循项目一贯的「cli 薄壳 → interface 实现」两层结构（见 u2-l1）：命令行层只解析 argv 并校验路径，真正的编排逻辑在接口层。

#### 4.2.2 核心流程

接口层 `package()` 的执行顺序（伪代码）：

```
读 mlc-package-config.json
校验 device ∈ {iphone, macabi, android}
build_model_library(...):            # 逐模型
    对每个 model_entry:
        下载模型(若 HF://)
        若未指定 model_lib 路径 → jit.jit() 编译该平台的模型库
        处理 bundle_weight(把权重拷进 bundle/)
    写出 dist/bundle/mlc-app-config.json
validate_model_lib(...):             # 合并 + 校验
    把所有模型库 .tar 合并成一个静态库(libmodel_android.a / libmodel_iphone.a)
    扫描静态库符号，确认每个 model_lib 的 ___tvm_ffi__library_bin 符号存在
按 device 分发:
    android → build_android_binding()  (调 android/mlc4j/prepare_libs.py)
    iphone  → build_iphone_binding()   (调 ios/prepare_libs.sh)
    macabi  → build_macabi_binding()   (调 ios/prepare_libs.sh --catalyst)
```

关键点：① 模型库的「按需 JIT」让 `package` 不强依赖你手动 `compile`；② 多个模型库最终**合并进一个静态库**，App 只链接这一个；③ 校验靠 **TVM FFI 的全局符号命名约定**（`___tvm_ffi__library_bin` 后缀）来确认某个模型库确实被打进去了。

#### 4.2.3 源码精读

**命令行层**只做参数解析与路径校验，核心是把三个参数转交给接口层：

```python
package(
    package_config_path=parsed.package_config,
    mlc_llm_source_dir=parsed.mlc_llm_source_dir,
    output=parsed.output,
)
```

见 [python/mlc_llm/cli/package.py:64-68](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/package.py)。`--mlc-llm-source-dir` 会被写进环境变量 `MLC_LLM_SOURCE_DIR`（见同文件 26-28 行），因为下游的 binding 构建（`prepare_libs.py` / `prepare_libs.sh`）需要找到 mlc-llm 源码树。

**接口层入口**读配置、校验 device、依次调用三个阶段：

```python
SUPPORTED_DEVICES = ["iphone", "macabi", "android"]
...
model_lib_path_for_prepare_libs = build_model_library(...)
validate_model_lib(...)
if device == "android":
    build_android_binding(mlc_llm_source_dir, output)
elif device == "iphone":
    build_iphone_binding(mlc_llm_source_dir, output)
elif device == "macabi":
    build_macabi_binding(mlc_llm_source_dir, output)
```

见 [python/mlc_llm/interface/package.py:18](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/package.py)（`SUPPORTED_DEVICES`）与 [python/mlc_llm/interface/package.py:380-400](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/package.py)（编排与分发）。这就是 device 的唯一合法取值表与分发点。

**模型库的按需 JIT**——当配置里没给出预编译库路径时，调用 `jit.jit()` 即时编译（与 u1-l4 讲的 JIT 兜底是同一套）：

```python
if model_lib_path is None:
    ...
    model_lib_path, model_lib = dataclasses.astuple(
        jit.jit(
            model_path=model_path,
            overrides=overrides,
            device=device,
            system_lib_prefix=model_lib,
            skip_log_jit_policy=True,
        )
    )
```

见 [python/mlc_llm/interface/package.py:82-104](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/package.py)。注意 `device` 直接透传给 JIT——它最终变成 `compile --device <device>`，决定了产物是 Android `.tar` 还是 iOS `.tar`。`system_lib_prefix` 就是配置里的 `model_lib`，决定库在静态库里的符号前缀。

**合并与符号校验**是 `validate_model_lib` 的核心：它把所有 `.tar` 合并成一个静态库，再扫描符号表，确认每个 `model_lib` 对应的 `___tvm_ffi__library_bin` 符号存在：

```python
suffix = "___tvm_ffi__library_bin"
for name, _ in global_symbol_map.items():
    if name.endswith(suffix):
        model_lib = name[: -len(suffix)]
        if model_lib.startswith("_"):
            model_lib = model_lib[1:]
        libs.append(model_lib)
```

见 [python/mlc_llm/interface/package.py:199-210](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/package.py)。这里的 `___tvm_ffi__library_bin` 正是 u9-l4 讲的「编译期↔运行期名字符串契约」在打包阶段的体现：每个模型库注册自己时都用这个固定后缀，打包器据此判断「我要的库在不在」。Android 用 `tvm.support.ndk`、iOS 用 `tvm.support.cc` 作为静态库工具（见 173-176 行）。

**Android binding 构建**把合并好的静态库搬进 mlc4j 工程并调 `prepare_libs.py`：

```python
def build_android_binding(mlc_llm_source_dir, output):
    mlc4j_path = mlc_llm_source_dir / "android" / "mlc4j"
    ...
    subprocess.run([sys.executable, mlc4j_path / "prepare_libs.py"], check=True, env=os.environ)
    ...
    # 把 mlc-app-config.json 搬到 src/main/assets/
```

见 [python/mlc_llm/interface/package.py:265-306](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/package.py)。iOS 与 macabi 的 binding 构建见 [309-323](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/package.py) 与 [326-347](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/package.py)，两者都调 `ios/prepare_libs.sh`，macabi 多传 `--catalyst`。

#### 4.2.4 代码实践

- **实践目标**：读懂 `package` 如何从「配置里的模型名」变成「静态库里的符号」。
- **操作步骤**：
  1. 读 [interface/package.py:51-104](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/package.py) 的循环，追踪 `model_lib`（字符串）→ `jit.jit` → `model_lib_path`（路径）→ 写进 `model_lib_path_for_prepare_libs` 字典的过程。
  2. 读 [interface/package.py:224-256](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/package.py)，理解 `model_lib.replace("-", "_")` 的作用——为什么配置里的 `gpt_neox_q4f16_1` 要把连字符换成下划线再拼符号？
- **需要观察的现象**：`model_lib` 这个字符串贯穿「JIT 的 `system_lib_prefix` → 静态库符号前缀 → App 配置里的 `model_lib` 字段」三处，是同一个值。
- **预期结果**：你能解释「连字符在 C 符号名里非法，所以 `-` 必须变 `_`」。
- 本实践为源码阅读型。若要真实运行 `mlc_llm package`，需配置 NDK/JDK/Rust 并准备一台 Android 真机（**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：`package` 命令默认不需要你先跑 `compile`，它是怎么得到模型库的？
> **答案**：接口层在 `model_lib_path_for_prepare_libs` 未提供对应路径时，调用 `jit.jit()` 即时编译；JIT 按配置内容算缓存键命中即复用、否则子进程跑标准 `compile`，产物路径回填进字典（见 4.2.3 的 JIT 片段）。

**练习 2**：为什么要把多个模型的库合并成 `libmodel_android.a` 一个静态库，而不是每个模型一个 `.so`？
> **答案**：移动 App 希望只链接一个库、按 `model_lib` 字符串在运行期按需查表加载；合并成一个静态库既简化了 gradle/Xcode 链接配置，又让 `___tvm_ffi__library_bin` 符号校验能在「一个符号表」里一次性完成。

**练习 3**：想强制 `package` 重新编译所有模型库（不用缓存），该怎么做？
> **答案**：设置环境变量 `MLC_JIT_POLICY=REDO` 再跑 `mlc_llm package`（见 [docs/deploy/android.rst:182-191](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/deploy/android.rst)，与 u1-l4 的 JIT 策略一致）。

---

### 4.3 bench 基准测试入口

#### 4.3.1 概念说明

`python/mlc_llm/bench/` 是 MLC LLM 自带的**服务端基准测试套件**，用来量化引擎在真实负载下的吞吐与延迟（TTFT、每秒 token 数等）。它通过 HTTP 访问一个「推理后端」（既可以是 MLC 自己的 `serve`，也可以是 vLLM、SGLang、TensorRT-LLM 等竞品，便于横向对比），按某条数据集生成请求、按某种并发模式发送、汇总指标。

它与 u9 的引擎、u11 的 REST 服务器是「测量者 vs 被测者」的关系：bench 只是一个客户端 + 编排器，本身不实现推理。

入口是 `bench/__main__.py`，运行方式为 `python -m mlc_llm.bench ...`（模块含 `__init__.py`，`__main__.py` 即模块入口）。

#### 4.3.2 核心流程

`main()` 的执行顺序（伪代码）：

```
若给了 --mlc-model-lib:
    用 PopenServer 拉起一个本地 mlc_llm serve (mode="server")
用 transformers 加载 tokenizer (--tokenizer)
create_dataset(...)       # 按 --dataset 选数据集类
create_pipelines(...)     # 按 --num-concurrent-requests / --request-rate 构造并发调度管线
对每条 pipeline:
    run_pipeline(): 生成请求记录 → 跑管线 → MetricAnalyzer → 汇总指标
    pretty_print_report()
尝试拉取服务端 metrics (/debug/dump_engine_metrics)
把所有报告转 DataFrame，写 --output CSV
```

两种并发模型：
- **固定并发**（`--num-concurrent-requests N`）：始终保持 N 个在飞请求，适合压满吞吐。
- **泊松到达**（`--request-rate R`）：每秒发 R 个新请求，适合模拟真实流量、测延迟曲线。
- 两者都不给时，进入**回放模式**，按数据集自带的时间戳重放（`--replay-timestamp-scale`）。

#### 4.3.3 源码精读

**总入口与参数**——`main()` 接收一个已解析的 argparse Namespace，可选地拉起本地 server 再跑流水线：

```python
def main(args):
    mlc_server = None
    if args.mlc_model_lib:
        mlc_server = _launch_mlc_server(args)
    ...
    def _main():
        tokenizer = AutoTokenizer.from_pretrained(args.tokenizer)
        dataset = create_dataset(args, tokenizer)
        f_create_api_endpoint = functools.partial(create_api_endpoint, args)
        pipelines = create_pipelines(args, f_create_api_endpoint, dataset)
        ...
```

见 [python/mlc_llm/bench/__main__.py:129-141](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/bench/__main__.py)。

**自动拉起 MLC server**——传 `--mlc-model-lib` 时，bench 会自己用 `PopenServer` 起一个子进程 server，免去手动开服：

```python
def _launch_mlc_server(args):
    return mlc_llm.serve.PopenServer(
        model=args.tokenizer, mode="server", model_lib=args.mlc_model_lib,
        host=args.host, port=args.port, engine_config=args.mlc_engine_config,
        ...
    )
```

见 [python/mlc_llm/bench/__main__.py:76-85](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/bench/__main__.py)。注意 `mode="server"`——正是 u2-l3/u11-l2 讲的 REST 服务模式；`--mlc-engine-config` 把 u11-l3 的 `EngineConfig` 覆盖透传进去（见同文件 56-73 行 `_parse_mlc_engine_config`）。

**支持的后端与数据集**是两个枚举常量，直接决定 `--api-endpoint` 与 `--dataset` 的合法取值：

```python
# api_endpoint.py
SUPPORTED_BACKENDS = ["openai", "openai-chat", "mlc", "sglang", "tensorrt-llm", "vllm"]
# dataset.py
SUPPORTED_DATASET = ["sharegpt", "llmperf", "json-mode-eval", "loogle", "react", "wildchat", "azure-llm-inference"]
```

见 [python/mlc_llm/bench/api_endpoint.py:426-433](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/bench/api_endpoint.py) 与 [python/mlc_llm/bench/dataset.py:800-808](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/bench/dataset.py)。其中 `openai` / `mlc` / `sglang` 走 `/v1/completions`，`openai-chat` 走 `/v1/chat/completions`（见下）。

**两条端点路径**——bench 客户端按后端类型打到不同的 OpenAI 端点：

```python
# OpenAIChatEndPoint (chat 模式)
self.url = f"http://{host}:{port}/v1/chat/completions"
# OpenAIEndPoint (纯 completion 模式)
self.url = f"http://{host}:{port}/v1/completions"
```

见 [python/mlc_llm/bench/api_endpoint.py:52](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/bench/api_endpoint.py) 与 [python/mlc_llm/bench/api_endpoint.py:197](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/bench/api_endpoint.py)。这两个端点正是 u11-l2 的 REST 服务器对外暴露的 `/v1/chat/completions` 与 `/v1/completions`——bench 与 serve 通过 OpenAI 兼容协议对接。

**关键必填参数**（节选）：

| 参数 | 作用 |
|------|------|
| `--tokenizer` | tokenizer 目录（必填） |
| `--num-gpus` | server 用的 GPU 数（必填，用于「每 GPU 吞吐」归一） |
| `--num-requests` | 请求总数（必填） |
| `--host` / `--port` | 后端地址（必填） |
| `--dataset` / `--dataset-path` | 数据集类型与文件 |
| `--num-concurrent-requests` / `--request-rate` | 两种并发模式二选一 |
| `--input-len` / `--output-len` | 控制请求长度（部分数据集用） |
| `--stream` | 流式（关掉则无 TTFT） |
| `--mlc-model-lib` | 自动拉起本地 MLC server |
| `--output` / `-o` | 结果 CSV 路径（默认 `mlc_benchmark.csv`） |

完整参数表见 [python/mlc_llm/bench/__main__.py:178-403](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/bench/__main__.py)。

#### 4.3.4 代码实践

- **实践目标**：在不真正发请求的前提下，摸清 bench 的「能力边界」。
- **操作步骤**：
  1. 在仓库根目录运行：
     ```bash
     python -m mlc_llm.bench --help
     ```
  2. 对照上面两个枚举常量，数一下 `--help` 里 `--dataset` 与 `--api-endpoint` 的可选值个数。
  3. 找出哪几个参数是 `required=True`（必填）。
- **需要观察的现象**：`--help` 会列出全部参数；`--dataset` 的 choices 应正好是 7 个，`--api-endpoint` 是 6 个。
- **预期结果**：你能写出一条「最小可跑」命令骨架（即便没有真模型也能拼出来），例如：
  ```bash
  python -m mlc_llm.bench \
    --tokenizer <HF-model-dir> --num-gpus 1 --num-requests 16 \
    --host 127.0.0.1 --port 8000 \
    --dataset sharegpt --dataset-path sharegpt.json \
    --num-concurrent-requests 8 -o out.csv
  ```
- **待本地验证**：真正跑通需先有一个运行中的推理后端（如 `mlc_llm serve`）和一份 ShareGPT 数据；本步骤只验证 `--help` 与参数枚举。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `--num-gpus` 是必填的？它并不影响请求发送。
> **答案**：它用于把总吞吐/总 token 数「归一化成每 GPU」的指标（见 `run_pipeline` 里 `args.num_requests * args.num_gpus` 的 `per_gpu_workload` 逻辑，[__main__.py:104-107](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/bench/__main__.py)），便于跨不同卡数的服务器横向比较。

**练习 2**：`--stream` 默认为 True，关掉它会损失什么指标？
> **答案**：会损失 TTFT（time-to-first-token）。因为 TTFT 依赖流式响应里「第一个 token 到达」的时刻，非流式只能拿到总时长（见 [__main__.py:282-289](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/bench/__main__.py) 的 help 文本）。

---

### 4.4 tests 测试体系

#### 4.4.1 概念说明

MLC LLM 的质量保障分两半：**Python 测试**（`tests/python/`，主力）与 **C++ 单元测试**（`tests/cpp/`，少量）。一个关键设计选择写在 [tests/README.md](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/tests/README.md)：**C++ 的功能大多经 TVM FFI 暴露给 Python，再用 pytest 测**——这与 u9/u11 讲的「C++ 引擎经 JSON FFI / PackedFunc 跨界」一脉相承。所以你会看到大量 `serve/`、`json_ffi/` 测试其实是在 Python 里驱动 C++ 引擎。

测试按子系统分目录组织，并用 pytest 的 marker 做分类（决定是否需要 GPU、是否需要模型）。

#### 4.4.2 核心流程

- **运行 Python 测试**：在仓库根目录 `pytest tests/python/...`；按 marker 过滤如 `pytest -m unittest`（不需 GPU）或 `pytest -m endpoint`（端到端）。
- **构建 C++ 测试**：CMake 配置时加 `-DBUILD_CPP_TEST=ON`，会拉起 googletest、收集 `tests/cpp/*unittest.cc`、编译出 `mlc_llm_cpp_tests` 可执行文件。

#### 4.4.3 源码精读

**测试总览**点明了「以 pytest 为主、C++ 经 FFI 暴露给 Python 测」的策略：

> We primarily relies on pytest to test our engine. Most of the unit functionalities in C++ can be exposed via TVM FFI, and tested through python environment.

见 [tests/README.md:1-9](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/tests/README.md)。

**pytest marker 分类**——`conftest.py` 在 `pytest_configure` 里注册了五类 marker：

```python
config.addinivalue_line("markers", "unittest: unittests for modules, do not require GPU, usually run fast")
config.addinivalue_line("markers", "op_correctness: unittest for op corectness, requires GPU")
config.addinivalue_line("markers", "engine: testing engine feature functionalities, requires model and GPU, ...")
config.addinivalue_line("markers", "endpoint: sending requests to a global endpoint fixture ...")
config.addinivalue_line("markers", "uncategorized: this test is not yet categorized, ...")
```

见 [tests/python/conftest.py:17-42](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/tests/python/conftest.py)。每个测试文件用 `pytestmark = [pytest.mark.category_name]` 声明类别，CI 据此决定在哪种环境（有/无 GPU）跑。

**C++ 单元测试的构建开关与规则**：

```cmake
option(BUILD_CPP_TEST "Build cpp unittests" OFF)
...
if(BUILD_CPP_TEST)
  message(STATUS "Building cpp unittests")
  add_subdirectory(3rdparty/googletest)
  file(GLOB_RECURSE MLC_LLM_TEST_SRCS ${PROJECT_SOURCE_DIR}/tests/cpp/*unittest.cc)
  add_executable(mlc_llm_cpp_tests ${MLC_LLM_TEST_SRCS})
  ...
  target_link_libraries(mlc_llm_cpp_tests PUBLIC mlc_llm gtest gtest_main)
endif(BUILD_CPP_TEST)
```

见 [CMakeLists.txt:43](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/CMakeLists.txt)（开关）与 [CMakeLists.txt:124-136](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/CMakeLists.txt)（构建规则）。规则按文件名约定 `*unittest.cc` 收集——目前 `tests/cpp/` 下只有 `conv_template_unittest.cc` 一个，所以「主力在 Python」的说法有据可依。

**Python 测试目录布局**（按子系统分目录，每个目录一组 `test_*.py`）。基于仓库当前 HEAD 的实际文件统计如下：

| 子系统目录 | `test_*.py` 数量 | 代表文件 / 覆盖内容 |
|------------|------------------|---------------------|
| `compiler_pass/` | 1 | 融合 pass（如 FT 反量化 matmul 融合），承接 u7/u8 |
| `model/` | 9 | 各架构模型（llama、mistral、gemma3、gpt2、phi、gptNeox、qwen3-embedding）+ KV cache + 量化 |
| `loader/` | 2 | HuggingFace / AWQ 权重加载，承接 u4 |
| `quantization/` | 2 | group-quant、awq 量化，承接 u5 |
| `serve/` | 13 | 异步/同步引擎、前缀缓存、推测解码、RNN、grammar、image、radix tree、embedding |
| `serve/server/` | 4 | REST 端点（chat/completions/embeddings/function-call/image），承接 u11-l2 |
| `json_ffi/` | 3 | JSON FFI 引擎（含 image、mock），承接 u11-l1 |
| `op/` | 6 | 算子正确性（tree attn、fp8 block matmul、top-p pivot、mrope 等） |
| `support/` | 5 | auto_config / auto_weight / auto_target / convert_weight CLI / LoRA 合并 |
| `conversation_template/` | 2 | 对话协议与 llama 模板，承接 u6 |
| `router/` | 1 | 微服务路由器，承接 u12-l2 |
| `integration/` | 1 | 端到端模型编译（`test_model_compile.py`） |
| `tokenizers/` | 1 | tokenizer 流式输出 |

可见 `tests/python/` 几乎对前面 11 个单元讲的每个子系统都有对应覆盖——尤其是体量最大的 `serve/`（13 + server 4）和 `model/`（9），正对应「引擎」与「模型定义」这两个最核心的模块。

#### 4.4.4 代码实践（即本讲指定实践任务）

- **实践目标**：亲手把 `tests/python` 按子系统分类统计，建立「测试与源码一一对应」的地图。
- **操作步骤**：
  1. 在仓库根目录用 Glob 列出所有测试文件：
     ```bash
     # 等价于：在文件树里 glob "tests/python/**/test_*.py"
     ```
     或直接 `ls tests/python/*/test_*.py tests/python/serve/server/test_*.py`。
  2. 按题目要求的六大类（compiler_pass / model / loader / quantization / serve / json_ffi）计数，补上你额外发现的目录（op、support、router 等）。
  3. 再跑一次 bench 的帮助，把两件事放一起：
     ```bash
     python -m mlc_llm.bench --help
     ```
- **需要观察的现象**：统计结果应与上表一致（compiler_pass=1、model=9、loader=2、quantization=2、serve（含 server）=17、json_ffi=3）；`bench --help` 列出的 `--dataset`/`--api-endpoint` 可选值与 4.3 的两个枚举常量一致。
- **预期结果**：你能指着 `tests/python/serve/test_serve_engine_spec.py` 说出「它对应 u10-l4 推测解码」，指着 `tests/python/loader/test_awq.py` 说出「它对应 u4-l2 的 AWQ 映射」。
- 本实践为纯文件统计 + `--help`，不依赖 GPU，可直接运行。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `tests/cpp/` 只有一个文件，而 `tests/python/` 有几十个？
> **答案**：见 [tests/README.md:3-5](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/tests/README.md)——C++ 功能大多经 TVM FFI 暴露给 Python，团队选择在 Python 侧用 pytest 测，C++ 侧只保留少量（如对话模板）必须原地测的单元测试。

**练习 2**：一个不需要 GPU 的纯逻辑测试，应该打哪个 marker？一个需要真模型 + GPU 的端到端测试呢？
> **答案**：前者打 `unittest`（do not require GPU, usually run fast）；后者打 `endpoint`（sending requests to a global endpoint fixture）或 `engine`（requires model and GPU）。见 [conftest.py:17-42](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/tests/python/conftest.py)。

**练习 3**：如何只构建并运行 C++ 单元测试？
> **答案**：CMake 配置时加 `-DBUILD_CPP_TEST=ON`（默认 OFF），它会把 `tests/cpp/*unittest.cc` 编译进 `mlc_llm_cpp_tests` 可执行文件并链接 gtest，直接运行该可执行文件即可。见 [CMakeLists.txt:124-136](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/CMakeLists.txt)。

## 5. 综合实践

把本讲四个模块串起来，完成一份**「多端部署 + 质量保障」速查卡**：

1. **部署侧**（承接 4.1 + 4.2）：
   - 选一个已在 HF 上的 MLC 模型（如 `HF://mlc-ai/Llama-3.2-3B-Instruct-q4f16_0-MLC`）。
   - 写一份最小 `mlc-package-config.json`（`device` 分别试 `android` 与 `iphone`），对照 [interface/package.py:51-104](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/package.py) 画出「配置字段 → JIT 编译 → 静态库符号」的数据流。
   - 为 Web 端写一份 `ModelRecord`，对照 [docs/deploy/webllm.rst](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/deploy/webllm.rst) 指出三端各自的模型库后缀（`.tar/.a` vs `.wasm`）。
2. **质量侧**（承接 4.3 + 4.4）：
   - 用 4.4.4 的方法统计 `tests/python`，挑出与 u10（KV/前缀缓存/采样/推测解码）对应的测试文件各一个。
   - 写一条「最小可跑」的 bench 命令骨架（不必真跑），并标注其中哪个参数控制并发模式、哪个参数能让 bench 自动拉起 MLC server。
3. **产出**：一张表，左列写「部署端 / 测试子系统 / bench 维度」，右列写「对应源码文件或命令」。

> 这个任务不要求你真机构建或发请求，重在把「同一份模型如何分发到多端」与「项目如何自测自证」两条线打通。如需真机验证，参考各 `docs/deploy/*.rst` 的前置依赖章节（**待本地验证**）。

## 6. 本讲小结

- **多端 = 同一产物换外壳**：权重跨平台共享、模型库按平台专用（Android/iOS 用 `.tar/.a`、Web 用 `.wasm`），外壳分别套 Java(mlc4j)、Swift(MLCSwift)、TypeScript(WebLLM)，三端对外都是 OpenAI 兼容 API。
- **`mlc_llm package` 是移动端一键打包**：读 `mlc-package-config.json` → `build_model_library`（按需 JIT）→ `validate_model_lib`（合并成单静态库 + 按 `___tvm_ffi__library_bin` 符号校验）→ 按 `device` 分发到 `prepare_libs.py` / `prepare_libs.sh`。
- **`device` 的合法取值**是 `iphone / macabi / android`（`SUPPORTED_DEVICES`），`macabi` 复用 iOS 脚本加 `--catalyst`。
- **bench 是服务端基准测试客户端**：`python -m mlc_llm.bench`，支持 7 种数据集、6 种后端、固定并发/泊松到达/回放三种并发模式，`--mlc-model-lib` 可自动拉起本地 `serve`，结果落 CSV。
- **测试以 pytest 为主**：`tests/python/` 按子系统分目录（serve/model 最密集），用 5 类 marker（unittest/op_correctness/engine/endpoint/uncategorized）区分是否需要 GPU；C++ 单元测试少量，靠 `-DBUILD_CPP_TEST=ON` 构建，主力功能经 TVM FFI 在 Python 测。
- **贯穿全讲的命名约定**：`___tvm_ffi__library_bin` 符号后缀是编译期↔运行期↔打包期的三方契约，与 u9-l4 的「FunctionTable 按名字符串取函数」同源。

## 7. 下一步学习建议

- 本讲是手册最后一篇，建议**回头串联**：把 u12-l1（多 GPU）→ u12-l2（微服务）→ u12-l3（分离式）→ 本讲（多端 + 工程化）作为一个整体，画出「MLC LLM 从一个 HF 模型到任意端、任意规模部署」的全景图。
- 若想动手深入移动端：读 `android/mlc4j/prepare_libs.py` 与 `ios/prepare_libs.sh`，看 binding 是如何把 `libmodel_*.a` 接入 gradle/Xcode 的。
- 若想动手深入 Web：clone `mlc-ai/web-llm`，对照本讲的 `ModelRecord` 看 `prebuiltAppConfig.model_list` 是如何注册的。
- 若想深入质量工程：挑一个 `tests/python/serve/test_serve_engine_*.py`，结合 u9/u10 的源码，理解 pytest 是如何经 FFI 驱动 C++ 引擎、断言 prefill/decode/verify 行为的；再用 bench 对比一次「开/关推测解码」的吞吐差异。
- 至此整套手册（U1 入门 → U7 编译 → U9 引擎 → U12 扩展部署）已闭环，可作为后续二次开发与贡献代码的地图。
