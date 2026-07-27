# 构建系统与运行方式

## 1. 本讲目标

本讲带你把项目「从源码跑起来」。学完后你应当能够：

- 读懂 `xmake.lua`，说清楚项目的依赖声明（`ncnn master`、`nlohmann_json`）与平台分支。
- 理解项目里 10 个 target 的划分：哪些是静态库、哪些是可执行文件，它们之间的依赖链长什么样。
- 用 `xmake build` / `xmake run` 单独构建并运行 `llm_ncnn_run`、`test_llm`、`benchllm` 等目标，并知道构建产物放在哪。
- 明白「构建」和「运行」是两件事——运行还需要在 `assets/` 下准备好模型目录。

承接上一讲（U1-L1）：我们已经知道 ncnn_llm 是「跑在 ncnn 之上的 C++ 推理运行时」。本讲回答下一个自然的问题：**这套 C++ 代码怎么被编译成一个能跑的程序**。

---

## 2. 前置知识

在进入 `xmake.lua` 之前，先建立三个基础概念。如果你已经熟悉构建系统，可以跳过本节。

### 2.1 什么是构建系统

源码（`.cpp`/`.h`）不能直接运行，需要经过「预处理 → 编译 → 链接」变成可执行文件或库。手写 `g++` 命令在文件一多时就不可行了，于是有了构建系统（Make / CMake / xmake 等）：你用一个配置文件描述「有哪些源文件、依赖什么、要生成什么」，构建系统负责推导出完整的编译命令。

ncnn_llm 用的是 **xmake**。xmake 的配置文件叫 `xmake.lua`，里面用 Lua 语法描述构建规则。它的两个核心动作是：

- `xmake build [target]`：编译。
- `xmake run <target> [args...]`：编译（如果需要）后运行某个目标。

### 2.2 静态库 vs 可执行文件

构建产物主要有两类：

| `set_kind(...)` | 产物 | 能否直接运行 |
| --- | --- | --- |
| `static` | 静态库（`.a` / `.lib`） | 否，只是「打包好的代码」 |
| `binary` | 可执行文件 | 是 |

ncnn_llm 的设计是：把核心运行时打成**静态库**（`ncnn_llm`），再让各个示例（`llm_ncnn_run` 等）作为**可执行文件**去链接它。这样多个示例共享同一份核心代码，既省编译时间，也保证行为一致——这正是上一讲提到的「跨模型族共享运行时」在工程上的体现。

### 2.3 target 与依赖

`target("名字")` 定义一个构建目标。target 之间通过 `add_deps("另一个 target")` 声明依赖：A 依赖 B，意味着链接 A 时会把 B 也带进来。`add_requires("包名")` 则声明**外部依赖包**（由 xmake 的包管理器下载/编译），这是获取 ncnn 的方式。

> 名词小贴士：**ncnn** 是腾讯开源的通用神经网络推理引擎（底层地基）；**ncnn_llm** 是本仓库（上层调度，负责分词、RoPE、KV cache、prefill/generate）。二者通过 `xmake.lua` 的 `add_requires("ncnn master")` 显式链接。

---

## 3. 本讲源码地图

本讲只围绕两个文件，它们都不算长，建议对照着读：

| 文件 | 作用 |
| --- | --- |
| [xmake.lua](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua) | 整个项目的构建配置：全局规则、依赖、平台分支、所有 target 定义 |
| [readme.md](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md) | 快速上手、CLI 用法、target 用途速查表（Other Examples） |

> 提示：`readme.md` 里「Other Examples」表格列出了 target 与用途，是构建产物的一张速查表；本讲会把它和 `xmake.lua` 的真实 target 定义逐一对账，**并指出二者之间的一处出入**（见 4.3）。

---

## 4. 核心概念与源码讲解

### 4.1 xmake 全局配置与平台分支

#### 4.1.1 概念说明

`xmake.lua` 最顶部的语句对**所有 target** 都生效，叫全局配置。它通常做三件事：声明规则（如调试/发布模式、生成 `compile_commands.json` 给 IDE）、设置语言标准、处理跨平台差异。ncnn_llm 要跑在 Windows / Linux / Android，还要支持 wasm（浏览器）和 Vulkan GPU，所以平台分支占了顶部不小的篇幅。

#### 4.1.2 核心流程

读 `xmake.lua` 顶部时的执行顺序（由 xmake 在配置阶段依次执行）：

