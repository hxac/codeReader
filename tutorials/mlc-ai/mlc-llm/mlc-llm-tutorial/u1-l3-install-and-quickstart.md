# 安装、构建与快速运行

## 1. 本讲目标

前两讲（u1-l1、u1-l2）我们建立了两件事：MLC LLM 是「ML 编译器 + 部署引擎」，以及这些能力分别住在仓库的哪个目录。但这些都是「读」出来的认知——本讲要把它变成「跑」出来的认知。

本讲要回答的核心问题是：**我怎么把 mlc_llm 装好、并最快地用它跟一个真实大模型对话一次？**

读完本讲，你应当能够：

1. 用两种方式（预编译 wheel / 源码编译）安装 `mlc_llm`，并能验证安装是否成功——知道为什么「跑模型」几乎不需要 TVM，但「编译模型」需要。
2. 用 `mlc_llm chat` 这一条命令，加载一个 `HF://` 前缀的 MLC 模型并交互对话，会用 `/stats`、`/set` 等特殊指令。
3. 区分三种运行入口——**chat CLI**、**Python `MLCEngine`**、**REST 服务器**，理解它们各自走的 JSON FFI 桥，并能分别跑通一次流式对话。

本讲是「动手型」讲义，含一个贯穿性的代码实践：跑 `sample_mlc_engine.py` 并观察每秒生成 token 数。受限于运行环境（需要 GPU 或至少 6GB 显存），部分结果标注「待本地验证」，但每一步都可操作。

## 2. 前置知识

### 2.1 预编译 wheel 与源码编译

Python 包有两种发版方式：

- **预编译 wheel（prebuilt wheel）**：作者已经在你目标平台（如 Linux + CUDA）上把需要编译的部分（C++ 代码）编译好，打成一个二进制包。你 `pip install` 时不用碰编译器，直接拿来用。MLC LLM 提供了 `mlc-llm-nightly-cu128` 这类按平台命名的 nightly wheel。
- **源码编译（build from source）**：你拿到的是纯源码，需要自己调 CMake/编译器生成二进制。慢，但能改代码、能拿到指定版本。

> 直觉类比：预编译 wheel 像「超市买的速冻饺子」，源码编译像「自己买面粉和馅儿包」。前者快，后者灵活。

### 2.2 TVM「编译器」与「运行时」的分家

上一讲（u1-l2）我们说 MLC LLM 的编译能力站在 `3rdparty/tvm` 肩膀上。这里要补一个关键区分：

- **TVM 编译器（compiler）**：把模型翻译成硬件专用代码的那一堆优化与代码生成逻辑。**只有「编译自己的模型」时才需要。**
- **TVM 运行时（runtime）**：加载并执行已编译产物的那部分最小逻辑。**跑模型时必须有，但它很轻量，已经被打包进 MLC LLM 里了。**

换句话说：如果你只是想「下载别人编译好的 MLC 模型跑起来」，你**不需要**装完整 TVM 编译器，`libmlc_llm.so` 里已经带着 `libtvm_runtime.so`。这是本讲最容易被忽略却最重要的一点。

### 2.3 三种运行模式 local / interactive / server

MLC LLM 的引擎有一个 `mode` 参数，预设了三种使用场景的并发与显存配置：

| 模式 | 典型入口 | 并发 | 说明 |
|------|----------|------|------|
| `interactive` | chat CLI | 最多 1 | 交互式单用户对话，max batch size=1 |
| `local` | Python `MLCEngine`（默认） | 低（max batch=4） | 本地部署，请求并发低 |
| `server` | REST 服务器 | 高 | 服务端，尽量榨干 GPU 显存与并发 |

记住这张表，本讲三种入口正好对应三种模式——这是贯穿性的线索。

### 2.4 `HF://` 前缀是什么

你会在命令里反复看到 `HF://mlc-ai/Llama-3-8B-Instruct-q4f16_1-MLC` 这种写法。`HF://` 是 MLC LLM 约定的「从 HuggingFace Hub 下载」协议前缀。`mlc-ai/XXX-MLC` 是 MLC 团队上传的「已经量化好、配好 `mlc-chat-config.json`」的即用模型仓库。引擎首次运行会自动拉取权重与配置并缓存到本地。

## 3. 本讲源码地图

