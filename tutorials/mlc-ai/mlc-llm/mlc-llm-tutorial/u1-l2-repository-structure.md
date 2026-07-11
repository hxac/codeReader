# 仓库目录结构与多语言布局

## 1. 本讲目标

上一讲（u1-l1）我们建立了全局心智模型：MLC LLM 是一个「ML 编译器 + 高性能部署引擎」，既能把模型优化成硬件专用代码，又能驱动推理并提供 OpenAI 兼容 API。但这些都还是「概念」层面的认知。

本讲要回答一个更落地的问题：**这些能力分别住在仓库的哪个目录里？**

读完本讲，你应当能够：

1. 看着顶层目录名，立刻说出它属于「编译」「引擎」「部署」「文档」哪一类职责。
2. 清楚地区分**编译期代码（Python）**和**运行期代码（C++）**的边界——为什么同一个项目里既有 Python 又有 C++，它们各自负责什么。
3. 认识 `3rdparty/` 下的 `tvm`、`tokenizers-cpp`、`xgrammar` 三个第三方依赖，理解 MLC LLM 站在谁的肩膀上。
4. 自己画出一张「顶层目录 → 职责」的思维导图，并能定位到 C++ 推理引擎的真正入口。

本讲是**纯阅读理解型**讲义，不写代码，重在建立「目录地图」。有了这张地图，后续每一讲你都能随时回来核对：我们要讲的那个机制，到底在仓库的哪一层。

## 2. 前置知识

### 2.1 编译期 vs 运行期

这是理解本讲最重要的一对概念。一个 LLM 要在硬件上跑起来，通常分两个阶段：

- **编译期（compile time）**：把「抽象的模型定义」翻译成「某块硬件（如某款 GPU）能高效执行的机器码」。这一步很慢，但只需做一次，结果可以缓存复用。在 MLC LLM 里，这一步主要由 **Python** 完成（借助 TVM）。
- **运行期（runtime）**：拿到编译产物，真正接收用户的 prompt、做前向推理、采样生成 token、把结果返回给用户。这一步必须很快，所以核心由 **C++** 实现。

> 直觉类比：编译期像「把菜谱写成这台特定烤箱的专用指令」，运行期像「按指令真正把菜烤出来端给客人」。MLC LLM 把两者放在同一个仓库里，但用不同语言、不同目录组织。

### 2.2 子模块（git submodule）

大型项目常把别人的代码「挂载」到自己仓库里，而不是复制粘贴。Git 通过 **submodule** 机制实现：主仓库只记录一个指针（某个 commit），实际代码单独存在另一个仓库里。

本仓库的 `3rdparty/` 下全是 submodule——它们是独立的项目（如 TVM），被 MLC LLM 当作依赖引入。克隆本仓库时需要 `git submodule update --init --recursive` 才能拿到这些目录里的真实代码。

### 2.3 Python 包的「入口点」

当你能在命令行敲 `mlc_llm compile ...` 时，背后是 Python 打包工具注册了一个 **console_scripts 入口点**：它告诉系统「`mlc_llm` 这个命令 = 调用 `mlc_llm.__main__` 模块里的 `main` 函数」。理解这一点，你就明白为什么 `python/mlc_llm/__main__.py` 是一切 CLI 命令的总入口。

## 3. 本讲源码地图

本讲聚焦于「仓库骨架」，涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| `CMakeLists.txt` | C++ 侧的总构建脚本，定义了如何编译 `cpp/` 下的源码，并链接 `3rdparty/` 的依赖 |
| `pyproject.toml` | Python 侧的现代打包配置，声明依赖、构建后端（scikit-build-core）、把 `python/mlc_llm` 打成 wheel |
| `python/setup.py` | Python 侧的传统打包脚本，注册 `mlc_llm` 命令行入口点 |
| `python/mlc_llm/__init__.py` | Python 包的导出层，决定 `import mlc_llm` 后能直接用到什么 |
| `.gitmodules` | 声明所有 git submodule（第三方依赖）的来源 |
| `docs/index.rst` | 文档站点首页，从它的目录树能反推出项目的功能分区 |

> 说明：这些都不是「逻辑实现」文件，而是「项目组织」文件。读它们，看的是项目如何自我组织。

## 4. 核心概念与源码讲解

### 4.1 顶层目录职责映射

#### 4.1.1 概念说明

