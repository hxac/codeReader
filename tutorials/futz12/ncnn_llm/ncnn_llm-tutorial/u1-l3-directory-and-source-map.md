# 目录结构与源码地图

## 1. 本讲目标

学完本讲，你应该能够：

- 说出仓库里每个主要目录（`src/`、`src/utils/`、`examples/`、`export/`、`benchmark/`、`tests/`、`assets/`）的职责。
- 知道「核心运行时」「示例入口」「工具集」「导出脚本」「测试」「基准测试」分别放在哪里。
- 对照 `xmake.lua`，理解目录是如何被组织成一个个 target（构建目标）的，以及 target 之间的依赖关系。
- 在脑中建立一张「源码地图」：当我说到「分词器」「RoPE」「采样」「OCR」时，你能立刻定位到对应文件。
- 识别 README 的 Project Layout 与真实仓库之间的几处差异（README 略有滞后）。

本讲是后续所有源码精读讲义的「指北针」。先认路，再深入。

## 2. 前置知识

本讲是纯目录与构建组织的讲解，几乎不涉及算法细节。你只需要：

- 知道这是一个 **C++ 项目**，用 **xmake** 构建（参见 u1-l2《构建系统与运行方式》）。
- 大致了解「编译成静态库（static library）」和「编译成可执行文件（binary / executable）」的区别：
  - **静态库**：一堆编译好的 `.o` 打包成一个 `.a`，本身不能直接运行，只供别的程序链接。
  - **可执行文件（binary）**：有 `main()` 的程序，能直接跑起来。
- 了解 u1-l1 建立的项目定位：ncnn_llm 是建在 ncnn 之上的推理运行时，支持 LLM / VLM / OCR / ASR / 翻译 / 嵌入六大类模型。

> 术语提示：**target（目标）** 是 xmake 里的构建单位，一个 target 编译出一个产物（库或可执行文件）。**KV cache**、**tokenizer（分词器）**、**RoPE（旋转位置编码）** 等术语在后续讲义会逐步展开，本讲只需知道「它们对应的代码住在哪个目录」即可。

## 3. 本讲源码地图

本讲主要对照两个文件来建立地图：

| 文件 | 作用 |
| --- | --- |
| `readme.md` | 项目说明，其中有一节 **Project Layout**（目录布局）是本讲的主线参考。 |
| `xmake.lua` | 构建配置，定义了所有 target，揭示「目录 → 产物」的映射与依赖关系。 |

阅读这两份文件时要注意：README 的 Project Layout 是给人看的「速览」，会滞后；`xmake.lua` 是给构建系统看的「真相」，以它为准。

先看仓库的顶层目录全景（基于 `git ls-files` 的真实文件，而非 README 的简化版）：

```text
ncnn_llm/
├── src/                       # 核心运行时（C++ 源码 + 头文件）
│   ├── utils/                 # 工具集：分词器、RoPE、图像、prompt、自定义算子
│   │   └── tokenizer/         # 分词器子模块（单独成库）
│   ├── ncnn_llm_gpt.*         # LLM / VLM 运行时
│   ├── ncnn_llm_ocr.*         # OCR 运行时
│   ├── ncnn_llm_asr.*         # ASR（语音识别）运行时
│   ├── ncnn_embedding.*       # 嵌入运行时
│   ├── nllb_600m.*            # NLLB 翻译运行时
│   ├── ncnn_text_runtime.*    # 共享的文本解码助手
│   ├── sampling.*             # 采样（top-k / top-p / temperature）
│   └── ncnn_llm_base.h        # 基类与公共工具（KVCache 类型、Mat 工具函数）
├── examples/                  # 示例入口（带 main()）
│   ├── llm_ncnn_run/          # 主交互入口（聊天 / 视觉语言）
│   ├── ocr_main.cpp           # OCR 示例
│   ├── asr_main.cpp           # ASR 示例
│   ├── embedding_main.cpp     # 文本嵌入示例
│   ├── clip_main.cpp          # CLIP 图文嵌入示例
│   ├── nllb_main.cpp          # 翻译示例
│   ├── unigram_main.cpp       # Unigram 分词器示例（无 xmake target）
│   ├── bytelevelbpe_main.cpp  # BPE 分词器示例（无 xmake target）
│   └── utf8_args.h            # 跨平台 UTF-8 命令行参数处理（被多示例共享）
├── export/                    # 模型导出脚本（Python）
├── benchmark/                 # 性能基准测试入口
├── tests/                     # 单元测试
├── assets/                    # 本地模型目录与演示素材（模型需自行下载）
└── xmake.lua                  # 构建配置
```