1. 注册规则：生成 `.vscode/compile_commands.json`、声明 `mode.debug` / `mode.release` 两种构建模式。
2. 统一源码文件编码为 UTF-8（`set_encodings`）。
3. 设置语言标准为 **C++20 + C11**——注意 C++20，说明项目用了较新的语言特性（后续讲义会看到 `std::format` 之类）。
4. 按平台加分（wasm 用 emscripten 工具链；Windows 加 `NOMINMAX` 和 `/utf-8`；Windows/MinGW 链接 `user32`、`gdi32`）。

#### 4.1.3 源码精读

全局规则、编码与语言标准：

[xmake.lua:1-6](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L1-L6) —— 声明 compile_commands 自动更新与 debug/release 规则、强制 UTF-8 编码、设置 C++20/C11 语言标准。

```lua
add_rules("plugin.compile_commands.autoupdate", {outputdir = ".vscode"})
add_rules("mode.debug", "mode.release")

set_encodings("utf-8")

set_languages("c++20", "c11")
```

> 小知识：`mode.debug` / `mode.release` 让你可以用 `xmake f -m debug`（或 `release`）切换模式，默认是 release。

平台分支：wasm 需要切到 emscripten 工具链并设置运行时选项（断言、FS 导出、内存）：

[xmake.lua:8-30](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L8-L30) —— wasm 平台声明 emscripten 工具链、设置 `-sASSERTIONS=2` 等链接参数，并为 wasm64 架构配置大内存。这一段在文件里出现了**两次**（L8–18 与 L20–30 内容几乎相同），属于重复声明，xmake 不会报错，但提醒你：**真实的构建文件不一定「干净」**，读源码时要带着核对的眼光。

Windows / MinGW 分支：

[xmake.lua:32-48](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L32-L48) —— Windows 定义 `NOMINMAX`（避免 Windows 头里的 `min`/`max` 宏与 `std::min`/`std::max` 冲突）、加 `/utf-8` 编译选项；Windows/MinGW 额外链接系统库 `user32`、`gdi32`（图像/窗口相关）。这里同样存在重复块（L39–44 与 L46–48）。

> 这两段重复的真实价值是教学性的：它说明平台分支是「按需叠加」的，重复声明同一选项不会破坏构建，但维护时应当去重。

#### 4.1.4 代码实践

**实践目标**：理解「全局配置对所有 target 生效」并验证语言标准。

**操作步骤**：

1. 打开 `xmake.lua`，确认第 6 行的 `set_languages("c++20", "c11")`。
2. 在 `src/` 下任意一个 `.cpp` 文件里临时写一行用到 C++20 的代码（例如一个 `concept` 或 `std::span`），不修改 `xmake.lua`。
3. 执行 `xmake build llm_ncnn_run` 观察是否能编译通过。
4. 把 `xmake.lua` 第 6 行临时改成 `set_languages("c++17", "c11")`，再次构建，观察编译器报错。

**需要观察的现象**：C++20 特性在第一种情况下可用；改成 C++17 后编译器应报「未定义/不支持」类错误。

**预期结果**：语言标准确实由这一行全局控制。改完后请**还原 `xmake.lua`**，不要把改动留在仓库里（本讲要求只读源码）。

**待本地验证**：不同编译器对 C++20 子特性的支持程度不同，具体能否编译取决于你本地的编译器版本。

#### 4.1.5 小练习与答案

**练习 1**：`set_encodings("utf-8")` 解决了什么问题？去掉它会在哪种平台上最容易出问题？
**参考答案**：它强制 xmake 把所有源码当作 UTF-8 处理（包括生成的编译命令）。在中文 Windows（默认 GBK/代码页）上最容易出问题，源码里的中文字符串字面量可能被错误解码，导致编译警告或乱码。

**练习 2**：`NOMINMAX` 这个宏为什么在 Windows 上必须定义？
**参考答案**：Windows 头文件默认会定义 `min`/`max` 宏，它们会和 C++ 标准库的 `std::min`/`std::max`（以及泛型模板）冲突，导致编译错误。定义 `NOMINMAX` 可以禁用这两个宏。

---

### 4.2 依赖声明：ncnn master 与 nlohmann_json

#### 4.2.1 概念说明

这是本讲最重要的一节。ncnn_llm 依赖两个**外部包**：

