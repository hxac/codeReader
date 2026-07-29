# triton-cuda-tile-opt 工具与 lit/FileCheck 测试

> 本讲属于第四单元（advanced），承接第三单元「MLIR 转换 Pass 体系」。前置认知：你已经知道 `make_tileir` 把 TTIR 的 `tt.*` 算子 lowering 成 `cuda_tile` 方言（u3-l1 ~ u3-l7）。本讲回答一个工程问题：**这些 C++ Pass 怎样在不依赖 GPU、不依赖 Python 前端的前提下被快速、可重复地回归测试？**

## 1. 本讲目标

学完后你应当能够：

1. 说清 `triton-cuda-tile-opt` 这个独立可执行文件是什么、它的 `main` 在哪、它和上游 `triton-opt`/`mlir-opt` 是什么关系。
2. 看懂一条 lit 测试的 `RUN` 行：工具名、`--pass-pipeline`、`-split-input-file`、`| FileCheck %s` 各自的作用，以及 pass 之间为什么按那个顺序串联。
3. 写出正确的 `CHECK` / `CHECK-NEXT` / `CHECK-LABEL` / `CHECK-NOT` 断言，会用 `[[VAR:%.*]]` 捕获变量、用 `{{...}}` 做通配、用 `--check-prefix` 在一个文件里做多组断言。
4. 在编译器崩溃时，按 AGENTS.md 的方法把 MLIR reproducer 存到 `/tmp` 并本地复现。

## 2. 前置知识

本讲只读源码、跑命令，不改 IR 逻辑。先建立三个直觉：

- **为什么需要独立测试工具？** 在第三单元里，转换 Pass 是用 C++ 写的、挂在 `make_tileir` 流水线里被 Python 间接调用。但 Pass 的正确性与 GPU、与 Python 前端都无关——它只接受一段 MLIR 文本、输出一段 MLIR 文本。用一个「喂文本、吐文本」的小工具来测它，比每次都启动 Python + GPU 快几个数量级，而且 lit 测试**不需要 GPU**（见 AGENTS.md 第 9 行）。
- **mlir-opt 是什么？** MLIR 上游自带一个通用驱动 `mlir-opt`：读一个 `.mlir` 文件 → 跑你指定的 pass pipeline → 把变换后的 IR 打印到 stdout。Triton 上游据此定制了 `triton-opt`；TileIR 后端再据此定制了 `triton-cuda-tile-opt`。三者是「同一套骨架 + 不同的方言/Pass 注册」。
- **lit 与 FileCheck 各管什么？** `lit`（LLVM Integrated Tester）是**调度器**：它扫描目录，找出每个测试文件里的 `// RUN:` 行，把里面的 `%s` 替换成文件路径，执行命令，按退出码判定 PASS/FAIL，可并行。`FileCheck` 是**断言器**：它读取工具的 stdout，按文件里的 `// CHECK:` 行逐条匹配，匹配顺序必须与输出顺序一致。一个 lit 测试通常是 `lit 调度 → RUN 行里 "工具 | FileCheck" → 工具产出 IR，FileCheck 校验 IR`。

关键术语：`MlirOptMain`（MLIR 提供的 opt 主循环）、`DialectRegistry`（方言注册表）、pass pipeline（pass 流水线，用括号表达嵌套）、`%s`（lit 里「当前测试文件」的占位符）、`RUN`/`CHECK`（lit/FileCheck 的指令）、reproducer（崩溃时 MLIR 自动生成的可重放现场）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `third_party/tileir/tools/triton-cuda-tile-opt/triton-cuda-tile-opt.cpp` | 工具的 `main` 入口，约 14 行，是 `MlirOptMain` 的薄壳。 |
| `third_party/tileir/tools/triton-cuda-tile-opt/RegisterTritonCudaTileDialects.h` | 注册 TileIR 相关方言与 Pass 的头文件。 |
| `third_party/tileir/tools/triton-cuda-tile-opt/CMakeLists.txt` | 声明该可执行文件链接哪些静态库。 |
| `third_party/tileir/test/CMakeLists.txt` | lit 测试的装配：测试依赖、FileCheck 路径、`check-triton-cuda-tile` 目标。 |
| `third_party/tileir/test/FileCheck/*.mlir` | 真正的测试用例，含 `RUN` 行与 `CHECK` 断言。 |
| `AGENTS.md` | 仓库给出的「构建 + 跑 lit + 复现 reproducer」操作手册。 |
| `Makefile` | `BUILD_DIR` 的推导方式与 `triton-opt`/`test-lit` 目标。 |

> 说明：本仓库里 `third_party/tileir/test/lit.cfg.py` 与 `lit.site.cfg.py.in` 目前是**空文件（0 字节）**，lit 的实际装配信息都写在 `test/CMakeLists.txt` 里。本讲讲 lit 配置时以 `CMakeLists.txt` 为准，不臆测这两个空文件的内容。

## 4. 核心概念与源码讲解