> 注意：上面这份地图比 README 的 Project Layout 更完整。README 没有列出 `ncnn_llm_asr.*`、`nllb_600m.*`、`sampling.*`、`ncnn_llm_base.h` 以及 `utils/` 下的 `gdr.*`、`vision_rope.*`、`stb_image.h`。我们在下面的精读里会逐一补上。

## 4. 核心概念与源码讲解

### 4.1 从目录到 target：构建流向

#### 4.1.1 概念说明

仓库里的目录是「物理组织」，而 xmake 的 target 是「逻辑产物」。同一个目录的文件可能被打包进不同产物，多个目录的文件也可能合并进同一个产物。要建立源码地图，第一步是搞清楚「哪些文件被编译成什么，谁依赖谁」。

ncnn_llm 把代码组织成两层：

- **静态库层**：把核心运行时打包成两个静态库，供所有示例复用。
  - `ncnn_tokenizer`：只包含分词器。
  - `ncnn_llm`：包含核心运行时 + 工具集，并依赖 `ncnn_tokenizer`。
- **可执行层**：每个示例是一个 binary，链接 `ncnn_llm`（间接也拿到 `ncnn_tokenizer` 和 ncnn）。

这样设计的好处是：所有示例共享同一份核心代码，改一处运行时，所有示例同步生效（参见 u1-l1 的设计主线：跨模型族共享运行时）。

#### 4.1.2 核心流程（构建依赖链）

```text
src/utils/tokenizer/*.cpp ──► [ncnn_tokenizer] (静态库)
                                      │
src/*.cpp + src/utils/*.cpp ──► [ncnn_llm] (静态库) ──依赖──► ncnn_tokenizer
                                      │
            ┌─────────────┬───────────┴──────────┬─────────────┐
            ▼             ▼                      ▼             ▼
   examples/llm_     examples/ocr_main.cpp   examples/      benchmark/
   ncnn_run/*.cpp                        asr_main.cpp    benchllm.cpp
            │             │                      │             │
            ▼             ▼                      ▼             ▼
      [llm_ncnn_run]  [ocr_main]            [asr_main]     [benchllm]   ……（共 8 个 binary）
```

要点：

1. 分词器单独成库（`ncnn_tokenizer`），核心库 `ncnn_llm` 通过 `add_deps("ncnn_tokenizer")` 依赖它。
2. 所有 binary 都 `add_deps("ncnn_llm")`，因此只需写各自的 `main()` 即可复用全部运行时。
3. 顶层有 `add_includedirs("src/")`，所以代码里可以用 `#include "utils/rope_embed.h"` 这样的相对写法。

#### 4.1.3 源码精读

`xmake.lua` 里 target 的定义是建立地图的关键证据。先看全局的 include 目录设置（让 `src/` 成为头文件搜索根）：

[xmake.lua:59-63](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L59-L63) —— 设置 `src/` 为头文件包含目录，并定义分词器静态库 `ncnn_tokenizer`（只编译 `src/utils/tokenizer/*.cpp`）。

[xmake.lua:65-72](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L65-L72) —— 核心静态库 `ncnn_llm`：编译 `src/*.cpp` 与 `src/utils/*.cpp`，并通过 `add_deps("ncnn_tokenizer")` 依赖分词器库，`add_packages` 引入 ncnn 与 nlohmann_json。

