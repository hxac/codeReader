# 工具链与端到端示例：从 MLIR 到 cubin

## 1. 本讲目标

学完本讲，你应当能够：

- 画出 CUDA Tile IR 从一段文本 MLIR 到在 GPU 上真正执行的完整数据流图，并指出每个阶段对应的工具与文件格式。
- 用 `cuda-tile-translate` 把一段 `.mlir` 编译成 `.tilebc` 字节码，并理解 `--bytecode-version`、`--mlir-to-cudatilebc`、`--no-implicit-module` 等参数的作用。
- 区分 AoT（`tileiras` 提前编译出 cubin）与 JIT（驱动直接加载 `.tilebc`）两条执行路径，并知道它们复用同一个 `cuLaunchKernel` 启动 API。
- 读懂 `cuda-tile-translate` 这个工具的入口源码，理解它「先注册、再交给 MLIR 主循环」的极简结构，以及翻译注册 `mlir-to-cudatilebc` / `cudatilebc-to-mlir` 是如何挂接进来的。

## 2. 前置知识

承接上一讲 u1-l2（仓库结构与 CMake 构建系统）。我们已经知道：

- 仓库用 CMake 构建，开启 `CUDA_TILE_ENABLE_TOOLS=ON`（默认开）时会构建出 `cuda-tile-translate` 等命令行工具。
- `cuda-tile-tblgen` 必须最先构建，因为它把 `.td` 翻译成 `.inc` 胶水代码，之后 include/lib 才能引用。

本讲需要补充几个名词的直觉：

- **MLIR 文本格式（`.mlir`）**：人能读写的中间表示文本。一段 `.mlir` 就是一个「程序」，里面有模块、函数、操作、类型。
- **字节码（bytecode / `.tilebc`）**：把同一个程序用紧凑的二进制编码表示。对机器而言更小、解析更快，也是驱动真正能加载的格式。可以类比成 `.c` 源码（文本）和 `.o` 目标文件（二进制）的关系——但注意 `.tilebc` 还不是机器码。
- **cubin（`.cubin`）**：CUDA 的 GPU 机器码二进制（Compiled Binary）。它针对具体 GPU 架构（如 `sm_100`），可以直接被 CUDA 驱动加载执行。
- **AoT（Ahead-Of-Time，提前编译）**：在部署/构建阶段就把字节码编译成 cubin，运行时直接加载，启动快。
- **JIT（Just-In-Time，即时编译）**：运行时由 CUDA 驱动把字节码当场编译成机器码再执行，灵活但首次启动略慢。
- **CUDA Driver API**：`cuModuleLoad`、`cuLaunchKernel` 等一组 C 接口，用于加载内核、分配显存、启动内核。它比更常见的 Runtime API（`cudaMalloc` 等）更底层。

一句话建立心智模型：