MLC LLM 是一个**多语言、多平台**的大型项目：同一个仓库里同时有 Python（编译器与 CLI）、C++（推理引擎）、Kotlin/Swift（移动端 App）、JavaScript/TypeScript（REST 客户端）、Rust（由 tokenizers-cpp 间接引入）。如果一开始就把每个文件都看一遍，必然迷失。

正确的做法是先做**顶层目录职责映射**：给每个目录贴一个标签，标签只有四类——

- **编译（Compile）**：把模型变成硬件代码的 Python 代码。
- **引擎（Engine）**：运行期驱动推理的 C++ 代码。
- **部署（Deploy）**：各平台（Android/iOS/Web）的 App 与打包脚本。
- **文档（Docs）**：用户文档与教程。

贴完标签后，整张地图就清晰了。

#### 4.1.2 核心流程

先用一张「目录 → 职责」映射表建立全景：

| 顶层目录 | 主要语言 | 职责标签 | 一句话说明 |
|----------|----------|----------|-----------|
| `python/` | Python | 编译 + CLI + 引擎封装 | 编译器、量化、模型定义、CLI、以及用 Python 包住 C++ 引擎的 `MLCEngine` |
| `cpp/` | C++ | 引擎 | 高性能推理引擎核心（`serve/`）、JSON FFI 桥（`json_ffi/`）、多 GPU 加载（`multi_gpu/`） |
| `3rdparty/` | 多语言 | 依赖 | TVM、tokenizers-cpp、xgrammar 等 submodule |
| `android/` | Kotlin/C++ | 部署 | Android App（`MLCChat`、`MLCEngineExample`）与 Java 绑定 `mlc4j` |
| `ios/` | Swift/C++ | 部署 | iOS App 与 Swift 包 `MLCSwift` |
| `web/` | C++→WASM | 部署 | WebGPU/WASM 运行时胶水代码 |
| `docs/` | reStructuredText | 文档 | Sphinx 文档站，对应 https://llm.mlc.ai |
| `examples/` | Python/JS/TS | 文档+编译 | 各入口的最小可运行示例 |
| `tests/` | Python/C++ | 工程 | 单元测试与集成测试，覆盖 compiler_pass / model / loader / quantization / serve |
| `ci/` `scripts/` `cmake/` | Shell/Python | 工程 | CI 流水线、构建脚本 |
| `site/` | HTML/SCSS | 文档 | 项目官网静态站点 |

记住这张表的几个关键判断点：

1. **看到 `python/` 不等于「全是编译器」**——它既装编译器，也装 CLI，还装了用 Python 包住 C++ 的引擎封装（`serve/engine.py`）。
2. **`cpp/serve/` 才是推理引擎的心脏**，而 `cpp/json_ffi/` 是 Python↔C++ 的 JSON 桥。
3. **`3rdparty/` 的体积最大**（尤其是 `tvm`），但它是别人的代码，学习时一般不深读。

#### 4.1.3 源码精读

`CMakeLists.txt` 是确认「C++ 侧到底编译了哪些源码」的权威文件。它用一行 glob 把整个 `cpp/` 目录下的 `.cc` 文件全部纳入编译：