- **ncnn（master 分支）**：推理引擎地基。注意它要求 master 分支，不是发布版——因为 master 才有 ncnn_llm 需要的最新特性（如完整的 kvcache 支持）。
- **nlohmann_json**：一个单头文件的 C++ JSON 库，用来读写 `model.json` 配置和工具调用相关的 JSON。

这两个包不是项目自带的，由 xmake 的包管理器（`xmake-repo`）自动下载并编译。`add_requires` 就是「声明需求」，`add_packages` 是「把需求告诉某个 target」。

#### 4.2.2 核心流程

依赖从声明到使用的流程：

1. 全局 `add_requires("ncnn master", {configs={vulkan=true}})`：告诉 xmake「整个项目需要 ncnn 的 master 版本，并且编译 ncnn 时要开启 vulkan」。
2. 全局 `add_requires("nlohmann_json")`：声明 JSON 库依赖。
3. 在具体 target 里 `add_packages("ncnn", "nlohmann_json")`：让该 target 能 `#include` 到这两个包的头文件、并链接它们的库。
4. `xmake build` 时，xmake 先检查本地有没有这两个包的缓存；没有就从 `xmake-repo` 下载源码、按 configs 编译、再链接进你的 target。

#### 4.2.3 源码精读

ncnn 的依赖声明（关键！）：

[xmake.lua:50-54](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L50-L54) —— 声明依赖 ncnn 的 master 版本，并通过 `configs.vulkan=true` 要求编译 ncnn 时开启 Vulkan 支持。没有这一行，GPU 推理（`--vulkan`）就无法工作。

```lua
add_requires("ncnn master", {
    configs = {
        vulkan=true
    }
})
```

> 重点：`add_requires("ncnn master", ...)` 里的 `master` 是版本/分支约束。如果你装的是某个发布版的 ncnn（如 `20231027`），版本不匹配可能导致接口缺失、链接失败。这也是 README「Requirements」明确写 `ncnn built from master` 的原因——见 [readme.md:64-67](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L64-L67)。

nlohmann_json 的依赖声明：

[xmake.lua:56-57](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L56-L57) —— `add_requires("nlohmann_json")`，这里出现了**重复的两次**（L56 和 L57 完全相同）。xmake 对重复声明是幂等的（不会装两次），但属于冗余。

公共头文件目录：

[xmake.lua:59](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L59) —— `add_includedirs("src/")`，让所有 target 都能直接 `#include "ncnn_llm_gpt.h"` 之类的核心头文件，而不必写完整相对路径。

> 包配置里的 `vulkan=true` 和运行时的 `--vulkan` 是两个层面的事：前者决定 ncnn 这个库「有没有」Vulkan 能力（编译期），后者决定某次推理「用不用」Vulkan（运行期）。两者都要开，GPU 推理才能生效——这会在 U2（基类 `create_option`）和 U8（Vulkan/线程配置）讲义里展开。

#### 4.2.4 代码实践

**实践目标**：直观感受 `add_requires` 拉取外部包的过程。

**操作步骤**：

1. 先清理 xmake 的包缓存（可选，确保你看到真实下载）：`xmake require --info ncnn` 查看 ncnn 包的元信息。
2. 执行 `xmake build llm_ncnn_run`。
3. 观察首次构建时的输出日志，重点看 xmake 是否打印 `downloading ncnn...` / `configuring ncnn...` / `building ncnn...` 之类字样。

**需要观察的现象**：首次构建会比后续慢很多，因为要先编译 ncnn（开启 vulkan 的 ncnn 编译较重）。

**预期结果**：xmake 把 ncnn(master) 和 nlohmann_json 缓存到本地，之后再次 `xmake build` 不再重复编译依赖。

**待本地验证**：首次编译 ncnn 的耗时取决于机器和是否启用 Vulkan SDK，可能从几分钟到十几分钟不等。如果机器没有 Vulkan SDK，开启 `vulkan=true` 的 ncnn 可能编译失败——此时需要先安装 Vulkan SDK。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `add_requires("ncnn master", {configs={vulkan=true}})` 里的 `master` 改成一个不存在的版本字符串，构建会怎样？
**参考答案**：xmake 在包管理阶段会报错，提示找不到该版本的 ncnn（找不到匹配的 package/版本），构建在拉依赖这一步就失败，根本进不到编译你的源码。

**练习 2**：为什么 `nlohmann_json` 不需要像 ncnn 那样写 `configs={...}`？
**参考答案**：nlohmann_json 是纯头文件库，没有需要开关的编译期特性（如是否启用某后端），所以不需要 configs；而 ncnn 有 Vulkan/平台/指令集等大量编译选项，必须通过 configs 告诉 xmake 怎么编译它。