> 文本 MLIR 是「源」，`.tilebc` 字节码是「搬运格式」，cubin 是「最终机器码」。`cuda-tile-translate` 负责 MLIR↔字节码的转换，`tileiras` 或 CUDA 驱动负责把字节码变成能在 GPU 上跑的东西。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [README.md](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L213-L340)（213–340 行） | 端到端 print 示例的完整说明、MLIR 程序、C++ host 程序与执行步骤，是本讲的「剧本」 |
| [tools/cuda-tile-translate/cuda-tile-translate.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-translate/cuda-tile-translate.cpp#L18-L28) | 工具入口 `main`，只负责注册选项与翻译、再调用 MLIR 主循环 |
| [lib/Bytecode/Translation/BytecodeTranslation.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Translation/BytecodeTranslation.cpp#L35-L85) | 注册两个翻译：`mlir-to-cudatilebc`（序列化）与 `cudatilebc-to-mlir`（反序列化） |
| [lib/Bytecode/Common/CommandLineOptions.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/CommandLineOptions.cpp#L73-L123) | `--bytecode-version`、`--list-versions`、`--Wunsupported-hints` 等命令行选项的定义与默认值 |
| [lib/Bytecode/Common/Version.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/Version.cpp#L39-L64) | 字节码版本常量：兼容版 13.1、当前版 13.3、最低支持 13.1 |

## 4. 核心概念与源码讲解

### 4.1 从文本 MLIR 到 GPU 执行：端到端全景

#### 4.1.1 概念说明

这个模块回答一个最朴素的问题：我写了一段 Tile IR，它是怎么变成 GPU 上的一次实际计算的？

CUDA Tile IR 的「程序」有三种存在形态：

1. 文本 `.mlir`：人写的源。
2. 字节码 `.tilebc`：紧凑二进制，是工具之间、以及驱动加载的标准格式。
3. cubin `.cubin`：GPU 机器码，针对具体 SM 架构。

在这三者之间流转的就是本讲的工具链。重要的是：**字节码阶段之后有两条等价路径**通往 GPU，它们最终用的启动代码几乎一样。

#### 4.1.2 核心流程

整体流程可以用下面这条链概括：

```
example.mlir ──cuda-tile-translate──> example.tilebc
                                         │
                    ┌────────────────────┴────────────────────┐
                    │ (AoT)                                    │ (JIT)
          tileiras --gpu-name sm_100                 驱动直接 cuModuleLoad
                    │                                    example.tilebc
            example.cubin                                     │
                    └──────────────────┬─────────────────────┘
                           cuModuleLoad + cuModuleGetFunction
                                   cuLaunchKernel(...)
                                       GPU 执行
```

四步：

1. **MLIR → 字节码**：`cuda-tile-translate example.mlir --bytecode-version=13.1 --mlir-to-cudatilebc --no-implicit-module -o example.tilebc`
2. **（可选）字节码 → cubin**：`tileiras --gpu-name sm_100 example.tilebc -o example.cubin`（AoT）
3. **host 加载**：`cuModuleLoad("example.cubin")`，或 JIT 时直接 `cuModuleLoad("example.tilebc")`
4. **启动**：`cuLaunchKernel(...)`

注意第 2、3 步的 AoT 与 JIT 之分只影响「字节码何时被编译成机器码」，启动内核的 API 完全相同。

#### 4.1.3 源码精读

先看 README 给出的这段最小可运行内核（这段代码我们在后续进阶单元会逐行拆解，这里只需建立整体印象）：

[README.md:233-246](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L233-L246) — 定义 `example_kernel`：打印一行字符串，从传入的指针 tile 构造 128 个元素的地址，加载 128 个 f32，再打印出来。

几个直观要点：

- `cuda_tile.module @example_module { ... }` 是顶层模块容器；`entry @example_kernel(...)` 是入口（可被驱动启动的内核），它接受一个指针 tile 参数 `%data_pr : tile<ptr<f32>>`。
- `iota` 生成一段 0..127 的索引序列（`tile<128xi32>`）。
- `reshape` + `broadcast` + `offset` 三连把单个指针「广播」成 128 个连续地址的 tile。
- `load_ptr_tko weak` 从这 128 个地址读出 128 个 float。
- `print_tko` 是带 token 顺序的打印操作（`tko` = token-ordered）。

再看 host 程序中真正启动内核的关键片段：

[README.md:296-316](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L296-L316) — 用 `cuModuleLoad` 装载 cubin（或 `.tilebc`）、取出 `example_kernel` 函数句柄、分配并拷贝输入数据、用 `cuLaunchKernel` 启动。注释里明确指出：**grid 维度决定 Tile Grid 大小，block 维度必须 `(1,1,1)`、共享内存必须 `0`**——这与普通 CUDA 内核不同，因为 Tile IR 自己管理线程划分。

最后是 README 的执行步骤清单，把它当作本讲的「操作手册」：

[README.md:328-334](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L328-L334) — 四步：translate 出字节码、（AoT）tileiras 出 cubin、g++ 编译 host、运行 `./example`。

#### 4.1.4 代码实践

实践目标：在不依赖 GPU 的前提下，先把工具链「前半段」跑通并验证产物。

操作步骤：

1. 按上一讲配置并构建项目，确认 `build/bin/cuda-tile-translate` 已生成。
2. 把 README 的 `example.mlir`（233–246 行）存成文件。
3. 运行：
   ```bash
   build/bin/cuda-tile-translate example.mlir \
     --bytecode-version=13.1 --mlir-to-cudatilebc --no-implicit-module \
     -o example.tilebc
   ```
4. 用 `xxd example.tilebc | head` 查看前若干字节（字节码格式在 u7 单元详解，现在只需看到它确实是二进制）。

需要观察的现象：

- 第 3 步无报错退出、生成 `example.tilebc`。
- 第 4 步能看到非文本的二进制内容（不是纯 ASCII）。

预期结果：生成一个非空的 `.tilebc` 文件。

> 说明：从 `tileiras`、`cuLaunchKernel` 一直到打印 128 个浮点数的完整运行，需要真实 GPU 与 CUDA Toolkit 13.1+ 环境，本环境无法验证，标记为「待本地验证」。README 第 337–340 行给出了预期的 128 个浮点数终端输出，可供本地核对。

#### 4.1.5 小练习与答案

**练习 1**：为什么 README 在 `cuLaunchKernel` 里把 block 维度设成 `(1,1,1)`、共享内存设成 `0`？

**答案**：因为 Tile IR 自己负责把计算划分到线程与共享内存，外部驱动层不再需要、也不应再指定这些；强行设置会与 Tile IR 的内部调度冲突。

**练习 2**：如果把第 1 步的 `--bytecode-version=13.1` 去掉会怎样？

**答案**：会使用当前版（13.3）写出字节码。它能在更新的工具/驱动上工作，但可能无法被只支持 13.1 的旧驱动加载。所以面向广泛兼容性时显式指定 13.1（兼容版）更稳妥。（详见 4.3。）

### 4.2 cuda-tile-translate 工具入口与命令行注册

#### 4.2.1 概念说明

`cuda-tile-translate` 是这个项目最常用的「瑞士军刀」：它本质上是 MLIR 自带的 `mlir-translate` 主程序，只不过在启动前多注册了几个 CUDA Tile 专用的「翻译（Translation）」和命令行选项。

理解它的关键是 MLIR 的 Translation 机制：一个「翻译」就是一对 `(名字, 处理函数)`。注册后，命令行上就可以用 `--mlir-to-cudatilebc` 这样的名字来选择要做哪种转换。**工具本身不写转换逻辑，转换逻辑在被注册的回调里。**

#### 4.2.2 核心流程

`main` 的执行只有两件事：

```
1. 依次注册若干命令行选项 + 翻译
2. 把 argc/argv 交给 mlir::mlirTranslateMain，由它解析参数、分派到对应翻译
```

注册顺序很关键：**必须在解析命令行之前完成所有注册**，否则 `--bytecode-version` 这样的选项和 `--mlir-to-cudatilebc` 这样的翻译都不会被认识。

#### 4.2.3 源码精读

整个工具的 `main` 只有十行出头，是「极简入口」的典型：

[tools/cuda-tile-translate/cuda-tile-translate.cpp:18-28](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-translate/cuda-tile-translate.cpp#L18-L28) — 五个注册调用后直接进入 MLIR 主循环。

五个注册调用各司其职：

| 注册函数 | 作用 |
|---|---|
| `registerTileIRBytecodeVersionOption()` | 注册 `--bytecode-version` |
| `registerTileIROptimizationHintsOptions()` | 注册 `--Wunsupported-hints` / `--Werr-hints` |
| `registerListVersionsOption()` | 注册 `--list-versions` |
| `registerTileIRTranslations()` | 注册 `mlir-to-cudatilebc` / `cudatilebc-to-mlir` 两个翻译 |
| `registerTileIRTestTranslations()` | 注册仅测试用的 round-trip 翻译 |

第 26–27 行 `mlir::failed(mlir::mlirTranslateMain(...))` 是 MLIR 提供的通用主循环：它负责读取输入文件、按 `--xxx` 选择翻译、把结果写到 `-o` 指定的文件。工具只需返回它的成功/失败。

`--bytecode-version` 的默认值是关键细节：它默认初始化为 `BytecodeVersion::kCurrentVersion`（即 13.3），而非兼容版 13.1。这正是 README 为什么要显式写 `--bytecode-version=13.1` 的根因——不带这个参数会产出 13.3 字节码，旧驱动可能无法加载。

[lib/Bytecode/Common/CommandLineOptions.cpp:73-87](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/CommandLineOptions.cpp#L73-L87) — 定义 `--bytecode-version` 选项，`init(BytecodeVersion::kCurrentVersion)`；`getCurrentBytecodeVersion()` 在未设置时回退到当前版。

选项背后挂的自定义 parser 只接受 `major.minor` 或 `major.minor.tag`，并通过 `BytecodeVersion::fromVersion` 校验是否落在支持区间内：

[lib/Bytecode/Common/CommandLineOptions.cpp:28-60](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/CommandLineOptions.cpp#L28-L60) — 解析版本字符串；非法值（如 `12.0`）会触发形如 `supported versions are [13.1 - 13.3]` 的错误。

`--list-versions` 则会枚举所有受支持版本后直接 `exit(0)`：

[lib/Bytecode/Common/CommandLineOptions.cpp:111-123](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/CommandLineOptions.cpp#L111-L123) — 列出 `getSupportedVersions()` 返回的全部版本并退出。

工具的构建依赖也可圈点：

[tools/cuda-tile-translate/CMakeLists.txt:8-24](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-translate/CMakeLists.txt#L8-L24) — 链接了 `CudaTileDialect` 与 `CudaTileBytecodeTranslation`，这正是 `main` 里那些 `register...` 符号的来源。

#### 4.2.4 代码实践

实践目标：用 `--list-versions` 和「故意给错版本」来验证命令行注册与 parser 行为，无需 GPU。

操作步骤：

1. 运行 `cuda-tile-translate --list-versions`，记录列出的版本。
2. 运行 `cuda-tile-translate example.mlir --mlir-to-cudatilebc --bytecode-version=12.0 -o /tmp/x.tilebc`，观察报错。
3. 分别用 `--bytecode-version=13.1` 与「不带该参数」生成两份 `.tilebc`，用 `cmp` 或 `xxd | head` 对比文件头是否一致。

需要观察的现象：

- 第 1 步输出形如 `13.1`、`13.2`、`13.3` 的若干行。
- 第 2 步因版本越界而报错退出，不会生成文件。
- 第 3 步两份字节码（版本不同）头部会有差异。

预期结果：见上；本环境未实际运行，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `main` 里 `registerTileIRTranslations()` 这一行删掉，重新构建后运行 `cuda-tile-translate example.mlir --mlir-to-cudatilebc` 会发生什么？

**答案**：`--mlir-to-cudatilebc` 这个翻译不会被注册，MLIR 主循环会报「unknown translation / unknown option」，命令无法执行。这正说明翻译必须先注册才能在命令行被识别。

**练习 2**：为什么 `main` 里所有 `register...` 都必须在 `mlirTranslateMain` 之前？

**答案**：因为命令行解析发生在 `mlirTranslateMain` 内部；注册必须在解析之前完成，否则选项与翻译都不被认识。

### 4.3 Translation 注册：mlir-to-cudatilebc 与 cudatilebc-to-mlir

#### 4.3.1 概念说明

上一模块讲了「工具入口」，这一模块讲入口里真正干活的两个翻译是怎么注册的。理解这一节，你就掌握了「新增一种 IR 之间的转换」在 MLIR 里是怎么落地的——这是后续自定义工具/Pass 的基础。

MLIR 提供两个注册类：

- `TranslateFromMLIRRegistration`：从 MLIR 操作（内存中的 IR）转成别的东西（这里是把字节码写到输出流）。
- `TranslateToMLIRRegistration`：从别的东西（这里是字节码字节串）解析成 MLIR 操作。

所以 `mlir-to-cudatilebc` 用前者，`cudatilebc-to-mlir` 用后者，方向正好相反、互为逆操作。

#### 4.3.2 核心流程

序列化方向（`mlir-to-cudatilebc`）：

```
拿到根 Operation*
  → 取出其中的 cuda_tile::ModuleOp（getCudaTileModuleOp）
  → 读取 --bytecode-version 得到目标版本
  → 设置方言的 hint 告警/错误开关
  → writeBytecode(output, moduleOp, targetVersion) 写出
```

反序列化方向（`cudatilebc-to-mlir`）：

```
拿到字节码字节串 + Context
  → 加载 CudaTileDialect，设置 hint 开关
  → readBytecode(buffer, context) 重建出 ModuleOp
```

两者都会在方言注册表（DialectRegistry）里插入 `CudaTileDialect`，确保解析时方言可用。

#### 4.3.3 源码精读

序列化注册：

[lib/Bytecode/Translation/BytecodeTranslation.cpp:65-80](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Translation/BytecodeTranslation.cpp#L65-L80) — 注册 `mlir-to-cudatilebc`：回调里取目标版本、提取 ModuleOp、设置 hint 开关、调用 `writeBytecode`。

注意它先调用 `getCudaTileModuleOp(op)` 来兼容两种输入：要么直接就是一个 `cuda_tile.module`，要么是一个外层 `mlir.module` 里嵌套了单个 `cuda_tile.module`（因为 MLIR 解析时默认会自动包一层 module）：

[lib/Bytecode/Translation/BytecodeTranslation.cpp:47-63](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Translation/BytecodeTranslation.cpp#L47-L63) — 兼容裸 `cuda_tile.module` 与被外层 `mlir.module` 包裹的情况。这也解释了 README 既可以用 `--no-implicit-module`（不包裹）也可以不用（包裹后由这里剥出来）。

反序列化注册：

[lib/Bytecode/Translation/BytecodeTranslation.cpp:35-42](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Translation/BytecodeTranslation.cpp#L35-L42) — 注册 `cudatilebc-to-mlir`：把字节串交给 `deserializeModule`，最终调 `readBytecode`。

两个注册被统一收口在一个公开函数里，供 `main` 调用：

[lib/Bytecode/Translation/BytecodeTranslation.cpp:82-85](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Translation/BytecodeTranslation.cpp#L82-L85) — `registerTileIRTranslations()` 同时注册序列化与反序列化两个翻译。

最后看版本常量的真身，理解 13.1 / 13.3 的来历：

[lib/Bytecode/Common/Version.cpp:39-64](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/Version.cpp#L39-L64) — 定义 `kCurrentCompatibilityVersion{13,1,0}`（兼容版，对应上一主要工具链版本）、`kCurrentVersion{13,3,0}`（当前版）、`kMinSupportedVersion{13,1,0}`（最低支持）。

由此可以闭环理解 4.2 里那个默认值问题：`--bytecode-version` 默认 = `kCurrentVersion` = 13.3；而面向广泛驱动兼容时应显式指定 13.1（= `kCurrentCompatibilityVersion`），这正是 README 示例的做法。

#### 4.3.4 代码实践

实践目标：跑一次完整的 round-trip（MLIR → 字节码 → MLIR），验证两个翻译互为逆操作。

操作步骤：

1. 用 4.1 的命令生成 `example.tilebc`。
2. 反向翻译：
   ```bash
   cuda-tile-translate example.tilebc --cudatilebc-to-mlir -o example_rt.mlir
   ```
   打开 `example_rt.mlir` 查看还原出的文本。
3. 再正向：
   ```bash
   cuda-tile-translate example_rt.mlir --mlir-to-cudatilebc \
     --bytecode-version=13.1 --no-implicit-module -o example_rt.tilebc
   ```
4. 用 `cmp example.tilebc example_rt.tilebc` 比较两次字节码。

需要观察的现象：

- 第 2 步得到的 `.mlir` 与原始 `example.mlir` 在语义上一致（空白/属性顺序可能略有差异）。
- 第 4 步两次字节码应高度一致（round-trip 稳定）。

预期结果：round-trip 成功；逐字节是否完全一致待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`mlir-to-cudatilebc` 为什么用 `TranslateFromMLIRRegistration` 而不是 `TranslateToMLIRRegistration`？

**答案**：因为它的方向是「从内存中的 MLIR Operation 出发，把字节码写到输出流」，即 From-MLIR；而 `cudatilebc-to-mlir` 是「把字节码读进内存变成 MLIR」，是 To-MLIR。

**练习 2**：如果不传 `--no-implicit-module`，`example.mlir` 会被解析成什么结构？序列化还能成功吗？

**答案**：会被自动包一层外层 `mlir.module`，里面嵌套 `cuda_tile.module`；仍能成功，因为 `getCudaTileModuleOp` 会把这层剥掉并校验「只有一个 cuda_tile module」。

## 5. 综合实践

把本讲三个模块串起来，完成一次「从源到运行」的完整走查（需要 GPU 的步骤标记待本地验证）：

1. 在仓库根目录按 u1-l2 的命令配置并构建（开启 TOOLS 与 TESTING）。
2. 创建 `example.mlir`（内容来自 README:233-246）与 `example_host.cpp`（内容来自 README:252-324）。
3. 生成字节码：
   ```bash
   build/bin/cuda-tile-translate example.mlir \
     --bytecode-version=13.1 --mlir-to-cudatilebc --no-implicit-module \
     -o example.tilebc
   ```
4. 先做一次 round-trip 自检：
   ```bash
   build/bin/cuda-tile-translate example.tilebc --cudatilebc-to-mlir -o check.mlir
   ```
   确认 `check.mlir` 与原文件语义一致。
5. AoT 路径（待本地验证，需 CUDA Toolkit 13.1+ 与 GPU）：
   ```bash
   tileiras --gpu-name sm_100 example.tilebc -o example.cubin
   g++ example_host.cpp -o example -I/usr/local/cuda/include -L/usr/local/cuda/lib64 -lcuda
   ./example
   ```
6. JIT 路径（待本地验证）：把 `example_host.cpp` 中的 `"example.cubin"` 改成 `"example.tilebc"`，重新编译运行，对比输出是否一致。
7. 在每一步对照 README:328-334 的说明，记录你实际遇到的命令行报错与版本差异。

成功标准：round-trip 自检通过；在具备 GPU 的机器上两条路径都打印出 README:337-340 所示的 128 个浮点数。

## 6. 本讲小结

- CUDA Tile IR 程序有三种形态：文本 `.mlir`、字节码 `.tilebc`、机器码 `.cubin`；`cuda-tile-translate` 负责 MLIR↔字节码，`tileiras`/驱动负责字节码→机器码。
- 字节码之后有 AoT（`tileiras` 提前出 cubin）与 JIT（驱动直接加载 `.tilebc`）两条等价路径，启动都复用 `cuLaunchKernel`。
- README 的 print 示例展示了从 `iota`/`reshape`/`broadcast`/`offset` 构造地址、`load_ptr_tko` 读数据到 `print_tko` 输出的最小内核。
- `cuda-tile-translate` 的 `main` 极简：先注册五个选项/翻译，再交给 `mlirTranslateMain`；所有注册必须在解析命令行之前。
- `mlir-to-cudatilebc`（From-MLIR）与 `cudatilebc-to-mlir`（To-MLIR）是互逆的两个翻译，收口在 `registerTileIRTranslations()`。
- `--bytecode-version` 默认是当前版 13.3，面向广泛兼容应显式指定兼容版 13.1；支持区间为 `[13.1, 13.3]`。

## 7. 下一步学习建议

- 本讲只让你「跑起来」并读懂工具入口。下一步进入 u2 单元学习 MLIR/LLVM 依赖与方言定义（u2-l1、u2-l2），理解 `cuda_tile` 方言与 `cuda_tile.module` / `entry` 这些结构的正式定义。
- 对字节码二进制格式（magic、各 Section）感兴趣的话，可以先存个疑问，等到 u7 单元「字节码二进制格式」再深入 `BytecodeWriter` / `BytecodeReader`。
- 想立即动手验证但又没 GPU 的读者，可以先把 `--list-versions`、round-trip、版本越界报错这些不需要 GPU 的实验做完，建立对工具链的肌肉记忆。