本讲聚焦「安装与三条运行入口」，涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| `docs/install/mlc_llm.rst` | 安装文档权威来源：预编译 wheel 与源码编译两条路径、验证命令、TVM 依赖说明 |
| `python/mlc_llm/libinfo.py` | 运行期定位 C++ 动态库 `libmlc_llm.so` 的「胶水」，是验证安装是否成功的关键 |
| `python/mlc_llm/cli/chat.py` | `mlc_llm chat` 命令的参数解析入口 |
| `python/mlc_llm/interface/chat.py` | chat 的真正实现：特殊指令（`/stats`、`/set`）、交互循环、`JSONFFIEngine` 创建 |
| `examples/python/sample_mlc_engine.py` | Python `MLCEngine` 的最小可运行示例（本讲主实践） |
| `python/mlc_llm/json_ffi/engine.py` | `JSONFFIEngine`：经 JSON FFI 桥创建 C++ 引擎（chat CLI 走这里） |
| `python/mlc_llm/serve/engine_base.py` | `MLCEngineBase`：经另一条 FFI 入口 `create_threaded_engine` 创建 C++ 引擎（Python API 走这里） |
| `python/mlc_llm/interface/serve.py` | `mlc_llm serve` 的实现：创建 `AsyncMLCEngine` 并启动 FastAPI/uvicorn |
| `docs/get_started/quick_start.rst` | 官方快速上手文档，三种入口的「标准答案」出处 |

> 说明：本讲不展开编译器（那是 U3–U8 的事），只看「装好之后怎么跑」。涉及 C++ 引擎内部时，点到「JSON FFI 桥」为止。

## 4. 核心概念与源码讲解

### 4.1 安装与验证

#### 4.1.1 概念说明

`mlc_llm` 的安装有两条路：**预编译 wheel** 和 **源码编译**。对绝大多数只想「跑模型」的读者，预编译 wheel 就够了；只有要改 MLC LLM 自身代码、或要部署到非预编译平台时，才需要源码编译。

无论哪条路，安装完都要做**两步验证**：

1. **Python 层验证**：`import mlc_llm` 不报错，说明 Python 包结构完整。
2. **C++ 层验证**：能找到 `libmlc_llm.so`（Linux）/ `.dylib`（macOS）/ `.dll`（Windows），说明 C++ 引擎的二进制产物到位了——因为真正的推理在 C++ 里（u1-l2 讲过的边界）。

这背后是上一讲提到的 `libinfo.py`：它在运行期被调用去「找」C++ 动态库，找不到就直接抛错。

#### 4.1.2 核心流程

安装与验证的完整流程：

```
[选路径]
  ├─ 预编译 wheel: pip install --pre -U -f https://mlc.ai/wheels mlc-llm-nightly-<平台>
  └─ 源码编译:    git clone --recursive → cmake/gen_cmake_config → cmake .. && make → pip install -e .

[验证]
  1. python -c "import mlc_llm; print(mlc_llm)"          ← Python 层
  2. 找到 libmlc_llm.so（预编译：在 site-packages 里；源码：在 build/ 里）  ← C++ 层
  3. mlc_llm chat -h                                       ← CLI 入口可用
```

关键判断点：

1. 预编译 wheel 把 C++ 产物直接塞进 Python 包目录，所以 `import mlc_llm` 后包目录里就有 `.so`。
2. 源码编译时，`.so` 默认在仓库的 `build/` 目录，靠 `libinfo.py` 的搜索路径找到。
3. 两种路径验证命令**几乎一样**，区别只在于 `import mlc_llm` 打印出的路径不同。

#### 4.1.3 源码精读

先看官方文档的验证命令，这是「标准答案」：