---

### 4.3 target 划分与依赖链

#### 4.3.1 概念说明

整个项目由 10 个 target 组成，可以分成三层：

1. **基础库层**：`ncnn_tokenizer`（分词器静态库）、`ncnn_llm`（核心运行时静态库）。
2. **可执行示例层**：`llm_ncnn_run`（主聊天/VL）、`ocr_main`、`asr_main`、`embedding_main`、`clip_main`、`nllb_main`。
3. **工具层**：`benchllm`（benchmark）、`test_llm`（单元测试）。

核心思路：所有可执行文件都 `add_deps("ncnn_llm")`，复用同一份核心代码。新增一个示例，只需要再加一个 `binary` target 并依赖 `ncnn_llm` 即可。

#### 4.3.2 核心流程

依赖链（箭头表示「依赖」）：

```
ncnn_tokenizer (static, src/utils/tokenizer/*.cpp)
        ▲
        │ add_deps
        │
ncnn_llm (static, src/*.cpp + src/utils/*.cpp)  ──add_packages──▶ ncnn, nlohmann_json
        ▲
        │ add_deps
        │
 ┌──────┴───────────────────────────────────────┐
 │ llm_ncnn_run  ocr_main  asr_main              │   (都是 binary)
 │ embedding_main  clip_main  nllb_main          │
 │ benchllm  test_llm                            │
 └───────────────────────────────────────────────┘
```

构建时 xmake 会按依赖关系自动决定编译顺序：先 `ncnn_tokenizer`，再 `ncnn_llm`，最后才是各个 binary。

#### 4.3.3 源码精读

基础库 `ncnn_tokenizer`（分词器单独成库）：

[xmake.lua:61-63](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L61-L63) —— 把 `src/utils/tokenizer/*.cpp` 编成静态库 `ncnn_tokenizer`。分词器被独立出来，是因为它逻辑相对独立、文件较多（BPE/Unigram 等，见 U3 单元）。

核心运行时 `ncnn_llm`：

[xmake.lua:65-72](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L65-L72) —— 编译 `src/*.cpp` 与 `src/utils/*.cpp` 为静态库，依赖 `ncnn_tokenizer`，并引入 ncnn 与 nlohmann_json 两个包。这里同样有重复声明（`add_files("src/utils/*.cpp")` 出现两次、`add_packages(...)` 出现两次），属冗余但无害。

```lua
target("ncnn_llm")
    set_kind("static")
    add_files("src/*.cpp")
    add_files("src/utils/*.cpp")
    add_deps("ncnn_tokenizer")
    add_packages("ncnn", "nlohmann_json")
```

> 注意 `src/*.cpp` 只匹配 `src/` 下一层（不递归），而分词器在 `src/utils/tokenizer/` 子目录里，所以单独用 `ncnn_tokenizer` 收集、再 `add_deps` 进来。理解这个划分，后面读源码时就不会疑惑「为什么分词器要单独一个 target」。

主示例 `llm_ncnn_run`：

[xmake.lua:74-85](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L74-L85) —— 这是项目的「主入口」可执行文件，编译 `examples/llm_ncnn_run/*.cpp`，依赖核心库 `ncnn_llm`，并把运行目录（rundir）设为项目根目录。Windows/MinGW 下额外链接 `shell32`（命令行相关）。

```lua
target("llm_ncnn_run")
    set_kind("binary")
    add_includedirs("examples/")
    add_files("examples/llm_ncnn_run/*.cpp")
    add_deps("ncnn_llm")
    add_packages("ncnn", "nlohmann_json")
    ...
    set_rundir("$(projectdir)/")
```

benchmark `benchllm` 与测试 `test_llm`：

[xmake.lua:87-94](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L87-L94) —— `benchllm` 编译 `benchmark/benchllm.cpp`，且**运行目录被刻意设为 `assets/minicpm4_0.5b/`**，意味着 benchmark 默认从该模型目录读取权重，所以它「开箱即跑」的前提是 `assets/minicpm4_0.5b/` 下有完整模型。

[xmake.lua:96-103](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L96-L103) —— `test_llm` 编译 `tests/test_llm.cpp`，依赖核心库，运行目录为项目根。测试框架本身的实现见 `tests/test_framework.h`（在 U8-L3 详讲）。