### 4.1 opt 工具入口：一个 14 行的 MlirOptMain 薄壳

#### 4.1.1 概念说明

`triton-cuda-tile-opt` 是一个**独立可执行文件**（不是 Python 模块），它的全部职责是：把命令行上指定的 pass pipeline，作用到从文件读进来的 MLIR 上，再把结果 IR 打印出来。这个「读 → 跑 → 打印」的主循环 MLIR 已经实现好了，叫 `MlirOptMain`。所以工具的 `main` 几乎不需要写逻辑，只需要做两件事：

1. **告诉 MLIR「我这套工具认得哪些方言、有哪些 pass 可用」**——这是通过一个 `DialectRegistry` 完成的。
2. **把命令行交给 `MlirOptMain`**，让它解析 `--pass-pipeline=...`、`-split-input-file` 等参数并执行。

如果一个 pass 没有在工具里注册，`--pass-pipeline` 里写它的名字就会报「unknown pass」。因此「注册了哪些 pass」直接决定了 lit 测试里能引用哪些 pass 名。

#### 4.1.2 核心流程

```
main(argc, argv)
  ├── DialectRegistry registry
  ├── registerTritonCudaTileDialects(registry)   // 注册全部方言 + 全部 Pass
  ├── cuda_tile::registerFuseFMAPass()           // 额外注册 fuse-fma
  ├── cuda_tile::registerLoopSplitPass()         // 额外注册 loop-split
  └── MlirOptMain(argc, argv, "Triton-Cuda-Tile test driver", registry)
        └── 解析命令行 → 读输入 IR → 跑 pipeline → 打印输出 IR
```

注意第 3、4 步：`fuse-fma` 与 `loop-split` 是 **cuda_tile 方言自带**的 pass，注册函数在 `cuda_tile` 命名空间下，需要单独调用；而 TileIR 自己写的转换 pass（`convert-triton-to-cuda-tile` 等）则在 `registerTritonCudaTileDialects` 里一次性注册。

#### 4.1.3 源码精读

工具 `main` 全文只有十几行：