[docs/install/mlc_llm.rst:154-157](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/install/mlc_llm.rst#L154-L157) —— 文档给出的 Python 层验证：`python -c "import mlc_llm; print(mlc_llm)"`，预期打印出包的安装路径。预编译与源码两种安装都用这同一条命令。

源码编译特有的 C++ 验证（确认两个关键 `.so` 生成）：

[docs/install/mlc_llm.rst:232-240](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/install/mlc_llm.rst#L232-L240) —— `ls -l ./build/` 应看到 `libmlc_llm.so` 和 `libtvm_runtime.so`；`mlc_llm chat -h` 应打印帮助。注意这里的 `libtvm_runtime.so` 印证了 2.2 节：**运行时（runtime）随包附带，无需单独装 TVM**。

最关键的「TVM 编译器 vs 运行时」区分，文档里有一段明确说明：

[docs/install/mlc_llm.rst:195-197](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/install/mlc_llm.rst#L195-L197) —— 对运行时来说，TVM **编译器**不是 chat CLI 或 Python API 的依赖，只需要 TVM 的运行时（已包含在 `3rdparty/tvm`）；**只有当你要编译自己的模型时**，才需要按 TVM 安装文档装完整编译器。这正是本节要传递的核心认知。

那么 Python 是怎么「找到」C++ 动态库的？答案在 `libinfo.py`。先看它如何根据平台决定库文件名：

[python/mlc_llm/libinfo.py:51-58](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/libinfo.py#L51-L58) —— 按操作系统把 `mlc_llm` 拼成 `libmlc_llm.so`（Linux）、`mlc_llm.dll`（Windows）、`libmlc_llm.dylib`（macOS）。这是跨平台定位的第一步。

再看它去哪些目录找：

[python/mlc_llm/libinfo.py:18-37](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/libinfo.py#L18-L37) —— `get_dll_directories()` 给出的候选搜索路径包含：包自身目录（预编译 wheel 的 `.so` 就在这）、`build/` 与 `build/Release`（源码编译的 `.so` 在这）、`MLC_LIBRARY_PATH` 环境变量、`CONDA_PREFIX/lib`，以及系统动态库路径（`LD_LIBRARY_PATH` / `DYLD_LIBRARY_PATH` / `PATH`）。这套搜索逻辑解释了「为什么两种安装方式都能被找到」。

最后看找不到时会发生什么：

[python/mlc_llm/libinfo.py:62-71](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/libinfo.py#L62-L71) —— 若所有候选路径都没有该 `.so` 且非可选，则抛 `RuntimeError: Cannot find libraries`，并列出所有候选路径。这是「C++ 层验证失败」的典型报错——遇到它就说明二进制产物没装到位。

> 旁注：文件第 7-8 行还有两个环境变量挂钩——`__version__` 和 `MLC_LIBRARY_PATH`。后者允许你显式指定一个额外的库搜索目录，是排查「找不到 `.so`」问题的常用手段。

#### 4.1.4 代码实践

**实践目标**：亲手完成两步验证，确认 Python 层与 C++ 层都安装成功。

**操作步骤**：

1. 执行文档的标准验证命令：
   ```bash
   python -c "import mlc_llm; print(mlc_llm)"
   ```
   记下打印的包路径。
2. 进入该路径（预编译 wheel 装的话），查看是否存在 `libmlc_llm.so`（或对应平台的扩展名）。源码编译的话则 `ls -l ./build/` 查看 `libmlc_llm.so` 与 `libtvm_runtime.so`。
3. 验证 CLI 入口：`mlc_llm chat -h`，应打印出 `model`、`--device`、`--model-lib`、`--overrides` 等参数帮助。

**需要观察的现象**：

- 第 1 步打印出形如 `<module 'mlc_llm' from '/.../mlc_llm/__init__.py'>` 的路径。
- 第 2 步能在 `libinfo.py` 列出的候选路径之一中找到二进制库。
- 第 3 步打印出帮助文本，证明 `console_scripts` 入口点（u1-l2 讲过）注册成功。

**预期结果**：三步全部通过，说明 mlc_llm 安装完整、可被调用。

> 待本地验证：若第 2 步找不到 `.so`，会在引擎启动时触发 `libinfo.py` 第 63-70 行的 `Cannot find libraries` 报错。可临时 `export MLC_LIBRARY_PATH=/path/to/lib` 指向正确的 `.so` 目录来排查。

#### 4.1.5 小练习与答案

**练习 1**：我只想用 `mlc_llm chat` 跑别人编译好的模型，需要装完整 TVM 编译器吗？  
**参考答案**：不需要。运行时只需要 TVM 的 runtime，而 `libtvm_runtime.so` 已随 MLC LLM 一起编译/打包（见 `install/mlc_llm.rst` 第 195-197 行）。完整 TVM 编译器只有在你「编译自己的模型」时才需要。

**练习 2**：`libinfo.py` 同时搜索「包自身目录」和「`build/` 目录」，为什么要有这两套？  
**参考答案**：分别对应两种安装方式——预编译 wheel 把 `.so` 放在包自身目录，源码编译把 `.so` 放在仓库 `build/` 目录。`get_dll_directories` 把两种位置都纳入候选，所以两种安装都能被同一套代码找到。

**练习 3**：遇到 `Cannot find libraries: libmlc_llm.so` 报错，最快的不重装排查方法是什么？  
**参考答案**：设置环境变量 `MLC_LIBRARY_PATH` 指向 `.so` 所在目录（见 `libinfo.py` 第 8 行与第 27-28 行），它会追加到候选搜索路径。这能在不改代码、不重装的前提下让运行期找到库。

### 4.2 chat CLI 一键运行

#### 4.2.1 概念说明

三种入口里，**chat CLI 是最快的一条**：一条命令、零行代码就能和真实大模型对话。它的本质是一个交互式 REPL（读取-求值-输出循环），背后仍然跑着完整的 C++ 推理引擎，只是把 Python API 包了一层「命令行聊天界面」。

chat CLI 有几个对初学者极友好的设计：

1. **`HF://` 自动下载**：给一个 HF 仓库地址，引擎自动拉权重与配置，首次下载、之后走缓存。
2. **`--device auto` 默认自动探测**：不指定设备也能跑，引擎自己挑可用的 GPU/Metal/Vulkan。
3. **特殊指令**：在对话里输入 `/stats`、`/set` 等以斜杠开头的命令，能查速度、改参数、重置历史，无需重启。

chat CLI 走的是 `mode="interactive"`（2.3 节表格里的第一种），最多 1 个并发请求——这正是「单用户对话」场景。

#### 4.2.2 核心流程

从敲下命令到收到第一个 token，调用链如下：

```
mlc_llm chat HF://mlc-ai/...-MLC
   │
   ▼
python/mlc_llm/__main__.py        ← 总入口，分发到 chat 子命令（u2-l1 详讲）
   │
   ▼
python/mlc_llm/cli/chat.py         ← 解析参数：model / --device / --model-lib / --overrides
   │
   ▼
python/mlc_llm/interface/chat.py   ← 创建 JSONFFIEngine(mode="interactive") + ChatState
   │
   ▼  经 JSON FFI 桥
cpp/json_ffi + cpp/serve            ← C++ ThreadedEngine 真正推理
   │
   ▼
交互循环：用户输入 → /stats /set 等特殊指令 或 generate(prompt) → 流式打印
```

关键判断点：

1. chat CLI 用的是 `JSONFFIEngine`（不是 Python API 用的 `MLCEngine`），它通过 JSON 字符串跨 FFI 调 C++——这条边界 u1-l2 已建立。
2. 特殊指令（`/stats` 等）是 **Python 层 `ChatState`** 处理的，不进 C++；只有真正生成（`generate`）才进 C++。
3. 速度统计（`/stats`）来自 C++ 回传的 `usage.extra`，Python 只负责格式化打印。

#### 4.2.3 源码精读

先看 CLI 参数定义，这是 `mlc_llm chat` 能接受什么的权威说明：

[python/mlc_llm/cli/chat.py:12-34](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/chat.py#L12-L34) —— 四个参数：位置参数 `model`（必填，模型路径或 `HF://` 地址）、`--device`（默认 `"auto"`）、`--model-lib`（手动指定编译库 `.so`，默认 None 时由引擎搜索/JIT）、`--overrides`（覆盖模型配置，如 `context_window_size`）。

接着看真正的实现入口：

[python/mlc_llm/interface/chat.py:285-311](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/chat.py#L285-L311) —— `chat()` 函数用 `JSONFFIEngine(..., mode="interactive", ...)` 创建引擎，再用 `ChatState(engine).chat()` 启动交互循环，`finally` 里调 `engine.terminate()` 收尾。注意 `mode="interactive"` 正是 2.3 节表格对应的单用户模式。

特殊指令的全集在这里：

[python/mlc_llm/interface/chat.py:18-30](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/chat.py#L18-L30) —— `_print_help_str()` 列出全部特殊指令：`/help`（打印帮助）、`/exit`（退出）、`/stats`（打印上次请求的 token/sec）、`/metrics`（完整引擎指标）、`/reset`（重启新对话）、`/set [overrides]`（覆盖生成参数）。还提示「escape+enter 换行」做多行输入。

交互循环如何识别这些指令：

[python/mlc_llm/interface/chat.py:257-282](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/chat.py#L257-L282) —— 循环读取用户输入，按前缀分发：`/set` 解析并覆盖 `overrides`，`/stats` 调 `self.stats()`，`/metrics` 调 `self.metrics()`，`/reset` 重置，`/exit` 退出，`/help` 打印帮助，其余输入当作 prompt 调 `self.generate(prompt)`。

`/stats` 的具体实现，揭示速度数据从哪来：

[python/mlc_llm/interface/chat.py:222-238](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/chat.py#L222-L238) —— `stats()` 从 `last_finished_request_usage.extra` 取 `prefill_tokens_per_s` 与 `decode_tokens_per_s`，格式化成 `prefill: X tok/s, decode: Y tok/s` 打印。这说明「速度」是 C++ 引擎测好后塞进响应的 `usage.extra` 字段回传的——这点在 4.3 节 Python API 实践里会再次用到。

#### 4.2.4 代码实践

**实践目标**：用一条命令跑起 chat CLI，体验交互对话与特殊指令。

**操作步骤**：

1. 运行官方快速上手里的命令（建议至少 6GB 显存）：
   ```bash
   mlc_llm chat HF://mlc-ai/Llama-3-8B-Instruct-q4f16_1-MLC
   ```
   首次运行会自动从 HF 下载权重与配置，耐心等待。
2. 出现 `>>> ` 提示符后，输入一句话（如 `What is the meaning of life?`），回车等待流式输出。
3. 输入 `/stats`，观察上次请求的 prefill/decode 速度。
4. 输入 `/set temperature=0.5;max_tokens=100`，再发同一句话，观察输出变化。

**需要观察的现象**：

- 引擎启动时会打印设备探测、模型加载日志；首次下载可见进度条。
- 流式输出时 token 逐字打印（`flush=True`）。
- `/stats` 打印形如 `prefill: XX.X tok/s, decode: YY.Y tok/s`。
- `/set` 后再次提问，生成风格/长度受新参数影响。

**预期结果**：你能用一条命令进入交互对话，并通过 `/stats`、`/set` 在不退出、不重启的情况下查看速度、修改生成参数。

> 待本地验证：实际 tok/s 数值取决于你的硬件（GPU 型号、显存带宽），本讲无法给出确定数字，需在本地实测。若显存不足，可换更小模型（如 `RedPajama-INCITE-Chat-3B-v1` 对应的 MLC 仓库）。

#### 4.2.5 小练习与答案

**练习 1**：`mlc_llm chat` 不传 `--model-lib` 也能跑，那模型库从哪来？  
**参考答案**：引擎会用 `model`（`HF://` 地址或本地路径）去搜索已编译的 model lib；若找不到，会按 JIT（即时编译）方式现场编译（`MLCEngine` 的 docstring 第 1406-1409 行明确说明了这一点）。`--model-lib` 只是允许你手动指定一个 `.so` 路径来跳过搜索。

**练习 2**：`/stats` 显示的速度是 Python 测出来的吗？  
**参考答案**：不是。`stats()`（`interface/chat.py` 第 222-238 行）只是从 C++ 回传的 `usage.extra` 里取出 `prefill_tokens_per_s` 与 `decode_tokens_per_s` 并格式化打印。真正的计时在 C++ 引擎侧，Python 仅做展示。

**练习 3**：chat CLI 用的是 `mode="interactive"`，它和 `local`/`server` 模式的根本区别是什么？  
**参考答案**：`interactive` 模式把 max batch size 设为 1（同时只服务 1 个请求），适合单用户对话；`local` 允许低并发（max batch=4）；`server` 尽量放大并发并榨干显存。模式本质是「针对使用场景预设的并发与显存配置」（详见 `MLCEngine` docstring 第 1411-1431 行）。

### 4.3 Python API 与 REST 用法

#### 4.3.1 概念说明

chat CLI 适合「人坐着对话」，但很多场景需要**程序化调用**——把推理能力嵌进你自己的代码或服务。MLC LLM 提供另外两个入口：

1. **Python `MLCEngine`**：一个同步的、OpenAI 风格的 Python API。`engine.chat.completions.create(...)` 几乎和 OpenAI Python SDK 一模一样，但推理在你本地硬件上跑。还有异步版本 `AsyncMLCEngine`。它默认 `mode="local"`。
2. **REST 服务器**：`mlc_llm serve` 启动一个 FastAPI/uvicorn HTTP 服务，暴露 OpenAI 兼容的 `/v1/chat/completions` 等端点。任何能发 HTTP 的语言都能调用。它用 `mode="server"`，背后是 `AsyncMLCEngine`。

三者背后是同一套 C++ ThreadedEngine，但**进入 C++ 的「门」有两条**（这是初学者容易混淆的细节）：

- chat CLI 的 `JSONFFIEngine` 走 `mlc.json_ffi.CreateJSONFFIEngine` 这扇门——所有请求先序列化成 JSON 字符串再过 FFI。
- Python API 的 `MLCEngineBase` 走 `mlc.serve.create_threaded_engine` 这扇门——更直接地拿到 `add_request`/`abort_request` 等结构化接口。

两扇门最终都创建了同一个 C++ `ThreadedEngine`（U9 会深入），只是封装层级不同。

> 直觉类比：同一个后厨（C++ 引擎），`JSONFFIEngine` 是「传纸条点单」（一切走 JSON 字符串），`MLCEngineBase` 是「对讲机点单」（直接调结构化接口）。菜都是后厨做的。

#### 4.3.2 核心流程

**Python `MLCEngine` 的数据流**（`sample_mlc_engine.py` 的本质）：

```
MLCEngine(model)                                    ← Python 构造，mode 默认 local
   │  内部调 mlc.serve.create_threaded_engine 创建 C++ ThreadedEngine
   ▼
engine.chat.completions.create(messages, stream=True)
   │  把请求转成引擎调用，注册 stream callback
   ▼
C++ ThreadedEngine                                  ← prefill / decode / sample
   │  通过 background_stream_back_loop 把流式响应推回
   ▼
Python 迭代器 yield 每个 ChatCompletionStreamResponse → choice.delta.content
```

**REST 服务器的数据流**（`mlc_llm serve`）：

```
mlc_llm serve HF://...                              ← CLI
   │
   ▼
interface/serve.py → 创建 AsyncMLCEngine(mode="server")
   │  + FastAPI app + CORS 中间件 + uvicorn.run()
   ▼
HTTP POST /v1/chat/completions  ← curl / OpenAI SDK / 任意客户端
   │
   ▼
AsyncMLCEngine（内部仍是 C++ ThreadedEngine）
```

关键判断点：

1. `MLCEngine`（同步）与 `AsyncMLCEngine`（异步）共享 `MLCEngineBase`，只是封装了「同步等待」还是「async/await」。
2. REST 服务器的端点实现（`/v1/chat/completions` 等）会在 U11 详讲，本讲只到「启动 + 发请求」。
3. 想在 Python 里拿「每秒 token 数」，要在请求里加 `stream_options={"include_usage": True}`，然后从 `response.usage.extra` 读——这正是 chat CLI `/stats` 的数据来源。

#### 4.3.3 源码精读

先看本讲主实践的示例脚本，它极简：

[examples/python/sample_mlc_engine.py:1-19](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/examples/python/sample_mlc_engine.py#L1-L19) —— 整个脚本只有三步：第 6-7 行 `MLCEngine(model)` 创建引擎；第 10-16 行以 `stream=True` 迭代 `engine.chat.completions.create(...)`，逐个 `response` 取 `choice.delta.content` 打印；第 19 行 `engine.terminate()` 收尾。注意它**没有** `stream_options={"include_usage": True}`，所以原脚本不会捕获 `usage`（速度数据）——这正是本讲实践要补充的。

这个脚本对应官方文档的 Python tab：

[docs/get_started/quick_start.rst:23-39](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/get_started/quick_start.rst#L23-L39) —— quick_start 的 Python tab 给出与示例脚本几乎一致的代码，强调 `MLCEngine` 提供 OpenAI API 风格接口。

`MLCEngine` 的构造参数与模式语义：

[python/mlc_llm/serve/engine.py:1441-1461](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine.py#L1441-L1461) —— `MLCEngine.__init__` 调父类 `MLCEngineBase("sync", ...)`，并挂上 `self.chat` 与 `self.completions` 两个 OpenAI 风格的接口对象。`model` 参数的 docstring（第 1397-1409 行）说明它接受 `mlc-chat-config.json` 路径、MLC 模型目录，或指向 MLC 编译模型的 HF 链接；`model_lib` 找不到时会 JIT 编译。

[python/mlc_llm/serve/engine.py:819-819](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine.py#L819-L819) —— `AsyncMLCEngine` 同样继承 `MLCEngineBase`，是 REST 服务器与异步场景用的版本。

Python API 进入 C++ 的那扇「门」：

[python/mlc_llm/serve/engine_base.py:612-612](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/serve/engine_base.py#L612-L612) —— `MLCEngineBase.__init__` 通过 `tvm.get_global_func("mlc.serve.create_threaded_engine")()` 拿到 C++ 工厂函数并创建引擎，随后第 613-622 行取出 `add_request`/`abort_request`/`run_background_loop` 等结构化 FFI 接口。这是 Python API 的桥。

对照 chat CLI 走的另一扇「门」：

[python/mlc_llm/json_ffi/engine.py:239-253](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/json_ffi/engine.py#L239-L253) —— `JSONFFIEngine.__init__` 通过 `tvm.get_global_func("mlc.json_ffi.CreateJSONFFIEngine")()` 创建引擎，取出的接口是 `chat_completion`/`abort`/`reload` 等——注意**没有** `add_request` 这种结构化接口，一切以 JSON 字符串进出。这就是「两扇门」的区别所在。

再看 REST 服务器怎么启动：

[python/mlc_llm/interface/serve.py:24-87](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/serve.py#L24-L87) —— `serve()` 函数把命令行参数（host/port/并发/显存/推测解码/前缀缓存等）整理成 `EngineConfig`，创建 `engine.AsyncMLCEngine(model=..., mode=mode, ...)`，再挂上 FastAPI app（含 CORS 中间件）由 uvicorn 运行。`mode` 在服务器场景下通常是 `"server"`。

REST 的官方用法：

[docs/get_started/quick_start.rst:60-75](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/docs/get_started/quick_start.rst#L60-L75) —— quick_start 的 REST tab：`mlc_llm serve HF://mlc-ai/Llama-3-8B-Instruct-q4f16_1-MLC` 启动服务（默认 `http://127.0.0.1:8000`），看到 `Uvicorn running on ...` 后，用 `curl -X POST ... /v1/chat/completions` 发 JSON 请求即可。

最后，速度数据的协议定义（Python API 取 tok/s 的依据）：

[python/mlc_llm/protocol/openai_api_protocol.py:56-67](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/protocol/openai_api_protocol.py#L56-L67) —— `CompletionUsage` 在标准 `prompt_tokens`/`completion_tokens`/`total_tokens` 之外，多了 `extra: Optional[Dict]` 字段，C++ 引擎把 `prefill_tokens_per_s`、`decode_tokens_per_s` 塞在这里回传。而要让流式响应里带上 `usage`，需要 `StreamOptions(include_usage=True)`——这两个类型共同支撑了「Python API 拿 tok/s」。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：运行 `sample_mlc_engine.py` 对 Llama-3-8B 发起一次流式 chat completion，并打印每秒 token 数。

**背景说明**：原脚本（第 10-16 行）只流式打印文本，没有捕获 `usage`。而 chat CLI 的 `/stats`（4.2 节）是 `ChatState` 的交互功能，Python `MLCEngine` 路径下取速度的等价做法是：开启 `stream_options={"include_usage": True}`，再从最后一个带 `usage` 的响应里读 `extra`。下面给出**示例代码**（基于原脚本修改，非仓库原文件）。

**示例代码**（在 `sample_mlc_engine.py` 基础上修改）：

```python
# 示例代码：在 sample_mlc_engine.py 上增加 usage 捕获
from mlc_llm import MLCEngine

model = "HF://mlc-ai/Llama-3-8B-Instruct-q4f16_1-MLC"
engine = MLCEngine(model)

last_usage = None
for response in engine.chat.completions.create(
    messages=[{"role": "user", "content": "What is the meaning of life?"}],
    model=model,
    stream=True,
    stream_options={"include_usage": True},  # 关键：让流式响应带上 usage
):
    if response.usage is not None:           # 带 usage 的最后一块
        last_usage = response.usage
        continue
    for choice in response.choices:
        print(choice.delta.content, end="", flush=True)
print("\n")

# 打印每秒 token 数（与 chat CLI 的 /stats 同源）
if last_usage is not None and last_usage.extra is not None:
    print("prefill:", last_usage.extra.get("prefill_tokens_per_s"), "tok/s")
    print("decode :", last_usage.extra.get("decode_tokens_per_s"), "tok/s")

engine.terminate()
```

**操作步骤**：

1. 先原样运行官方示例，确认链路通：
   ```bash
   python examples/python/sample_mlc_engine.py
   ```
   预期流式打印出对 `What is the meaning of life?` 的回答。
2. 把上面的「示例代码」存成一个新文件（如 `sample_with_stats.py`，放仓库外或 `examples/` 之外即可，**不要覆盖原脚本**），运行它。
3. 观察末尾打印的 `prefill` / `decode` 速度。

**需要观察的现象**：

- 第 1 步：文本逐字打印，证明 `MLCEngine` → C++ ThreadedEngine → 流式回传整条链路工作正常。
- 第 2 步：除文本外，末尾多出两行速度数字。
- 对照 4.2 节 chat CLI 的 `/stats`：两者打印的字段名（`prefill_tokens_per_s`/`decode_tokens_per_s`）应一致，因为同源（都来自 `usage.extra`，见 `interface/chat.py` 第 232-233 行）。

**预期结果**：你能用 Python API 完成一次流式对话，并用与 `/stats` 等价的方式拿到每秒 token 数，从而理解「特殊指令在 CLI 是命令、在 Python API 是 `usage.extra` 字段」的对应关系。

> 待本地验证：实际 tok/s 数值依赖硬件，本讲无法给出确定数字。若显存不足，把 `model` 换成更小的 MLC 仓库重试。

#### 4.3.5 小练习与答案

**练习 1**：`MLCEngine` 和 chat CLI 用的 `JSONFFIEngine` 进入 C++ 的「门」一样吗？  
**参考答案**：不一样。`MLCEngine`（经 `MLCEngineBase`）走 `mlc.serve.create_threaded_engine`（`engine_base.py` 第 612 行），拿到 `add_request` 等结构化接口；`JSONFFIEngine` 走 `mlc.json_ffi.CreateJSONFFIEngine`（`json_ffi/engine.py` 第 239 行），只有 `chat_completion` 等 JSON 字符串接口。两扇门最终都创建同一个 C++ ThreadedEngine。

**练习 2**：为什么原版 `sample_mlc_engine.py` 拿不到 token/sec？  
**参考答案**：因为它没有传 `stream_options={"include_usage": True}`，流式响应里就不会带 `usage` 块（`CompletionUsage` 定义在 `openai_api_protocol.py` 第 56-62 行，`StreamOptions.include_usage` 在第 65-67 行）。加上该选项后，最后一个响应会带 `usage.extra`，里面就是 `prefill_tokens_per_s`/`decode_tokens_per_s`。

**练习 3**：`mlc_llm serve` 默认监听哪个地址？用什么客户端发请求？  
**参考答案**：默认 `http://127.0.0.1:8000`（见 `quick_start.rst` 第 60-63 行）。任何能发 HTTP POST 的客户端都行——文档示例用 `curl`，但用 OpenAI 官方 Python SDK（把 `base_url` 指向该地址）也能直接调用，因为端点是 OpenAI 兼容的。

## 5. 综合实践

把本讲三种入口串起来，完成一次「同模型、三入口」对比。

**任务**：选定同一个模型（如 `HF://mlc-ai/Llama-3-8B-Instruct-q4f16_1-MLC`），分别用三种方式各发起一次相同 prompt（如 `用一句话介绍你自己`）的对话，并记录：

1. **chat CLI**：`mlc_llm chat HF://...`，对话后输入 `/stats`，记录 prefill/decode tok/s。
2. **Python `MLCEngine`**：运行 4.3.4 的示例代码（或原版 `sample_mlc_engine.py`），记录末尾的 tok/s。
3. **REST 服务器**：一个终端 `mlc_llm serve HF://...`，另一个终端用 `curl` 向 `/v1/chat/completions` 发请求（参考 `quick_start.rst` 第 67-75 行）。

**验收标准**：

- 三种方式都能成功生成回答，证明安装完整、三条链路皆通。
- 你能说清三者各自用的 `mode`（interactive / local / server）和进入 C++ 的「门」（`CreateJSONFFIEngine` vs `create_threaded_engine`）。
- 你能解释：为什么 chat CLI 的 `/stats` 和 Python API 的 `usage.extra` 显示的是同一组字段。

> 这是贯穿本讲的综合实践。做完后，你对「安装 → 三入口 → JSON FFI 桥 → C++ 引擎」这条主线就有了实操层面的体感，为后续讲义（U2 的 CLI 分发、U9 的 C++ 引擎、U11 的 Python 服务端）打下基础。

## 6. 本讲小结

- `mlc_llm` 有两种安装方式：预编译 wheel（快，按平台选 `mlc-llm-nightly-<平台>`）与源码编译（灵活，`cmake && make`）。验证分两层：`import mlc_llm`（Python）+ 找到 `libmlc_llm.so`（C++）。
- **跑模型不需要完整 TVM 编译器**，只需 TVM runtime，而 `libtvm_runtime.so` 已随包附带；TVM 编译器仅在「编译自己的模型」时才需要。
- `libinfo.py` 的 `find_lib_path`/`get_dll_directories` 负责跨平台定位 C++ 动态库，覆盖包目录、`build/`、`MLC_LIBRARY_PATH` 等候选；找不到时报 `Cannot find libraries`。
- 三种运行入口对应三种 `mode`：**chat CLI**（`interactive`，单用户，走 `JSONFFIEngine`）、**Python `MLCEngine`**（`local`，走 `MLCEngineBase`）、**REST `serve`**（`server`，`AsyncMLCEngine` + FastAPI/uvicorn）。
- 进入 C++ 有「两扇门」：`mlc.json_ffi.CreateJSONFFIEngine`（JSON 字符串接口，chat CLI 用）与 `mlc.serve.create_threaded_engine`（结构化接口，Python API 用），最终都创建同一个 C++ ThreadedEngine。
- 速度统计在 chat CLI 是 `/stats` 指令、在 Python API 是 `response.usage.extra`（需 `stream_options={"include_usage": True}`），两者同源，字段为 `prefill_tokens_per_s` / `decode_tokens_per_s`。

## 7. 下一步学习建议

本讲让你「跑通了」，但每条命令背后的分发机制还没展开。建议下一步学习 **u1-l4 端到端工作流与模型产物**，把 `convert_weight → gen_config → compile → serve` 这条主线和「MLC 权重 / model library / mlc-chat-config.json」三类产物串起来，理解你刚才 `HF://` 下载的到底是个什么结构。

如果你想先把命令行吃透，可以直接跳到 **u2-l1 CLI 总入口与子命令分发**，研究 `python/mlc_llm/__main__.py` 如何把 `mlc_llm` 这一个命令分发到 chat / serve / compile 等子命令——本讲里你反复敲的 `mlc_llm chat`、`mlc_llm serve` 就是从那里开始的。

对推理引擎本身感兴趣的读者，现在可以带着本讲的体感去浏览 `cpp/serve/threaded_engine.h` 与 `cpp/json_ffi/json_ffi_engine.h` 这两个头文件——但要完整理解 C++ 引擎的事件-动作循环、KV 缓存与采样，需要等到进阶层（U9–U10）。