其余示例 target（`nllb_main`、`embedding_main`、`clip_main`、`ocr_main`、`asr_main`）结构都一样：`binary` + 各自的单个 `.cpp` + `add_deps("ncnn_llm")` + `add_packages(...)`，唯一差异是 `set_rundir` 不同。例如 `ocr_main` 和 `asr_main` 在 Windows/MinGW 下也链接了 `shell32`——见 [xmake.lua:129-153](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L129-L153)。

> ⚠️ **对账提醒（重要）**：`readme.md` 的「Other Examples」表格列出了 `unigram_main` 作为 target（[readme.md:196-206](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L196-L206)），但 `xmake.lua` 里**并没有定义 `unigram_main` 这个 target**（同样没有 `bytelevelbpe_main`，尽管 `examples/unigram_main.cpp` 和 `examples/bytelevelbpe_main.cpp` 这两个源文件都存在）。也就是说 `xmake build unigram_main` 会失败（提示 unknown target）。这是文档与构建文件之间的一处真实出入，读源码时要以 `xmake.lua` 为准。如果你想跑这两个示例，需要自己照着别的 target 在 `xmake.lua` 里补一个定义（但本讲要求只读源码，先别改）。

#### 4.3.4 代码实践

**实践目标**：把 10 个 target 与它们的依赖关系整理清楚。

**操作步骤**：

1. 通读 `xmake.lua`，列出所有 `target(...)` 的名字及其 `set_kind`。
2. 对每个 binary target，标注它的 `add_deps` 和 `set_rundir`。
3. 用 `xmake --help` 或直接 `xmake`（不带参数）查看 xmake 是否列出所有 target。

**需要观察的现象**：xmake 列出的 target 名单应与 `xmake.lua` 里 `target(...)` 一致，且**不包含** `unigram_main`/`bytelevelbpe_main`。

**预期结果**：得到一张与 4.3.2 节类似的依赖关系表。

**待本地验证**：`xmake` 不带参数的默认行为（构建全部）在不同版本可能略有差异；以你本机 xmake 版本实际输出为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么要把分词器单独做成 `ncnn_tokenizer` 静态库，而不是直接放进 `ncnn_llm`？
**参考答案**：因为分词器源码在 `src/utils/tokenizer/` 子目录，而 `ncnn_llm` 用的是 `src/*.cpp`（不递归）。把它独立成 target 既清晰，也方便后续单独复用分词器（例如 U3 会单独讲分词器，届时可以只关注这个库）。

**练习 2**：`benchllm` 的 `set_rundir("$(projectdir)/assets/minicpm4_0.5b/")` 与其它 target 不同，会带来什么实际影响？
**参考答案**：`xmake run benchllm` 时，进程的当前工作目录会是 `assets/minicpm4_0.5b/`，所以 benchllm 里用相对路径读取 `model.json` 等文件时，会以这个目录为基准——不需要在命令行额外传 `--model` 路径。如果该目录不存在或没有模型，benchllm 就会因为找不到文件而失败。

**练习 3**：如果你想新增一个示例 `my_main.cpp`，最小改动是什么？
**参考答案**：在 `xmake.lua` 末尾仿照 `embedding_main` 加一个 `target("my_main")`，`set_kind("binary")`、`add_files("examples/my_main.cpp")`、`add_deps("ncnn_llm")`、`add_packages("ncnn","nlohmann_json")`、`set_rundir("$(projectdir)/")` 即可，核心代码会被 `ncnn_llm` 自动带进来。

---

### 4.4 构建、运行与产物

#### 4.4.1 概念说明

读懂 `xmake.lua` 之后，最后一步是真正动手。需要区分三件事：

- **配置（`xmake f ...`）**：选择平台、架构、模式。多数情况下不用手动配，xmake 会自动检测当前平台。
- **构建（`xmake build [target]`）**：编译，产出在 `build/` 下。
- **运行（`xmake run <target> [args]`）**：先确保构建好，再启动可执行文件。运行 LLM/VLM 还需要在 `assets/` 下放好模型目录。

#### 4.4.2 核心流程

一次完整的「克隆 → 构建 → 运行」流程（对应 README 的 Quick Start，[readme.md:62-124](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L62-L124)）：

```
git clone ... && cd ncnn_llm        # 1. 获取源码
xmake build llm_ncnn_run            # 2. 构建主示例（首次会自动编译 ncnn + nlohmann_json）
# 把模型目录放到 assets/qwen3_0.6b/  # 3. 准备模型（见 README「Download Models」）
xmake run llm_ncnn_run --model ./assets/qwen3_0.6b    # 4. 运行
```