[third_party/tileir/tools/triton-cuda-tile-opt/triton-cuda-tile-opt.cpp:6-13](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/tools/triton-cuda-tile-opt/triton-cuda-tile-opt.cpp#L6-L13) —— 构建 registry、注册方言与 pass，最后把控制权交给 `MlirOptMain`。关键三行是：`registerTritonCudaTileDialects(registry)` 注册 TileIR 方言与 pass，`registerFuseFMAPass()` / `registerLoopSplitPass()` 注册两个 cuda_tile 自带 pass，`MlirOptMain(...)` 接管命令行。

`registerTritonCudaTileDialects` 的实现在同名头文件里，决定了「这个工具到底认得哪些 pass」：

[third_party/tileir/tools/triton-cuda-tile-opt/RegisterTritonCudaTileDialects.h:31-43](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/tools/triton-cuda-tile-opt/RegisterTritonCudaTileDialects.h#L31-L43) —— 这里能看到三件事：(1) `registerAllPasses()` 把 MLIR 全部标准 pass（`inline`、`canonicalize`、`cse`、`reconcile-unrealized-casts` 等）都注册了，所以 lit 测试里能直接用这些名字；(2) 显式注册了 TileIR 三个 pass——`registerConvertTritonToCudaTilePass()`（`convert-triton-to-cuda-tile`）、`registerRewriteAssumeWithCudaTilePass()`（`rewrite-assume-with-cuda-tile`）、`registerAutoGenMemoryTokenPass()`（`auto-gen-memory-token`），它们对应 u3-l2/u3-l5/u3-l6 讲过的转换；(3) `registry.insert<...>()` 把 `cuda_tile`、`triton`、`cf`、`math`、`arith`、`scf`、`gpu`、`LLVM`、`NVVM`、`ub` 等方言塞进注册表，保证读入的 IR 里出现这些方言的算子时不会被工具拒绝。

> 对照 u3-l4：`lift-tt-cf-to-scf` 这个 pass **没有**在这里注册。它属于 `make_tileir` 的 Python 侧流水线（经 pybind 暴露），而这个 opt 工具的 lit 测试不直接测它——这正解释了为什么本目录的 `RUN` 行里看不到 `lift-tt-cf-to-scf`。

工具链接的库清单揭示了它的「拼装」方式：

[third_party/tileir/tools/triton-cuda-tile-opt/CMakeLists.txt:8-21](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/tools/triton-cuda-tile-opt/CMakeLists.txt#L8-L21) —— 链接 `TritonTransforms`、`TritonTileIRTransforms`、`TritonToTileIR`、`TritonNvidiaGPUTransforms`（转换/变换实现）、`TritonTestAnalysis`（测试用分析 pass）、以及 `MLIROptLib`/`MLIRPass`/`MLIRRegisterAllDialects`/`MLIRRegisterAllPasses`（MLIR 核心）。也就是说：转换 pass 的 C++ 实现在这些静态库里，工具只是把它们「拉」到一起注册。

#### 4.1.4 代码实践

**实践目标**：确认「工具能跑哪些 pass」是由注册代码决定的，建立 pass 名 ↔ 注册函数的对应。

**操作步骤**：

1. 在 `RegisterTritonCudaTileDialects.h` 里找到三个 `register...Pass()` 调用（L34-L36）。
2. 在 `triton-cuda-tile-opt.cpp` 里找到 `registerFuseFMAPass()` / `registerLoopSplitPass()`（L9-L10）。
3. 把每个注册函数对应到它在 lit 测试 `RUN` 行里的 pass 名。

**需要观察的现象**：你会发现 lit 测试里出现的 pass 名（`convert-triton-to-cuda-tile`、`rewrite-assume-with-cuda-tile`、`auto-gen-memory-token`、`fuse-fma`）每一个都能在上面的注册调用里找到源头；没注册的 pass（如 `lift-tt-cf-to-scf`）在 `RUN` 行里不会出现。

**预期结果**：能画出一张「pass 名 → 注册函数 → 所在文件」的小表。若想反向验证，可在构建后执行 `triton-cuda-tile-opt --help`，在 pass 列表里找到上述名字（待本地验证，取决于本地是否已构建）。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `fuse-fma` 要在 `main` 里单独注册，而 `convert-triton-to-cuda-tile` 不用？
  - **答案**：`convert-triton-to-cuda-tile` 是 TileIR 自己的 pass，已在 `registerTritonCudaTileDialects` 里随方言一起注册；`fuse-fma` 属于 `cuda_tile` 方言自带的 pass（来自 NVIDIA cuda-tile），命名空间不同，需在 `main` 里显式调用 `cuda_tile::registerFuseFMAPass()`。

- **练习 2**：如果想让 lit 测试也能跑 `lift-tt-cf-to-scf`，需要在哪两处补注册？
  - **答案**：需要在 `RegisterTritonCudaTileDialects.h` 里加一行对应的 `register...Pass()`（该 pass 的注册函数在 `Transform/Passes.h` 里）。注：这是改造设想，本仓库当前未这么做。

---

### 4.2 lit 配置与运行

#### 4.2.1 概念说明

lit 不读 CMake，它读**配置文件**（`lit.cfg.py` / `lit.site.cfg.py`）来知道「测试根目录在哪、工具在哪、有哪些命令替换」。在本仓库里，这两个 `.py` 文件目前是空的，真正有意义的信息在 `test/CMakeLists.txt` 里——CMake 在构建期用 `configure_lit_site_cfg` 把它加工成生成到 build 目录的配置，并注册一个 `check-triton-cuda-tile` 目标。

`FileCheck` 是和 lit 配套的二进制，由 LLVM 提供，路径通常在 LLVM 安装目录的 `bin/` 下。CMake 把它的路径通过 `LIT_ARGS` 传给 lit（`-Dfilecheck=...`）。

#### 4.2.2 核心流程

```
构建期 (CMake):
  test/CMakeLists.txt
    ├── configure_lit_site_cfg(lit.site.cfg.py.in → build/lit.site.cfg.py, MAIN_CONFIG=lit.cfg.py)
    ├── TRITON_CUDA_TILE_TEST_DEPENDS = [triton-cuda-tile-opt, triton-opt, triton-llvm-opt]
    ├── FILECHECK_PATH = <LLVM>/bin/FileCheck ;  LIT_ARGS = "-Dfilecheck=..."
    └── add_lit_testsuite(check-triton-cuda-tile, "Running ...", <build test root>,
                           DEPENDS ${TRITON_CUDA_TILE_TEST_DEPENDS})

运行期 (从 build 目录):
  lit -v <测试文件或目录>
    └── 对每个 .mlir: 执行其 // RUN: 行 → 看退出码 → 汇总 PASS/FAIL
```

`TRITON_CUDA_TILE_TEST_DEPENDS` 里的三个工具是测试的前提：`triton-cuda-tile-opt`（跑 TileIR pass）、`triton-opt`（跑上游 triton pass）、`triton-llvm-opt`（跑 LLVM 层 pass）。`add_lit_testsuite` 把整个 `test/` 源目录注册成一个可批量运行、可并行的测试套件。

#### 4.2.3 源码精读

lit 装配的核心在测试目录的 CMakeLists：

[third_party/tileir/test/CMakeLists.txt:11-32](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/CMakeLists.txt#L11-L32) —— 注意三处：`TRITON_CUDA_TILE_TEST_DEPENDS` 列出测试依赖的三个可执行文件（L11-L15）；`FILECHECK_PATH` 从 LLVM 库目录反推 FileCheck 二进制、并通过 `LIT_ARGS` 注入（L17-L18）；`add_lit_testsuite(check-triton-cuda-tile ...)` 建立一个批量跑全部 lit 测试的目标，`DEPENDS` 保证跑测试前先编出那三个工具（L28-L32）。同文件 L34-L36 的 `add_lit_testsuites` 则把源目录下所有 `.mlir` 逐一登记。

仓库的操作手册 AGENTS.md 给出了「怎么跑」的权威方法：

[AGENTS.md:7-10](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/AGENTS.md#L7-L10) —— 三条要点：(1) build 目录由 `get_cmake_dir()` 推导（L7）；(2) 从 build 目录跑：先 `ninja <工具>`，再 `lit -v test/<path>.mlir`（L8）；(3) lit 测试**不需要 GPU**（L9），并且崩溃时可保存 MLIR reproducer 用 `triton-opt ... --run-reproducer` 复现（L10）。

`BUILD_DIR` 的推导方式与 Makefile 里的一致：

[Makefile:7-9](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/Makefile#L7-L9) —— `BUILD_DIR` 由 `PYTHONPATH=./python python3 -c 'from build_helpers import get_cmake_dir; print(get_cmake_dir())'` 得到（优先读环境变量 `TRITON_BUILD_DIR`，见 `python/build_helpers.py` 的 `get_cmake_dir`）；上游 `triton-opt` 产物在 `$(BUILD_DIR)/bin/triton-opt`。Makefile 第 21-23 行的 `triton-opt` 目标就是 `ninja -C $(BUILD_DIR) triton-opt`，第 27-29 行的 `test-lit` 目标则对应 `ninja -C $(BUILD_DIR) check-triton-lit-tests`（注意这是上游 Triton 的 lit 套件名，不是 TileIR 的 `check-triton-cuda-tile`）。

> 一个易错点：TileIR 自己的 lit 套件目标是 `check-triton-cuda-tile`（见 test/CMakeLists.txt L28），与上游 Triton 的 `check-triton-lit-tests`（Makefile L29）是**两个不同**的目标，别混淆。另外，本快照里 `third_party/tileir/CMakeLists.txt` 只 `add_subdirectory` 了 `include/lib/tools`（见该文件 L108-L110），并未显式加入 `test`，所以该 lit 套件是否被默认纳入构建取决于上层配置——以本地 `ninja help | grep triton-cuda-tile` 为准（待本地验证）。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：依据 AGENTS.md 的方法，在本地构建 `triton-cuda-tile-opt` 并跑通一个 lit 测试，然后解释其 `RUN` 行 pass-pipeline 的串联顺序。

**操作步骤**：

```bash
# 0) 进入仓库根目录
cd <仓库根>

# 1) 先重建 triton（AGENTS.md 要求：跑测试前先 make）
make

# 2) 推导 build 目录（与 Makefile/AGENTS.md 一致）
BUILD_DIR=$(PYTHONPATH=./python python3 -c 'from build_helpers import get_cmake_dir; print(get_cmake_dir())')

# 3) 构建 opt 工具（ninja 目标名 == 可执行文件名）
ninja -C "$BUILD_DIR" triton-cuda-tile-opt

# 4) 让 lit/命令行能找到该工具，把它所在目录加入 PATH
#    （工具产物路径见 tileir/CMakeLists.txt 的 TRITON_CUDA_TILE_TOOL_DIR）
export PATH="$BUILD_DIR/third_party/tileir/tools/triton-cuda-tile-opt:$PATH"

# 5) 用 lit 跑一个测试（从 build 目录，按 AGENTS.md 的 lit 用法）
cd "$BUILD_DIR"
lit -v third_party/tileir/test/FileCheck/op-rewrite-assume.mlir
```

> 上面第 4、5 步的具体路径可能因本地 lit 配置而异（本快照的 `lit.cfg.py` 为空）。**如果 lit 找不到工具或测试根**，改用「手动复现」方式，它最稳——直接把 `RUN` 行翻译成命令执行（见下面）：

```bash
# 手动复现 op-rewrite-assume.mlir 的 RUN 行（等价于 lit 做的事）
TOOL="$BUILD_DIR/third_party/tileir/tools/triton-cuda-tile-opt/triton-cuda-tile-opt"
T=<仓库根>/third_party/tileir/test/FileCheck/op-rewrite-assume.mlir
"$TOOL" "$T" -split-input-file \
  --pass-pipeline="builtin.module(rewrite-assume-with-cuda-tile)" \
  | FileCheck "$T"
```

**需要观察的现象**：lit 打印出 `--pass-pipeline` 的解析、每个 `// -----` 分隔的用例的 PASS/FAIL，最后汇总 `Expected Passes: N`。手动方式下，FileCheck 不输出即代表全部 `CHECK` 通过（退出码 0）。

**预期结果**：`op-rewrite-assume.mlir` 的三个用例全部 PASS（它测的是 u3-l5 讲的 assume 改写）。具体数字**待本地验证**（本环境未构建，不假装已运行）。

**解释 RUN 行 pass-pipeline 的串联顺序**（以 `op-conversion.mlir` 的 pipeline 为例）：

[third_party/tileir/test/FileCheck/op-conversion.mlir:1](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion.mlir#L1) —— 这条 RUN 行的 pipeline 是：

```
builtin.module(
  convert-triton-to-cuda-tile,
  cuda_tile.module(cuda_tile.experimental$func(fuse-fma)),
  reconcile-unrealized-casts
)
```

顺序由「每一步产生什么、下一步需要什么」决定，是严格的因果链：

1. `convert-triton-to-cuda-tile`（最前）：主转换，把 `tt.func` 体内的 `tt.*` lowering 成 `cuda_tile` 算子，并在 module 内**插入** `cuda_tile.module` 容器与每个 kernel 的 `cuda_tile.experimental$func`（EntryOp）。它是 cuda_tile IR 的源头，必须最先跑（对应 u3-l2）。
2. `cuda_tile.module(...)`（下降一层）：`convert` 刚插出了 `cuda_tile.module` 容器，`fuse-fma` 要作用在容器**内部**的 kernel 上，所以用 `cuda_tile.module(...)` 把后续 pass 嵌套下沉进容器（对应 u3-l7 的 nest 概念）。
3. `cuda_tile.experimental\$func(fuse-fma)`（再下降一层）：进入每个 kernel 的 EntryOp 函数体跑 FMA 融合。两层嵌套 = `module → entry`，与 u3-l7 讲的「fuse-fma 嵌套两层」一致。`\$` 是 shell 转义，让字面量 `experimental$func`（EntryOp 的助记符）原样传给工具。
4. `reconcile-unrealized-casts`（最后收尾）：转换过程中大量产生了 `builtin.unrealized_conversion_cast` 桥接算子（在 4.3 的例子中随处可见），这一步把它们清理/收口。

> 顺序不能乱：转换必须先产生容器与 cast，fuse-fma 才有 nest 的目标，reconcile 必须放最后清理转换刚造出来的 cast。

#### 4.2.5 小练习与答案

- **练习 1**：`op-conversion.mlir` 的 RUN 行没有写 `cuda_tile.module(...)` 之外的东西来跑 `auto-gen-memory-token`，而 `op-conversion-auto-memtoken.mlir` 写了。两者 pipeline 差在哪？
  - **答案**：后者在 `reconcile-unrealized-casts` 之后多挂了 `,auto-gen-memory-token`（或带选项 `auto-gen-memory-token{autogen-alias-memtoken=true}`）。因为 memtoken pass 处理的是 cuda_tile 访存算子，必须在主转换完成、cast 收口之后才能跑（对应 u3-l6）。

- **练习 2**：为什么 lit 测试「不需要 GPU」？
  - **答案**：opt 工具只做 IR→IR 的文本变换，验证的是 Pass 的转换正确性；真正的 GPU 代码生成（`tileiras` 产 cubin）和内核启动是另一条链路，lit 测试不触及它们。

---

### 4.3 FileCheck 匹配语法

#### 4.3.1 概念说明

FileCheck **不是**把工具输出和 `CHECK` 行做字符串全等比较，而是「**按顺序逐条匹配**」：从上一次匹配结束的位置继续往下找，找下一条 `CHECK` 的匹配点。因此：

- 各 `CHECK` 行在文件里的书写顺序，必须与它们在输出里出现的顺序一致。
- `CHECK` 行里可以写**正则**：`{{...}}` 表示正则片段，`%.*` 是「匹配到下一个分隔符的通配」（最常用），`[[$name:pattern]]` 既匹配又把命中文本**捕获**到变量 `$name`，之后用 `[[$name]]` 引用它。

常用指令：

| 指令 | 语义 |
| --- | --- |
| `CHECK:` | 匹配一行（从当前位置向后找）。 |
| `CHECK-NEXT:` | 必须紧贴**上一条匹配的下一行**命中（用来强制相邻）。 |
| `CHECK-LABEL:` | 匹配一个「分界标记」（通常是函数/模块名），并**重置**后续 CHECK 的搜索范围；常用于在一个文件里隔离多个用例。 |
| `CHECK-NOT:` | 断言两次匹配之间**不出现**某模式（反向校验，常用于「不该有 error」）。 |
| `CHECK-DAG:` | 在一个区间内**乱序**匹配（不要求顺序）。 |

辅助开关：`-split-input-file`（按 `// -----` 把一个文件切成多个独立用例）、`--check-prefix=NAME`（只用 `// NAME:` 前缀的断言，实现一个文件多套校验）、`-verify-diagnostics`（配合 `// expected-error` 做「期望报错」测试）。

#### 4.3.2 核心流程

```
FileCheck 读入: 测试文件(CHECK 指令) + 工具 stdout(被校验文本)
pos = 0
for 每条 CHECK 指令(按文件出现顺序):
    在 stdout[pos..] 中向后搜索该指令的模式
    CHECK-LABEL: 命中后重置后续区间; CHECK-NEXT: 要求命中点紧跟上一行
    CHECK-NOT:   断言当前区间内不出现该模式
    命中失败 → 报错并指出差异; 命中 → pos 推进到命中点之后
全部命中 → 退出码 0
```

#### 4.3.3 源码精读

**例 1：捕获变量 + CHECK-NEXT 的典型链**。`op-rewrite-assume.mlir` 是最好的教学用例，它测 u3-l5 的 assume 改写：

[third_party/tileir/test/FileCheck/op-rewrite-assume.mlir:3-6](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-rewrite-assume.mlir#L3-L6) —— `// CHECK-LABEL: kernel_assume` 用函数名当分界锚点（L3）；`// CHECK: [[V0:%.*]] = builtin.unrealized_conversion_cast {{.*}} : i32 to !cuda_tile.tile<i32>` 用 `[[V0:%.*]]` 捕获了转换算子的结果 SSA 值，`{{.*}}` 通配中间的操作数（L4）；`// CHECK-NEXT: [[V1:%.*]] = cuda_tile.assume div_by<32>, [[V0]]` 强制这条**紧跟**上一行，并用 `[[V0]]` 引用刚才捕获的值（L5）；最后 `// CHECK-NEXT: ... {tt.divisibility = 32 : i32}` 验证 divisibility 属性被回写（L6）。同一文件第 19 行的 `// -----` 是 `-split-input-file` 的用例分隔符，它后面又是一组以 `kernel_assume_2` 为 LABEL 的独立断言。

**例 2：CHECK-NOT 反向断言**。`op-conversion-assume.mlir` 在主转换 + fuse-fma 的完整 pipeline 下，只断言「不报错」：

[third_party/tileir/test/FileCheck/op-conversion-assume.mlir:1-3](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-assume.mlir#L1-L3) —— `// CHECK-NOT: error`（L3）表示整个输出里不允许出现 `error` 字样；配合 `2>&1`（RUN 行 L1）把 stderr 也纳入校验，确保转换这段复杂 IR 时不抛任何诊断。

**例 3：`--check-prefix` 一个文件多套断言 + pass 选项**。`op-conversion-modifiers.mlir` 用五条 RUN 行，每条换一组 pass 选项、换一个 check 前缀：

[third_party/tileir/test/FileCheck/op-conversion-modifiers.mlir:1-5](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-modifiers.mlir#L1-L5) —— 这里能看到两个要点：**(a) pass 选项写在大括号里**，如 `convert-triton-to-cuda-tile{approx-modifier=true flush-to-zero-modifier=true}`（L1）、`{compute-capability=100 num-cta-in-cga=2}`（L4）——这正是 u3-l1 讲的 Passes.td 里定义的 approx/ftz/capability/num_ctas 等选项；**(b) 每条 RUN 用不同的 `--check-prefix`**（`APPROX_FTZ`/`APPROX`/`FTZ`/`HINT-100`/`HINT-120`），于是同一个输入 IR 在五组配置下分别校验各自期望的 modifier 是否被烘焙进 IR。

**例 4：`-verify-diagnostics` 期望报错**。`op-conversion-xfailure.mlir` 测的是「转换应当失败」：

[third_party/tileir/test/FileCheck/op-conversion-xfailure.mlir:1](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-xfailure.mlir#L1) —— 注意它的 RUN 行**末尾没有 `| FileCheck %s`**，而是带 `-verify-diagnostics`。这种测试靠文件里的 `// expected-error{{...}}` 注解来声明「这个 op 应当引发某条诊断」，MLIR 的 `-verify-diagnostics` 负责校验声明的错误确实发生（且没有多余错误）。它和 `CHECK` 是两套互补机制：`CHECK` 验「跑通后的 IR 长啥样」，`expected-error` 验「该挂的时候确实挂了」。

#### 4.3.4 代码实践

**实践目标**：读懂一条 `CHECK` 链，并亲手为一段输出补一条断言。

**操作步骤**：

1. 打开 `op-rewrite-assume.mlir`，对照 L8-L17 的输入 IR（`tt.func @assume`，含 `arith.remsi` + `llvm.intr.assume`）与 L3-L6 的断言。
2. 在脑子里跑一遍 u3-l5 的改写：`assume(remsi(x,C)==0)` → `cuda_tile.assume div_by<C>`。
3. 假设你想额外断言「输出的 assume 的整除常数是 32」，确认 L5 的 `div_by<32>` 已经覆盖。
4. 进阶：把 L4 的 `[[V0:%.*]]` 改成 `[[V0:%[0-9]+]]`（只匹配纯数字 SSA 名），思考这会让断言变严格还是变宽松。

**需要观察的现象**：`CHECK-LABEL` 把校验范围锁定在 `kernel_assume` 内；`CHECK-NEXT` 保证 conversion_cast 与 assume 相邻出现；捕获变量 `[[V0]]` 让 L5 能精确引用 L4 命中的那个值。

**预期结果**：理解「`%.*` 是正则、`$` 只是命名符号、`{{}}` 是正则块」三者的区别。第 4 步的改动会让断言更严格（不再接受非数字 SSA 名），但对本用例仍通过（待本地验证）。

#### 4.3.5 小练习与答案

- **练习 1**：把 `CHECK:` 误写成两条相邻的，但没有用 `CHECK-NEXT:`，会有什么风险？
  - **答案**：`CHECK:` 只要求「按顺序往后命中」，两条之间允许插入任意行；如果转换意外多吐了一行，断言仍可能通过。`CHECK-NEXT:` 才强制「必须紧贴上一行」，能捕捉到这种意外插入。

- **练习 2**：`op-conversion-assume.mlir` 为什么用 `CHECK-NOT: error` 而不是写一堆 `CHECK:`？
  - **答案**：这个用例的目的是「确保这段复杂 IR 能被整条 pipeline 干净地转换、不抛诊断」，重点是**不报错**而非精确校验每个算子，所以用反向断言 `CHECK-NOT: error` 最省事也最贴合意图。

- **练习 3**：一个文件里有多个 `// -----` 分隔的用例时，为什么每个用例都建议以 `CHECK-LABEL:` 开头？
  - **答案**：`-split-input-file` 会把每个片段当作独立输入单独跑一遍工具、单独校验；`CHECK-LABEL` 在每个片段内重置搜索区间，避免上一个用例的断言「漏匹配」到下一个用例的输出，保证隔离。

---

### 4.4 本地复现 MLIR reproducer

#### 4.4.1 概念说明

当某个 pass 把 IR 改崩了（断言失败、段错误），MLIR 会在崩溃点**自动打印一份 reproducer**：一段完整的 IR 文本，顶部带 `external_resources` / `mlir_reproducer` 标记，末尾附 `{-# ... #-}` 形式的元数据，记录了触发崩溃的完整 pass pipeline 与选项。把它存成文件，就能脱离原始 Python 调用现场，用 opt 工具**精确重放**这次崩溃。这是定位 C++ Pass bug 的标准入口。

#### 4.4.2 核心流程

```
1. 触发崩溃（跑某个内核或某条 pipeline）
2. 从崩溃日志里抓出 reproducer 文本（external_resources ... 到 {-# ... #-}）
3. 存到 /tmp/xxx.mlir
4. triton-cuda-tile-opt /tmp/xxx.mlir --run-reproducer   # 精确重放，复现崩溃
   （也可加 --mlir-print-ir-after-failure 等观察中间 IR）
```

`--run-reproducer` 是 `MlirOptMain` 自带的能力——因为 `triton-cuda-tile-opt` 就是 `MlirOptMain` 薄壳（4.1），所以它天然支持这个开关，元数据里的 pipeline 会**覆盖**命令行上的 `--pass-pipeline`。

#### 4.4.3 源码精读

这套方法记录在 AGENTS.md 里：

[AGENTS.md:10](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/AGENTS.md#L10) —— 原文要点：编译器崩溃时有时会打印 MLIR reproducer（含 `external_resources` / `mlir_reproducer`），把完整的 MLIR 文本连同 `{-# ... #-}` 元数据存到 `/tmp/<file>.mlir`，再用 `triton-opt /tmp/<file>.mlir --run-reproducer` 本地复现。对 TileIR 而言，把这里的 `triton-opt` 换成 `triton-cuda-tile-opt` 即可（同样是 `MlirOptMain` 薄壳，同样支持 `--run-reproducer`）。

#### 4.4.4 代码实践

**实践目标**：掌握 reproducer 的「抓 → 存 → 放」三步。

**操作步骤**：

1. 在一次会让 TileIR 转换崩溃的运行中，从报错堆栈上方找到 `external_resources "mlir_reproducer"` 起始的那段文本。
2. 把从 `external_resources` 到结尾 `{-# ... #-}` 的**完整**内容复制到 `/tmp/repro.mlir`（缺了元数据就无法重放）。
3. 执行重放：

```bash
TOOL="$BUILD_DIR/third_party/tileir/tools/triton-cuda-tile-opt/triton-cuda-tile-opt"
"$TOOL" /tmp/repro.mlir --run-reproducer
# 想看每一步 IR：再加 --mlir-print-ir-after-failure
```

**需要观察的现象**：用 reproducer 重放时，崩溃**稳定复现**（与原始现场完全一致），而元数据里的 pipeline 会自动接管，无需你在命令行再写 `--pass-pipeline`。

**预期结果**：得到一个最小可复现现场，便于在 `gdb`/`lldb` 下 `run /tmp/repro.mlir --run-reproducer` 单步调试对应 Pass。具体能否复现取决于是否真有崩溃输入，**待本地验证**。

#### 4.4.5 小练习与答案

- **练习 1**：reproducer 末尾的 `{-# ... #-}` 元数据为什么必须一起保存？
  - **答案**：它记录了触发崩溃的完整 pass pipeline 与各 pass 选项。`--run-reproducer` 正是靠它重放，没有它 opt 就不知道该跑哪条 pipeline，复现就失效。

- **练习 2**：为什么用 reproducer 比「重新跑那个出问题的 Triton kernel」更好定位 bug？
  - **答案**：reproducer 已经把现场压缩成「一段静态 IR + 一条 pipeline」，去掉了 Python 前端、autotune、GPU 启动等无关变量，崩溃可瞬间重放，还能直接挂调试器单步进 Pass 代码。

---

## 5. 综合实践

把本讲三个模块串起来，**新增一个属于自己的最小 FileCheck 测试**（本练习为示例代码，不在仓库中，请自行创建）：

1. **造输入 IR**：在 `third_party/tileir/test/FileCheck/` 下新建 `my-first-test.mlir`，参考 `op-rewrite-assume.mlir` 写一个最小的带 `llvm.intr.assume` 的 `tt.func`，例如：

   ```mlir
   // RUN: triton-cuda-tile-opt %s -split-input-file \
   // RUN:   --pass-pipeline="builtin.module(rewrite-assume-with-cuda-tile)" | FileCheck %s

   // CHECK-LABEL: my_kernel
   // CHECK: cuda_tile.assume div_by<32>
   module @my_kernel {
     tt.func private @my_kernel(%arg0: i32 {tt.divisibility = 16 : i32}) -> i32 {
       %c32 = arith.constant 32 : i32
       %c0  = arith.constant 0  : i32
       %r = arith.remsi %arg0, %c32 : i32
       %e = arith.cmpi eq, %r, %c0 : i32
       llvm.intr.assume %e : i1
       %a = arith.addi %arg0, %arg0 : i32
       tt.return %a : i32
     }
   }
   ```

2. **设计 pipeline**：`RUN` 行只挂 `rewrite-assume-with-cuda-tile`（u3-l5 的 pass），避免被主转换的复杂性干扰。
3. **写断言**：`CHECK-LABEL` 锚定 `my_kernel`，`CHECK: cuda_tile.assume div_by<32>` 验证 assume 被改写（参考 4.3 例 1）。
4. **跑通**：用 4.2.4 的 lit 命令或手动复现命令执行；若断言失败，对照工具实际输出调整 `CHECK` 的正则。
5. **加难**：再写第二条 `RUN`，把 pipeline 换成 `convert-triton-to-cuda-tile{approx-modifier=true}` 并用 `--check-prefix=APPROX`（参考 4.3 例 3），体会「同一输入、多配置、多前缀」的写法。

完成这个练习，你就掌握了「读 → 改 → 跑 → 调」一个 TileIR lit 测试的完整闭环。

## 6. 本讲小结

- `triton-cuda-tile-opt` 是一个 14 行的 `MlirOptMain` 薄壳：它的能力边界 = `registerTritonCudaTileDialects` + `registerFuseFMAPass/LoopSplitPass` 注册了哪些 pass；它和上游 `triton-opt`/`mlir-opt` 同骨架、不同注册。
- lit 是调度器（扫 `RUN` 行、并行执行、汇总结果），FileCheck 是断言器（按顺序逐条匹配 stdout）；两者通过 `RUN: 工具 | FileCheck %s` 串联，且 lit 测试不需要 GPU。
- TileIR 的 lit 装配写在 `test/CMakeLists.txt`：依赖 `triton-cuda-tile-opt`/`triton-opt`/`triton-llvm-opt`，批量目标叫 `check-triton-cuda-tile`（区别于上游的 `check-triton-lit-tests`）。
- `RUN` 行的 pass-pipeline 是因果链：先 `convert-triton-to-cuda-tile` 造容器与 cast，再 `cuda_tile.module(cuda_tile.experimental$func(fuse-fma))` 两层 nest 跑融合，最后 `reconcile-unrealized-casts` 收口；顺序不可乱。
- FileCheck 要点：`CHECK-LABEL` 锚点、`CHECK-NEXT` 强制相邻、`CHECK-NOT` 反向断言、`[[V:%.*]]` 捕获与引用、`{{...}}` 正则、`--check-prefix` 多套断言、`-split-input-file` 多用例、`-verify-diagnostics` 期望报错。
- 崩溃时按 AGENTS.md 抓 MLIR reproducer 存 `/tmp`，用 `triton-cuda-tile-opt <file> --run-reproducer` 精确重放，是定位 C++ Pass bug 的标准入口。

## 7. 下一步学习建议

- 想知道这些 pass 在真实编译里如何影响性能？继续 **u4-l2 性能调优实践**（occupancy/num_ctas/TMA 偏好等旋钮）。
- 想理解转换失败时如何回退到 PTX 后端？继续 **u4-l3 编译期与运行期 Fallback 容错**。
- 想弄清 `triton-cuda-tile-opt` 链接的那些静态库（`TritonToTileIR` 等）是怎么被 cuda-tile 依赖与构建出来的？继续 **u4-l4 构建系统与 cuda-tile 依赖管理**。
- 想深入具体 pass 行为，可挑一个 `test/FileCheck/*.mlir`（如 `op-conversion-auto-memtoken.mlir`、`op-conversion-barrier.mlir`）对照 u3-l6 阅读，用本讲的方法本地复现它。