[xmake.lua:74-85](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L74-L85) —— 主入口 `llm_ncnn_run`：binary，编译 `examples/llm_ncnn_run/*.cpp`，`add_deps("ncnn_llm")` 复用核心库，并用 `set_rundir` 把运行目录设为项目根。

[xmake.lua:96-103](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L96-L103) —— 测试 binary `test_llm`：编译 `tests/test_llm.cpp`，同样依赖 `ncnn_llm`。

> 横向对比：`ocr_main`、`asr_main`、`embedding_main`、`clip_main`、`nllb_main`、`benchllm` 的 target 写法几乎一致——各自一个 `.cpp`，`add_deps("ncnn_llm")`。这正说明了「核心逻辑在库里、示例只写入口」的分层。

#### 4.1.4 代码实践

1. **目标**：用 xmake 列出所有 target，验证地图里的产物数量。
2. **操作步骤**：在仓库根目录执行 `xmake show -t` 或直接 `xmake`。
3. **观察现象**：应能看到 `ncnn_tokenizer`、`ncnn_llm` 以及 8 个 binary（`llm_ncnn_run`、`benchllm`、`test_llm`、`nllb_main`、`embedding_main`、`clip_main`、`ocr_main`、`asr_main`）。
4. **预期结果**：共 10 个 target——2 个静态库 + 8 个可执行文件。
5. 如果环境里没装好 xmake 或 ncnn 导致命令跑不通，标注「待本地验证」，不影响理解地图。

#### 4.1.5 小练习与答案

- **练习 1**：如果新增一个示例 `examples/my_main.cpp`，最少要在 `xmake.lua` 写几行才能让它被编译？  
  **答案**：写一个 `target("my_main")`，`set_kind("binary")`、`add_files("examples/my_main.cpp")`、`add_deps("ncnn_llm")`，再加上需要的 `add_packages(...)`，参照 `nllb_main` 的 target 即可。

- **练习 2**：为什么 `ncnn_tokenizer` 要从 `src/utils/tokenizer/` 单独抽出来成库，而不是和 `ncnn_llm` 合并？  
  **答案**：分词器是一个相对独立、可单独复用的子模块；单独成库让边界清晰，也方便将来只链接分词器而不引入完整运行时（例如纯分词器示例）。

---

### 4.2 `src/`：核心运行时

#### 4.2.1 概念说明

`src/` 是整个项目的心脏。这里放的是「真正干活」的运行时代码：加载 ncnn 网络、维护 KV cache、做 prefill / generate、调用分词器与采样、处理图像与音频。`src/` 下的每个 `.h/.cpp` 对（或单个头文件）通常对应一个模态或一个子系统。

#### 4.2.2 核心流程（文件 → 职责映射）

下表把 `src/` 下的源码按「模态 / 子系统」分组，并标注它对应 u1-l1 里哪类模型：

| 文件 | 职责 | 对应能力 |
| --- | --- | --- |
| `ncnn_llm_base.h` | 基类与公共工具：定义 `KVCache` 类型、`ncnn::Mat` 工具函数（如 `argmax1d`、`add_mats_inplace`） | 所有模态共用 |
| `ncnn_llm_gpt.*` | LLM / VLM 运行时：构造、prefill、generate、ctx 多轮上下文、工具调用 | LLM、VLM |
| `ncnn_text_runtime.*` | 共享的文本解码助手：embed、decoder+KV、lm_head、token 选择 | 所有自回归模态共用 |
| `sampling.*` | 采样策略：softmax（带温度）、top-k、top-p、按概率抽样 | 解码共用 |
| `ncnn_llm_ocr.*` | OCR：图像 prefill（GLM-OCR / HunyuanOCR）+ 复用共享解码 | OCR |
| `ncnn_llm_asr.*` | ASR：pcm→mel→audio encoder→拼入文本 decoder 的端到端链路 | ASR |
| `ncnn_embedding.*` | 文本 / CLIP 图文嵌入 | Embedding |
| `nllb_600m.*` | NLLB encoder-decoder 翻译 | 翻译 |