构建产物路径遵循 xmake 的默认约定：`build/<平台>/<架构>/<模式>/`，例如 Linux 上 release 模式通常是 `build/linux/x86_64/release/llm_ncnn_run`。`xmake run` 会自动找到这个产物并执行，你通常不必手动定位它。

#### 4.4.3 源码精读

README 给出的单 target 构建与运行命令：

[readme.md:76-86](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L76-L86) —— `xmake build` 构建全部 target；`xmake build llm_ncnn_run` 只构建主示例（及其依赖 `ncnn_llm` → `ncnn_tokenizer`）。

[readme.md:105-124](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L105-L124) —— 运行 `llm_ncnn_run` 的几种形式：纯文本 `--model`、带线程数 `--threads`、启用 Vulkan `--vulkan --vulkan-device 0`、视觉语言 `--image`。这些命令行选项的解析逻辑在 U1-L4（CLI 入口）详讲，本讲只需知道怎么调用。

CLI 选项速查表：

[readme.md:126-136](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L126-L136) —— 列出 `--model`/`--threads`/`--vulkan`/`--vulkan-device`/`--image`/`--builtin-tools` 等运行时选项。注意它们都是**运行期**参数，和 `xmake.lua` 里**编译期**的 `configs.vulkan` 不是一回事（见 4.2.3 的提示）。

测试与 benchmark 的运行方式：

[readme.md:207-219](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L207-L219) —— `xmake build test_llm && xmake run test_llm` 跑单元测试；`xmake build benchllm && xmake run benchllm [loop_count] [num_threads] [powersave] [gpu_device] [cooling_down] [seqlen]` 跑性能测试（参数细节在 U8-L2 详讲）。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：亲手构建并运行主示例，再单独构建 test_llm 与 benchllm 两个 target，确认依赖链工作正常。

**操作步骤**：

1. **准备环境**：安装 xmake；准备一个从 master 编译的 ncnn（或让 xmake 自动拉取，见 4.2.4）。如需 GPU，安装 Vulkan SDK。
2. **构建主示例**：
   ```bash
   xmake build llm_ncnn_run
   ```
   记录构建产物路径（通常在 `build/linux/x86_64/release/` 下，文件名 `llm_ncnn_run`）。
3. **单独构建测试 target**：
   ```bash
   xmake build test_llm
   ```
4. **单独构建 benchmark target**：
   ```bash
   xmake build benchllm
   ```
5. **运行测试**（test_llm 不一定需要模型文件，很多是纯逻辑测试）：
   ```bash
   xmake run test_llm
   ```