[CMakeLists.txt:74-78](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/CMakeLists.txt#L74-L78) —— 用 `tvm_file_glob(GLOB_RECURSE ...)` 递归收集 `cpp/*.cc`，说明 **C++ 引擎的所有实现都在 `cpp/` 这一棵子树里**，没有散落到别处。

接着它定义了三个产物库：

[CMakeLists.txt:96-97](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/CMakeLists.txt#L96-L97) —— `mlc_llm`（动态库）与 `mlc_llm_static`（静态库）共享同一批对象文件 `mlc_llm_objs`。

[CMakeLists.txt:143-146](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/CMakeLists.txt#L143-L146) —— 额外的 `mlc_llm_module` 库，专门用于「编译出的模型库」场景（即 compile 流程的产物会被组装成这种可加载的 module）。

文档站首页 `docs/index.rst` 的目录树（toctree）则从「用户视角」反向印证了功能分区：

[docs/index.rst:24-67](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/index.rst#L24-L67) —— 文档分为 Get Started、Build and Deploy Apps、Compile Models、Dependency Installation、Microserving API 等板块。其中 **Compile Models** 板块对应 `python/` 的编译能力，**Build and Deploy Apps** 板块对应 `android/`、`ios/`、`web/` 等部署目录。

#### 4.1.4 代码实践

**实践目标**：亲手把顶层目录与职责标签对应起来，形成一张可复用的地图。

**操作步骤**：

1. 在仓库根目录运行 `git ls-files`，浏览输出的文件路径前缀。
2. 对照本讲 4.1.2 的映射表，逐个目录核对：你看到的文件名是否符合标签描述？
3. 特别检查两个 C++ 入口：
   - `git ls-files cpp/serve | head` —— 这是推理引擎心脏，应能看到 `engine.cc`、`threaded_engine.cc`、`model.cc` 等。
   - `git ls-files cpp/json_ffi | head` —— 这是 JSON FFI 桥，应能看到 `json_ffi_engine.cc`。

**需要观察的现象**：

- `python/` 下的子目录名（`cli/`、`interface/`、`model/`、`loader/`、`quantization/`、`compiler_pass/`、`serve/`、`protocol/`）几乎一对一对应了后续讲义要讲的子系统。
- `cpp/` 下的 `serve/` 又有 `engine_actions/`、`sampler/` 等子目录，说明引擎内部还有更细的分层。

**预期结果**：你能在不看本讲的情况下，指着任何一个顶层目录说出它的职责标签。

> 说明：本实践是源码阅读型，不修改任何文件。

#### 4.1.5 小练习与答案

**练习 1**：`tests/python/` 下按子目录分类，能反推出哪些子系统？  
**参考答案**：从 `compiler_pass/`、`model/`、`loader/`、`quantization/`、`serve/`、`json_ffi/`、`op/`、`tokenizers/`、`support/`、`conversation_template/`、`router/` 这些子目录名，可反推出 MLC LLM 的核心子系统正是这些——测试目录结构是项目结构的镜像。

**练习 2**：`site/` 和 `docs/` 都是「文档」，它们有何不同？  
**参考答案**：`docs/` 是给开发者看的技术文档（Sphinx + reStructuredText，构建后是 https://llm.mlc.ai/docs）；`site/` 是项目营销官网（Jekyll + Markdown/SCSS），侧重展示与介绍。两者用途不同，互不替代。

**练习 3**：为什么 `examples/` 既算「文档」又算「编译」相关？  
**参考答案**：`examples/python/sample_mlc_engine.py` 这类示例同时承担两个角色——对用户它是「怎么用」的活文档（文档属性），对引擎它是验证 Python↔C++ 链路可用的最小用例（编译/集成属性）。

### 4.2 Python 包与 C++ 库的边界

#### 4.2.1 概念说明

上一讲我们说 MLC LLM 通过 `MLCEngine` 对外提供 OpenAI 兼容 API，并且能从 Python 调用。但 `MLCEngine` 的「实现」其实在 C++。这就引出本节核心问题：

**Python 和 C++ 各自的边界在哪里？它们如何拼成一个整体？**

边界划法如下：

- **Python 侧（`python/mlc_llm/`）负责**：编译期全部逻辑（模型定义、量化、compiler pass、convert_weight、compile）、CLI 命令、以及运行期引擎的「薄封装」（`MLCEngine`/`AsyncMLCEngine` 只是把调用转成 JSON 字符串，丢给 C++）。
- **C++ 侧（`cpp/serve/`、`cpp/json_ffi/`）负责**：真正的高性能推理循环——请求调度、KV 缓存、采样、推测解码、分页缓存。

两者通过 **JSON FFI（Foreign Function Interface，外部函数接口）** 衔接：Python 把请求序列化成 JSON 字符串跨语言调进 C++，C++ 处理完再把结果（也是 JSON）推回来。

> 直觉类比：Python 是「前台接待 + 后厨调度」，C++ 是「后厨真正炒菜」。客人（用户）只跟前台打交道，但菜是 C++ 炒的。

#### 4.2.2 核心流程

一条贯穿性数据流，串起两边的边界：

```
用户 Python 代码
    │  调用 MLCEngine.chat(...)
    ▼
python/mlc_llm/serve/engine.py        ← Python「薄封装」
    │  把请求序列化为 JSON 字符串
    ▼
python/mlc_llm/json_ffi/engine.py     ← JSON FFI 调用层
    │  跨语言 FFI 调用
    ▼
cpp/json_ffi/json_ffi_engine.cc       ← C++ 接住 JSON
    │
    ▼
cpp/serve/threaded_engine.cc          ← 真正的推理引擎后台循环
    │  prefill / decode / sample / KV cache
    ▼
结果原路返回（C++ → JSON → Python → 用户）
```

关键判断点：

1. **编译期几乎不出 C++**——`model/`、`quantization/`、`compiler_pass/`、`loader/`、`interface/` 全在 Python 侧。TVM 这个 C++ 编译器虽然被调用，但它是 `3rdparty/` 的依赖，不属于本仓库代码。
2. **运行期的「快路径」几乎不出 Python**——真正耗算力的是 C++ 引擎，Python 只是发号施令。
3. **`python/mlc_llm/__init__.py` 决定了「导出边界」**——`import mlc_llm` 后你直接能用的东西，就在这个文件里声明。

#### 4.2.3 源码精读

先看 Python 包的导出层：

[python/mlc_llm/__init__.py:8-10](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/__init__.py#L8-L10) —— 这几行说明：`import mlc_llm` 后，`mlc_llm.AsyncMLCEngine` 和 `mlc_llm.MLCEngine` 是直接可用的，且它们来自 `.serve` 子模块。这就是「Python 对外暴露的引擎 API」的入口。

[python/mlc_llm/__init__.py:13-20](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/__init__.py#L13-L20) —— 这里注册了一个全局函数 `runtime.disco.create_socket_session_local_workers`，用于多 GPU（disco 分布式会话）场景。它揭示了 Python 侧也会反过来给 C++/TVM 运行时「注入」回调——边界是双向的。

再看 CLI 入口点的注册：

[python/setup.py:117-119](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/setup.py#L117-L119) —— `console_scripts` 把命令行命令 `mlc_llm` 绑定到 `mlc_llm.__main__:main`。这就是为什么敲 `mlc_llm compile` 之类的命令能进入 Python CLI（下一讲 u2-l1 会展开 CLI 分发机制）。

Python 库如何「携带」C++ 编译产物，看打包配置：

[pyproject.toml:83](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/pyproject.toml#L83) —— `wheel.packages = ["python/mlc_llm"]` 声明被打包的 Python 源码目录。

[pyproject.toml:64-66](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/pyproject.toml#L64-L66) —— 构建后端是 `scikit-build-core`，它的特殊之处在于能**在装 Python 包的同时调 CMake 编译 C++**。

[pyproject.toml:80](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/pyproject.toml#L80) —— `cmake.args = ["-DMLC_LLM_BUILD_PYTHON_MODULE=ON"]` 打开 CMakeLists.txt 里的 Python 模块安装开关。

而 C++ 侧的库文件如何被 Python 找到，由 `libinfo.py` 负责：

[python/mlc_llm/libinfo.py:18-37](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/libinfo.py#L18-L37) —— `get_dll_directories()` 在多个候选位置搜索 `libmlc_llm.so`（或 `.dll`/`.dylib`），包括包自身目录、`build/`、`MLC_LIBRARY_PATH` 环境变量等。这就是 Python↔C++ 边界上的「胶水」：Python 运行时靠它定位并加载 C++ 编译出的动态库。

#### 4.2.4 代码实践

**实践目标**：亲手验证「Python 包里其实藏着 C++ 动态库」，从而体会两边边界。

**操作步骤**：

1. 在已安装 `mlc_llm` 的环境里运行：
   ```
   python -c "import mlc_llm; print(mlc_llm.__path__)"
   ```
   记下输出的包目录路径。
2. 进入该目录，用 `ls` 查看是否存在形如 `libmlc_llm.so`（Linux）、`libmlc_llm.dylib`（macOS）或 `mlc_llm.dll`（Windows）的文件。
3. 对照本讲 4.2.3 的 `libinfo.py` 片段，理解这些 `.so` 正是 `CMakeLists.txt` 编译 `cpp/*.cc` 产出的 `mlc_llm` 动态库，被打包进了 Python wheel。

**需要观察的现象**：

- Python 包目录里除了 `.py` 文件，确实存在二进制动态库。
- 这些库的命名与 `libinfo.py` 中 `find_lib_path("mlc_llm")` 的查找逻辑完全对应。

**预期结果**：你直观看到「一个 Python 包 = Python 源码 + C++ 编译产物」的混合形态，理解为何 `pip install mlc_llm` 之后既能跑编译器、又能直接推理。

> 待本地验证：若环境未正确编译 C++，可能找不到动态库，此时 `import mlc_llm` 会因 `find_lib_path` 抛出 `Cannot find libraries` 错误（见 `libinfo.py` 第 63-70 行）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `python/mlc_llm/` 里既有编译器代码又有 `serve/`（引擎封装），这违反「单一职责」吗？  
**参考答案**：不违反。编译器和引擎封装虽同居一个 Python 包，但分属不同子目录（`compiler_pass/` vs `serve/`），且职责清晰：前者是编译期，后者是运行期的「薄封装」。放一起是为了让用户 `import mlc_llm` 一次拿到全部能力，简化分发。

**练习 2**：`MLCEngine` 是用 Python 实现的推理引擎吗？  
**参考答案**：不是。`MLCEngine`（在 `python/mlc_llm/serve/engine.py`）只是 Python 侧的封装，真正的推理逻辑在 `cpp/serve/` 的 C++ 引擎里。Python 这层主要负责把 OpenAI 风格的请求转成 JSON，经 `json_ffi` 调进 C++。

**练习 3**：`scikit-build-core` 在这个项目里起什么特殊作用？  
**参考答案**：它是一个能桥接 Python 打包与 CMake 构建的后端。普通 Python 包只装 `.py`，而 MLC LLM 需要在 `pip install` 时调 CMake 编译 `cpp/` 成动态库并塞进 wheel，这正是 `scikit-build-core`（配合 `MLC_LLM_BUILD_PYTHON_MODULE=ON`）完成的。

### 4.3 第三方依赖（TVM 等）

#### 4.3.1 概念说明

MLC LLM 不是从零造轮子。`3rdparty/` 目录下的几个 submodule 是它的「地基」。最重要的三个：

1. **TVM（`3rdparty/tvm`）**：Apache TVM 是一个成熟的机器学习编译器框架。MLC LLM 的「编译器」身份几乎完全建立在 TVM 之上——模型定义用的是 TVM 的 Relax nn，优化用的是 TVM 的 pass 机制，代码生成用的是 TVM 的 target 后端。可以说**没有 TVM 就没有 MLC LLM 的编译能力**。
2. **tokenizers-cpp（`3rdparty/tokenizers-cpp`）**：把 HuggingFace 的 Rust tokenizer 封装成 C++ 可调用的库。运行期把文本切成 token、把 token 还原成文本，靠的就是它。
3. **xgrammar（`3rdparty/xgrammar`）**：MLC 团队自研的「语法约束解码」库，用于让模型输出严格符合 JSON 等结构化格式（function calling 的底层依赖）。

此外还有：`googletest`（C++ 单元测试框架）、`stb`（单头图像加载库，多模态用）、`argparse`（C++ 命令行参数解析）。

> 直觉理解：`3rdparty/` 是「别人写好的、可独立升级的」积木。学习 MLC LLM 时，除非专门研究某个依赖，否则把它们当黑盒即可——重点是知道「哪个能力来自哪个依赖」。

#### 4.3.2 核心流程

依赖如何被「接入」主构建，分两步看：

**第一步：submodule 声明**。`.gitmodules` 记录每个第三方仓库的来源 URL 与挂载路径。

**第二步：CMake 链接**。`CMakeLists.txt` 把这些依赖的源码纳入编译、把产物链接进 `mlc_llm` 库。

判断这些依赖归属哪种能力，可用下表：

| 依赖 | 提供的能力 | 在 MLC LLM 中的角色 |
|------|-----------|---------------------|
| `tvm` | ML 编译器框架（Relax nn、pass、codegen） | 编译能力的地基，无处不在 |
| `tokenizers-cpp` | 分词 / 反分词 | 运行期文本↔token 转换 |
| `xgrammar` | 结构化解码约束 | function calling / JSON 输出 |
| `googletest` | C++ 测试框架 | 仅 `BUILD_CPP_TEST=ON` 时用 |
| `stb` | 图像解码 | 多模态（VLM）图像加载 |
| `argparse` | C++ 命令行解析 | C++ 侧工具/示例 |

#### 4.3.3 源码精读

先看 submodule 声明，注意 TVM 的真实来源：

[.gitmodules:10-12](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/.gitmodules#L10-L12) —— `3rdparty/tvm` 的 url 指向 `https://github.com/mlc-ai/relax.git`，即 MLC 团队维护的 TVM fork（Relax 分支）。这点很关键：MLC LLM 用的不是上游主线 TVM，而是带了 Relax 的定制版。

[.gitmodules:4-6](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/.gitmodules#L4-L6) —— `tokenizers-cpp` 同样来自 `mlc-ai` 组织，是配套的 C++ 分词库。

[.gitmodules:16-18](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/.gitmodules#L16-L18) —— `xgrammar` 也是 `mlc-ai` 自研。

再看 CMake 如何把依赖接入。TVM 的路径定位与子目录引入：

[CMakeLists.txt:59-67](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/CMakeLists.txt#L59-L67) —— 若未显式指定 `TVM_SOURCE_DIR`，默认指向 `3rdparty/tvm`，并通过 `add_subdirectory` 把 TVM 整个纳入构建（`EXCLUDE_FROM_ALL` 表示除非被依赖，否则不主动构建 TVM 的全部目标）。

[CMakeLists.txt:49-58](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/CMakeLists.txt#L49-L58) —— 这一段关闭了 TVM 运行时里用不到的组件（RPC、graph executor、profiler 等），只保留最小运行时。说明 MLC LLM 对 TVM 是「裁剪着用」的，不需要完整 TVM。

tokenizers-cpp 与 xgrammar 的引入：

[CMakeLists.txt:70-77](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/CMakeLists.txt#L70-L77) —— 定义 `TOKENZIER_CPP_PATH` 与 `XGRAMMAR_PATH` 指向 `3rdparty/`，`add_subdirectory` 引入 tokenizers，并把 xgrammar 的源码（`xgrammar/cpp/*.cc`，排除其 pybind 部分）合并进 `MLC_LLM_SRCS` 一起编译。

最后是链接关系，体现「谁依赖谁」：

[CMakeLists.txt:102-104](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/CMakeLists.txt#L102-L104) —— `mlc_llm` 库 `PUBLIC` 链接 `tvm_runtime`，`PRIVATE` 链接 `tokenizers_cpp`。这意味着 `tvm_runtime` 是对外暴露的依赖（MLC 的公开 API 里会用 TVM 类型），而 `tokenizers_cpp` 是内部实现细节。

#### 4.3.4 代码实践

**实践目标**：用只读 git 命令，确认 `3rdparty/` 下的依赖确实是独立 submodule，而非主仓库代码。

**操作步骤**：

1. 在仓库根目录运行 `git submodule status`，观察每个 submodule 当前指向的 commit。
2. 运行 `git ls-files 3rdparty/`，观察输出——你会发现 submodule 目录本身**不会**列出其内部文件（git 只跟踪 submodule 指针，不跟踪其内部文件）。
3. 对照本讲 `.gitmodules` 的源码精读，确认 `3rdparty/tvm` 的来源是 `mlc-ai/relax`，而非上游 `apache/tvm`。

**需要观察的现象**：

- `git submodule status` 会列出 6 个 submodule（argparse、tokenizers-cpp、googletest、tvm、stb、xgrammar）及其 commit hash。
- `git ls-files 3rdparty/` 只显示目录条目，不展开内部文件——这是 submodule 的特征。
- 若未执行 `git submodule update --init`，`3rdparty/tvm/` 目录可能为空。

**预期结果**：你理解了「`3rdparty/` 是挂载点，真实代码在各自独立仓库」，并明白为何 `pyproject.toml` 的 `sdist.exclude` 里特意排除了 `3rdparty/tvm`（打包源码分发时不带庞大的 TVM）。

> 说明：本实践只读不写。

#### 4.3.5 小练习与答案

**练习 1**：为什么 MLC LLM 用的是 `mlc-ai/relax` 而不是 Apache 主线 TVM？  
**参考答案**：MLC 团队维护的 Relax 分支包含 Relax（一种面向 LLM 的图 IR）相关特性，这些尚未或专门为 LLM 场景定制。MLC LLM 的模型定义（Relax nn）和许多 compiler pass 依赖这些特性，所以必须用定制 fork。

**练习 2**：CMake 里 `EXCLUDE_FROM_ALL` 加在 `add_subdirectory(tvm ...)` 上是什么意图？  
**参考答案**：表示 TVM 的构建目标默认不会被主动构建（不进 `make` 的默认目标），只有当主项目（如 `mlc_llm`）依赖其中某个目标（如 `tvm_runtime`）时，那个目标才会被连带构建。这避免每次都全量编译 TVM，节省时间。

**练习 3**：如果要做 function calling（让模型输出 JSON），最依赖 `3rdparty/` 的哪个库？  
**参考答案**：`xgrammar`。它负责语法约束解码，是把模型输出「锁」在合法 JSON（或其他结构）内的底层引擎。

## 5. 综合实践

把本讲三个模块串起来，完成一张「MLC LLM 仓库全景图」。

**任务**：在一张纸或文本文件上，画出仓库顶层结构，并完成以下标注：

1. **职责标签**：给 `python/`、`cpp/serve`、`cpp/json_ffi`、`cpp/multi_gpu`、`android/`、`ios/`、`web/`、`docs/`、`examples/`、`tests/`、`3rdparty/` 各贴一个标签（编译 / 引擎 / 部署 / 文档 / 依赖 / 工程）。
2. **语言边界**：在 `python/` 与 `cpp/` 之间画一条箭头，标注「JSON FFI」，并用一句话说明数据流方向（Python 请求 → JSON → C++ 引擎 → 结果原路返回）。
3. **依赖地基**：在 `3rdparty/` 下标注 `tvm`（编译地基）、`tokenizers-cpp`（分词）、`xgrammar`（结构化输出），并各用一句话说明作用。
4. **C++ 入口定位**：明确标出推理引擎心脏在 `cpp/serve/`（尤其 `threaded_engine.cc`、`engine.cc`、`model.cc`），JSON 桥在 `cpp/json_ffi/`。

**验收标准**：

- 仅凭这张图，你能回答：「我想看推理引擎的后台循环，去哪个文件？」「`MLCEngine` 是 Python 还是 C++？」「编译器站在谁的肩膀上？」三个问题。
- 答案应分别是：`cpp/serve/threaded_engine.cc`；Python 封装 + C++ 实现（边界在 `json_ffi`）；`3rdparty/tvm`。

> 这是贯穿本讲的综合实践，做完后建议把图保存下来，后续每一讲都可用它定位代码位置。

## 6. 本讲小结

- 顶层目录可按四类贴标签：`python/`（编译+CLI+引擎封装）、`cpp/`（C++ 推理引擎）、`android/ios/web`（部署）、`docs/`（文档）、`3rdparty/`（依赖地基）。
- **编译期逻辑几乎都在 Python 侧**（`model/`、`quantization/`、`compiler_pass/`、`loader/`、`interface/`），**运行期快路径在 C++ 侧**（`cpp/serve/`）。
- Python 与 C++ 的边界是 **JSON FFI**：Python 把请求序列化成 JSON 调进 C++，`MLCEngine` 只是这层边界的薄封装。
- 一个 `mlc_llm` Python 包 = Python 源码 + C++ 编译产物（`libmlc_llm.so`），由 `scikit-build-core` 在 `pip install` 时调 CMake 一并构建。
- 三个核心第三方依赖：`tvm`（编译地基，用的是 `mlc-ai/relax` fork）、`tokenizers-cpp`（分词）、`xgrammar`（结构化解码）。
- C++ 推理引擎的真正入口在 `cpp/serve/`（心脏是 `threaded_engine.cc`），JSON 桥在 `cpp/json_ffi/`。

## 7. 下一步学习建议

有了这张目录地图，接下来该进入「怎么用」了。建议下一步学习 **u1-l3 安装、构建与快速运行**，亲手把 `mlc_llm` 跑起来，用 chat CLI、Python `MLCEngine`、REST 服务器三种方式各发起一次对话。

如果你想先看 CLI 的全貌，可以直接跳到 **u2-l1 CLI 总入口与子命令分发**，研究 `python/mlc_llm/__main__.py` 如何把 `mlc_llm` 这一个命令分发到 compile / convert_weight / chat / serve 等子命令。

对于想提前接触引擎代码的读者，可以现在就去浏览 `cpp/serve/engine.h` 和 `cpp/serve/threaded_engine.h` 这两个头文件——但完整理解要等到进阶层（U9）的 C++ 引擎架构单元。