> 这张表比 README 的 Project Layout 多了四行（`ncnn_llm_base.h`、`sampling.*`、`ncnn_llm_asr.*`、`nllb_600m.*`）——它们真实存在，但 README 没列。看源码以仓库为准。

#### 4.2.3 源码精读

先看基类头文件里的核心类型定义：

[src/ncnn_llm_base.h:14-20](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L14-L20) —— 定义 `KVCache` 类型别名（一组「key Mat + value Mat」的 pair），以及把 `int` 向量转成 `ncnn::Mat` 的工具函数。这是贯穿全项目的 KV cache 数据结构。

再看 ASR 运行时的总览注释（一个文件就能看清一条模态链路）：

[src/ncnn_llm_asr.h:19-20](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.h#L19-L20) —— 用一行注释概述 Qwen3-ASR 的端到端管线：`pcm(16k mono) → mel(STFT) → audio_conv → audio_encoder → 拼入文本 decoder`。

#### 4.2.4 代码实践

1. **目标**：用一次「定位练习」熟悉 `src/` 的文件命名规律。
2. **操作步骤**：在编辑器里打开 `src/`，按下表左列去找对应文件，打开头文件看一眼顶部注释或类名。
3. **观察现象**：你会发现「模态名 = 文件名前缀」的规律：`ncnn_llm_gpt`（文本/视觉语言）、`ncnn_llm_ocr`（OCR）、`ncnn_llm_asr`（ASR）、`ncnn_embedding`（嵌入）、`nllb_600m`（翻译）。
4. **预期结果**：能凭文件名判断它服务哪类模型。
5. 命名规律为「待本地验证」也无妨，重点是建立直觉。

#### 4.2.5 小练习与答案

- **练习 1**：采样相关代码在哪个文件？它被几个模态共用？  
  **答案**：在 `src/sampling.*`。由于所有自回归模态（LLM/VLM/OCR/ASR/翻译）都需要从 logits 选下一个 token，它被广泛共用。

- **练习 2**：`KVCache` 这个类型定义在哪个头文件？为什么放在「基类」里？  
  **答案**：定义在 `src/ncnn_llm_base.h`。因为它是最基础的公共数据结构，放在基类头里可以让所有派生类和工具函数统一引用，避免重复定义。

---

### 4.3 `src/utils/`：工具集（分词器 / RoPE / 图像 / prompt / 自定义算子）

#### 4.3.1 概念说明

`src/utils/` 是核心运行时的「工具箱」。运行时主体（`ncnn_llm_gpt` 等）负责调度，而真正干「具体某一件小事」的代码被拆到这里：把文本切成 token、生成位置编码、读图、构造对话模板、实现 ncnn 没有内置的算子。把工具拆出来的好处是：单一职责、易于测试、可被多个模态复用。

#### 4.3.2 核心流程（子目录与文件分组）

```text
src/utils/
├── tokenizer/          # 分词器（单独成库 ncnn_tokenizer）
│   ├── tokenizer_types.h    # 特殊令牌配置等公共类型
│   ├── bpe_tokenizer.*      # BPE / BBPE 分词器
│   └── unigram_tokenizer.*  # Unigram（SentencePiece 风格）分词器
├── rope_embed.*        # 文本 RoPE（含 NTK/YaRN/LongRoPE 长上下文变体）
├── vision_rope.*       # 视觉 RoPE（mRoPE 2D）
├── image_utils.*       # 图像加载与 patch 切分工具
├── stb_image.h         # 第三方单头图像解码库（被 image_utils 使用）
├── prompt.*            # 对话模板（ChatML / YouTu）与消息拼接
└── gdr.*               # 自定义 ncnn 算子：GatedDeltaRule / ShortConv
```

对应关系：

| 子模块 | 解决的问题 |
| --- | --- |
| `tokenizer/` | 把字符串变成 token id，或把 token id 变回字符串 |
| `rope_embed` / `vision_rope` | 给 token 生成旋转位置编码（文本单轴 / 视觉多轴） |
| `image_utils` + `stb_image` | 把图片文件读成 ncnn::Mat，并切成 patch 网格 |
| `prompt` | 把多轮对话拼成模型能理解的输入字符串 |
| `gdr` | 实现 Qwen3.5 混合架构需要的、ncnn 未内置的算子 |

#### 4.3.3 源码精读

分词器单独成库的证据（注意它只编译 `src/utils/tokenizer/*.cpp`）：

[xmake.lua:61-63](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L61-L63) —— `ncnn_tokenizer` 这个静态库的文件范围被严格限定在 `src/utils/tokenizer/*.cpp`，与核心库 `ncnn_llm` 分开。

自定义 ncnn 算子的类定义（说明 `gdr.*` 实现的是 ncnn 的 `Layer` 子类）：

[src/utils/gdr.h:7-15](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/gdr.h#L7-L15) —— `GatedDeltaRule` 继承自 `ncnn::Layer`，重写 `forward`，是一个注册进 ncnn 网络的自定义算子。

视觉 RoPE 的入口签名：

[src/utils/vision_rope.h:7-12](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/vision_rope.h#L7-L12) —— `generate_vision_rope_cache_2d` 按 patch 高/宽、空间合并大小生成 2D 旋转位置编码，服务于视觉编码器。

> 提示：`src/utils/` 被 `ncnn_llm` 库整体编译（`xmake.lua` 第 68 行 `add_files("src/utils/*.cpp")`），而 `tokenizer/` 子目录则被 `ncnn_tokenizer` 库编译。两者最终都通过依赖链汇入所有 binary。

#### 4.3.4 代码实践

1. **目标**：理解「工具被谁调用」，建立调用直觉。
2. **操作步骤**：在仓库里搜索某个工具的引用。例如用编辑器全局搜索 `rope_embed.h`，看哪些 `src/*.cpp` include 了它。
3. **观察现象**：你会发现 `rope_embed` 被 LLM/VLM 主运行时引用，`image_utils` 被视觉相关运行时引用，`prompt` 被示例入口引用。
4. **预期结果**：能说出「图像工具服务视觉模态、分词器服务所有模态」这样的复用关系。
5. 若搜索工具不可用，可改为直接打开文件看 `#include` 关系。

#### 4.3.5 小练习与答案

- **练习 1**：`stb_image.h` 是项目自己写的吗？为什么放在 `utils/`？  
  **答案**：不是，它是业界知名的第三方单头图像解码库（stb 系列）。放在 `utils/` 是因为它被 `image_utils` 直接 include 使用，作为图像加载的底层依赖。

- **练习 2**：为什么 `tokenizer/` 单独成库，而 `rope_embed`、`prompt` 等没有？  
  **答案**：分词器体量大、独立性强、且存在「只想要分词器」的复用场景（如纯分词器示例），因此单独成库更合理；其余工具更贴近运行时主体，合并进 `ncnn_llm` 即可。

---

### 4.4 `examples/`：示例入口

#### 4.4.1 概念说明

`examples/` 下放的都是带 `main()` 的可执行入口。它们「薄」——核心逻辑都在 `ncnn_llm` 库里，示例只负责解析命令行参数、构造模型对象、调用推理 API、打印结果。每个示例对应 README 里的一类用法（聊天、OCR、嵌入、翻译……）。

#### 4.4.2 核心流程（示例 → 用法 → 是否有 target）

| 示例文件 | 用途 | 有 xmake target？ |
| --- | --- | --- |
| `llm_ncnn_run/`（含 `main.cpp` 等） | 统一聊天 / 视觉语言 CLI（主入口） | ✅ `llm_ncnn_run` |
| `ocr_main.cpp` | GLM-OCR 图像转文字 | ✅ `ocr_main` |
| `asr_main.cpp` | Qwen3-ASR 语音识别（读 wav） | ✅ `asr_main` |
| `embedding_main.cpp` | 文本嵌入 | ✅ `embedding_main` |
| `clip_main.cpp` | CLIP 图文嵌入 | ✅ `clip_main` |
| `nllb_main.cpp` | NLLB 翻译 | ✅ `nllb_main` |
| `unigram_main.cpp` | Unigram 分词器演示 | ❌ 无 target |
| `bytelevelbpe_main.cpp` | BPE 分词器演示 | ❌ 无 target |

> 重要差异：README 的 Other Examples 表格里列了 `unigram_main`，但 `xmake.lua` 里**并没有定义** `unigram_main` 这个 target（u1-l2 已指出）。同样，`bytelevelbpe_main.cpp` 也没有 target。这两个文件是「存在源码但默认不构建」的分词器小工具，需要时可手动编译或临时加 target。反过来，`asr_main` 有 target，却没出现在 README 的 Other Examples 表里。**这再次说明：看源码和 `xmake.lua`，别只看 README。**

另外，`llm_ncnn_run/` 是唯一一个「目录」形式的示例（多个文件协作），其余示例都是单文件。它内部还有 `options.*`（命令行选项）、`cli_runner.*`（多轮对话循环）、`tools.*`（工具调用）、`json_utils.*`（消息解析）等子模块。

还有一个跨示例共享的头文件：`examples/utf8_args.h`，它位于 `examples/` 顶层（不在 `llm_ncnn_run/` 里），被 `llm_ncnn_run`、`ocr_main`、`asr_main` 三个示例 include，用于跨平台 UTF-8 命令行参数处理。

#### 4.4.3 源码精读

主入口目录的 target 定义：

[xmake.lua:74-78](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L74-L78) —— `llm_ncnn_run` 编译整个 `examples/llm_ncnn_run/*.cpp` 目录，并依赖 `ncnn_llm`。

README 的示例用法总表（与 target 对照阅读）：

[readme.md:196-205](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L196-L205) —— README 的 Other Examples 表。注意它列了 `unigram_main`（实际无 target）、漏了 `asr_main`（实际有 target）。

ASR 示例入口引用共享头文件：

[examples/asr_main.cpp:8-9](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/asr_main.cpp#L8-L9) —— `asr_main.cpp` include 了 `ncnn_llm_asr.h`（核心运行时）和 `utf8_args.h`（共享的示例工具），印证示例「薄入口 + 复用核心库」的模式。

#### 4.4.4 代码实践

1. **目标**：验证「示例文件 ↔ target」的对应关系，并发现 README 与实际的不一致。
2. **操作步骤**：
   - 打开 `xmake.lua`，数一下 `target("...")` 里 `set_kind("binary")` 的有几个，分别叫什么。
   - 打开 `examples/` 目录，列出所有 `*_main.cpp`。
   - 把两份清单做对照。
3. **观察现象**：`examples/` 里有 8 个 `*_main.cpp`（含 `llm_ncnn_run/main.cpp`），但只有 6 个单文件示例 + 1 个目录示例 = 7 个有 target 的入口；`unigram_main.cpp` 与 `bytelevelbpe_main.cpp` 没有 target。
4. **预期结果**：列出一张「示例文件 → 是否有 target」的表（即上面的 4.4.2 表）。
5. 这是纯阅读型实践，无需运行即可完成。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 `llm_ncnn_run/` 是一个目录，而其他示例是单文件？  
  **答案**：因为主入口功能最复杂——需要选项解析、多轮对话循环、工具调用、消息解析等多个子模块，单文件装不下；其他示例功能单一，一个 `main()` 足够。

- **练习 2**：`utf8_args.h` 为什么放在 `examples/` 顶层而不是 `llm_ncnn_run/` 里？  
  **答案**：因为它被多个示例（`llm_ncnn_run`、`ocr_main`、`asr_main`）共享，放在 `examples/` 顶层体现其「公共示例工具」的定位，便于各示例 include。

---

### 4.5 `export/`、`benchmark/`、`tests/`、`assets/`：辅助目录

#### 4.5.1 概念说明

除了核心运行时和示例，仓库还有四个辅助目录，分别承担「模型从哪来」「跑多快」「对不对」「素材在哪」的职责：

- `export/`：Python 脚本，把 HuggingFace / PyTorch 模型转成 ncnn 能用的 `.param/.bin` 和分词器文件。**注意**：README Roadmap 明确说导出脚本可能滞后，应以仓库最新版本为准（部分细节待确认）。
- `benchmark/`：性能基准，只有一个 `benchllm.cpp`，用来测 tokens/s。
- `tests/`：单元测试，含一个极简测试框架 `test_framework.h` 和测试用例 `test_llm.cpp`。
- `assets/`：本地模型目录与演示素材。仓库里只跟踪了一个分词器数据文件 `assets/mclip_unigram_tokenizer.txt`；真正的模型目录（如 `qwen3_0.6b/`）需要按 README 指引从镜像下载后放进 `assets/`。

#### 4.5.2 核心流程（各目录的产物与去向）

```text
export/*.py  ──产出──►  *.ncnn.param / *.ncnn.bin / vocab.txt / merges.txt / model.json
                                   │
                                   ▼
                          放进 assets/<模型名>/  （手动下载或导出）
                                   │
                                   ▼
examples/*_main + tests/test_llm + benchmark/benchllm  运行时读取这些文件
```

#### 4.5.3 源码精读

导出脚本之一：从 HuggingFace tokenizer 抽取 vocab 与 merges：

[export/extract_tokenizer.py:5-20](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/export/extract_tokenizer.py#L5-L20) —— `extract_tokenizer` 读取 HuggingFace 的 `tokenizer.json`，写出 `vocab.txt` 与 `merges.txt`，供 `bpe_tokenizer` 使用。这解释了 `assets/` 模型目录里分词器文件的来源。

基准测试的 target（运行目录被设成一个具体模型目录）：

[xmake.lua:87-94](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L87-L94) —— `benchllm` 编译 `benchmark/benchllm.cpp`，并把 `set_rundir` 设为 `assets/minicpm4_0.5b/`，说明基准默认会跑这个模型（需提前下载）。

README 对导出脚本滞后的提醒：

[readme.md:286-294](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L286-L294) —— Roadmap 说明：随着运行时演进，旧的导出脚本可能过时，应以最新的模型示例和 `model.json` 为准。

#### 4.5.4 代码实践

1. **目标**：理清「模型文件从哪来、被谁用」。
2. **操作步骤**：
   - 打开 `export/`，阅读 4 个脚本的开头注释，猜测各自用途。
   - 打开 `assets/`，看仓库实际跟踪了哪些文件（`git ls-files assets/`）。
   - 对照 README 的 Quick Start，确认模型目录需要手动下载。
3. **观察现象**：`assets/` 仓库里只有 `mclip_unigram_tokenizer.txt`，没有真正的模型权重——权重需要下载。
4. **预期结果**：能复述「export 产出模型文件 → 放进 assets → 示例/测试/基准读取」这条链路。
5. 导出脚本的具体 API 细节以仓库最新版本为准，必要时标注「待确认」。

#### 4.5.5 小练习与答案

- **练习 1**：`benchmark/`、`tests/` 都依赖 `ncnn_llm` 吗？为什么？  
  **答案**：是的，二者 target 都 `add_deps("ncnn_llm")`。因为基准要跑真实推理、测试要测运行时行为，都需要核心库。

- **练习 2**：为什么 `assets/` 里几乎没有文件？  
  **答案**：模型权重文件很大，不适合放进 git 仓库；项目通过镜像提供下载，使用者按需放进 `assets/`。仓库只跟踪了体积小、且被测试/示例直接使用的分词器数据文件。

## 5. 综合实践

**任务：为每个核心目录找一个「代表文件」并说明职责。**

这是本讲的主实践，目标是把目录地图真正内化。请按下表完成：

| 目录 | 找一个代表文件 | 用一两句话说明它的职责 |
| --- | --- | --- |
| `src/` | （示例）`src/ncnn_llm_gpt.h` | LLM/VLM 运行时的主类声明，定义 prefill/generate 等接口 |
| `src/utils/` | ？ | ？ |
| `src/utils/tokenizer/` | ？ | ？ |
| `examples/` | ？ | ？ |
| `examples/llm_ncnn_run/` | ？ | ？ |
| `export/` | ？ | ？ |
| `benchmark/` | ？ | ？ |
| `tests/` | ？ | ？ |

操作步骤：

1. 对照本讲的「源码地图」与 4.x 各节的文件表，为每个目录挑选一个你认为最具代表性的文件。
2. 打开该文件，阅读顶部注释或主要类名/函数名。
3. 用一两句中文写下它的职责（不要复制本讲的原文，用自己的话）。
4. 额外挑战：在 `xmake.lua` 里找到这个文件所属的 target，确认它被编译进哪个产物。
5. 把成品整理成一张「目录 → 代表文件 → 职责 → 所属 target」的四列表，作为你个人的源码地图速查表。

**预期结果**：完成表格后，当你听到「分词器在哪儿」「基准怎么跑」「OCR 入口是哪个文件」等问题时，能不假思索地回答。

## 6. 本讲小结

- 仓库以 `src/`（核心运行时）、`examples/`（示例入口）、`src/utils/`（工具集）、`export/`（导出脚本）、`benchmark/`（基准）、`tests/`（测试）、`assets/`（模型素材）七个目录组织。
- xmake 把代码组织成「两个静态库 + 八个 binary」：`ncnn_tokenizer`（分词器）和 `ncnn_llm`（核心运行时）是库，所有示例都依赖 `ncnn_llm`。
- `src/utils/tokenizer/` 单独成库，其余工具（RoPE、图像、prompt、自定义算子 gdr）合入 `ncnn_llm`。
- README 的 Project Layout 会滞后：它漏列了 `ncnn_llm_asr.*`、`nllb_600m.*`、`sampling.*`、`ncnn_llm_base.h`、`utils/gdr.*`、`utils/vision_rope.*` 等；README 的示例表也和 `xmake.lua` 的 target 不完全一致（`unigram_main` 无 target、`asr_main` 有 target）。**建立地图以 `xmake.lua` 和实际文件为准。**
- 命名规律很强：「模态名 = 文件名前缀」（`ncnn_llm_gpt` / `ncnn_llm_ocr` / `ncnn_llm_asr` / `ncnn_embedding` / `nllb_600m`）。
- 模型权重不在仓库里，需从镜像下载放进 `assets/`；导出脚本（`export/`）负责把 HuggingFace 模型转成 ncnn 格式。

## 7. 下一步学习建议

建立好地图后，建议按以下顺序深入：

1. **u1-l4《CLI 入口、选项解析与 UTF-8》**：从 `examples/llm_ncnn_run/main.cpp` 进入，看一个真实的 `main()` 如何调用运行时——这是把「地图」变成「代码」的第一步。
2. **u1-l5《模型目录与 model.json 配置体系》**：理解 `assets/<模型>/model.json` 如何描述一个模型，把「素材目录」和「运行时构造」连起来。
3. 之后进入 U2《LLM 推理主链路》，从 `src/ncnn_llm_gpt.cpp` 的 prefill / generate 开始精读核心源码——届时你会感谢自己先备好了这张地图。

> 阅读源码时，养成「先查 `xmake.lua` 确认 target、再打开文件」的习惯，能避免被滞后的 README 误导。