6. **（可选）运行主示例**：先从 [Model Zoo](https://mirrors.sdu.edu.cn/ncnn_modelzoo/) 下载一个模型（如 `qwen3_0.6b`）放到 `assets/`，再：
   ```bash
   xmake run llm_ncnn_run --model ./assets/qwen3_0.6b
   ```

**需要观察的现象**：

- 步骤 2/3/4：xmake 先编译 `ncnn_tokenizer`，再编译 `ncnn_llm`，最后编译对应 binary；再次构建时会跳过未改动部分（增量编译）。
- 步骤 5：`test_llm` 打印测试结果（通过/失败计数）。
- 步骤 6：进入交互式聊天，输入 `Hello` 能得到回复（参考 README 的示例会话 [readme.md:138-143](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L138-L143)）。

**预期结果**：三个 target 都能成功构建；`test_llm` 能运行并报告测试结果。

**待本地验证**：

- 步骤 2 的产物确切路径取决于平台和模式，以本机 `build/` 目录实际为准。
- 步骤 6 是否能跑通，取决于是否下到了正确转换的 ncnn 模型目录；模型缺失或不匹配会报加载错误，这不是构建问题。
- benchllm 单独构建可以成功，但**运行**需要 `assets/minicpm4_0.5b/` 里有模型（因为它的 `set_rundir` 指向那里）；本实践只要求确认「构建」成功。

> 提醒：本讲要求**只读源码**。如果你在步骤中临时修改了 `xmake.lua` 来做实验，结束后请用 `git checkout xmake.lua` 还原。

#### 4.4.5 小练习与答案

**练习 1**：`xmake build`（不带 target 名）和 `xmake build llm_ncnn_run` 有什么区别？
**参考答案**：前者构建 `xmake.lua` 里定义的**全部** target（耗时较长）；后者只构建 `llm_ncnn_run` 及其依赖（`ncnn_llm`、`ncnn_tokenizer`），更快，适合开发主示例时反复用。

**练习 2**：为什么 `xmake run test_llm` 通常能直接跑，而 `xmake run llm_ncnn_run` 必须额外准备模型？
**参考答案**：`test_llm` 里很多测试是针对模板、工具定义等**纯逻辑**的（不加载模型权重），且对依赖模型的测试有 `has_model` 跳过机制（见 U8-L3）；而 `llm_ncnn_run` 是真正的推理入口，必须有模型目录才能工作。

**练习 3**：如果你只改了 `examples/llm_ncnn_run/main.cpp`，再次 `xmake build llm_ncnn_run`，xmake 会重新编译哪些 target？
**参考答案**：只需要重新编译 `llm_ncnn_run` 这个 binary（以及重新链接）。`ncnn_llm` 和 `ncnn_tokenizer` 没变，会被跳过——这就是分层静态库带来的增量编译收益。

---

## 5. 综合实践

**任务：为 `xmake.lua` 写一份「target 说明书」。**

把本讲学到的全部内容串起来，完成下面这张表（在自己笔记里填，不修改仓库）：

| target 名 | kind | 主要源文件 | add_deps | add_packages | set_rundir | 对应 README 用途 |
| --- | --- | --- | --- | --- | --- | --- |

要求：

1. 遍历 `xmake.lua` 中全部 10 个 target，逐行填写。
2. 标出哪些 target 之间存在重复声明（`add_files`/`add_packages` 写了两次）。
3. 在表外单独记录：README「Other Examples」里提到、但 `xmake.lua` **没有**对应 target 的示例名是什么（答案：`unigram_main`，以及虽未列入表格但源码存在的 `bytelevelbpe_main`）。
4. 选一个 binary target（建议 `llm_ncnn_run`），实际执行 `xmake build` 与 `xmake run`，把构建产物路径和运行结果补充到表后。

这张表就是你对本项目构建系统的「速查卡」，后续每讲涉及某个 target 时都可以回查。

---

## 6. 本讲小结

- ncnn_llm 用 xmake 构建，语言标准是 **C++20 / C11**，全局强制 UTF-8 编码，并用平台分支处理 wasm / Windows / MinGW 的差异。
- 外部依赖只有两个：**ncnn（master，开启 vulkan）** 和 **nlohmann_json**，通过 `add_requires` 声明、`add_packages` 引入到 target。
- 项目分为「静态库（`ncnn_tokenizer`、`ncnn_llm`）+ 可执行示例（`llm_ncnn_run` 等）+ 工具（`benchllm`、`test_llm`）」三层，所有 binary 都依赖核心库 `ncnn_llm`。
- `xmake build <target>` 单独构建，`xmake run <target>` 运行；构建产物在 `build/<平台>/<架构>/<模式>/` 下；运行 LLM/VLM 还需在 `assets/` 准备模型目录。
- 编译期的 `configs.vulkan`（决定 ncnn「有没有」Vulkan 能力）与运行期的 `--vulkan`（决定某次推理「用不用」）是两件事，都要开才能 GPU 推理。
- 真实构建文件存在冗余声明与文档出入（README 提到的 `unigram_main` 在 `xmake.lua` 里并未定义），读源码要以 `xmake.lua` 为准。

---

## 7. 下一步学习建议

构建跑通之后，建议按以下顺序继续：

1. **U1-L3 目录结构与源码地图**：从「怎么编译」过渡到「编译出来的东西分别在哪、干什么」，建立全仓库的源码地图。
2. **U1-L4 CLI 入口、选项解析与 UTF-8**：进入 `examples/llm_ncnn_run/main.cpp`，看本讲里那些 `--model`/`--threads`/`--vulkan` 选项到底是怎么被解析的，并理解 Windows GBK 环境下的 UTF-8 参数处理。
3. **U1-L5 模型目录与 model.json 配置体系**：理解「运行还需要模型目录」里的 `model.json` 长什么样、各字段如何被构造函数读取。

如果你对 GPU/性能感兴趣，可以提前跳读 U8-L1（Vulkan/线程/精度）和 U8-L2（benchmark），但它们依赖 U2 的运行时知识，建议学完 U2 再深入。
