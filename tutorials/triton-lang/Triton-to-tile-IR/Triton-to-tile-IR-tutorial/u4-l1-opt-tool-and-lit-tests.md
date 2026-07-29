# triton-cuda-tile-opt 工具与 lit/FileCheck 测试

## 1. 本讲目标

在前三个单元里，我们一路追踪了 TileIR 后端的编译链路：从 Python 侧的 `make_ttir` / `make_tileir` / `make_cubin` 三段式流水线（见 [u2-l3](u2-l3-compile-stages.md)），到 C++ 侧 `convert-triton-to-cuda-tile` 这一主转换 pass 把 triton 方言 lowering 成 `cuda_tile` 方言（见 [u3-l1](u3-l1-pass-plugin-skeleton.md)）。但有个问题一直悬而未决：**当某个 MLIR 转换出错、或你想单独验证一个算子的 lowering 结果时，怎么脱离整条 Python JIT 链路、把转换 pass 单独跑起来？**

答案就是本讲的主角——独立工具 `triton-cuda-tile-opt`，以及围绕它的 `lit` + `FileCheck` 测试体系。这是 TileIR 后端 C++ 转换 pass 的「实验台」与「回归测试网」。

学完本讲，你应当能够：

1. 说清 `triton-cuda-tile-opt` 这个可执行文件是怎么从一个 14 行的 `main` 函数搭起来的、它复用了 MLIR 的哪套基础设施、又额外注册了哪些 cuda_tile 专属 pass。
2. 看懂一份 `.mlir` 测试文件里的 `RUN` 行：知道 lit 如何发现并运行它、`--pass-pipeline` 文本里那串嵌套的含义、以及工具二进制是怎么被找到的。
3. 掌握 FileCheck 的匹配语法（`CHECK` / `CHECK-LABEL` / `CHECK-NOT` / 变量捕获 `%[[NAME:.*]]`），以及 `-verify-diagnostics` + `expected-*` 这种「断言报错」的写法。
4. 当编译器崩溃打印出 MLIR reproducer 时，知道怎么把它保存下来、用工具本地复现。

本讲是专家层（advanced）的第一篇，承接 [u3-l2](u3-l2-core-conversion-pass.md) 的核心转换 pass——你要测的正是那个 pass 的输入输出。本讲**不需要 GPU**，所有 lit 测试都是纯 CPU 的 IR 文本级检查。

## 2. 前置知识

本讲会用到一些 MLIR / LLVM 工具链的通用概念。如果没接触过，先建立下面这几个直觉再往下读。

- **mlir-opt 与 MlirOptMain**：MLIR 官方提供了一个标准命令行工具 `mlir-opt`，它能读入一份 `.mlir` 文本、按 `--pass-pipeline="..."` 指定的 pass 序列做变换、再把结果打印出来。任何项目都可以「复用」这套骨架，只需把自家的方言和 pass 注册进一个 `DialectRegistry`，再调用 `mlir::MlirOptMain(...)` 即可得到一个功能等价的、带自家方言的 opt 工具。`triton-cuda-tile-opt` 正是这么来的。
- **Pass pipeline 文本语法**：`--pass-pipeline="builtin.module(pass-a,pass-b)"` 表示「在最外层 `builtin.module` 上依次跑 pass-a、pass-b」。需要嵌套进某个容器 op 时，写成 `容器op(...)` 的嵌套形式（详见 4.2 节）。
- **lit**：LLVM 的测试执行器（test runner）。它把一份份 `.mlir`（或 `.ll`、`.c`）文件当成一个个独立的 shell 脚本来跑——文件顶部的 `// RUN: ...` 行就是要执行的命令，命令的退出码决定这个用例通过与否。lit 的价值在于批量、并发地跑成百上千个这样的小脚本。
- **FileCheck**：LLVM 的「输出匹配」工具。它从输入文件里读 `// CHECK: ...` 这类注释行当作「期望模式」，再拿被测程序的实际 stdout 去逐条匹配，匹配不上就报错。它不是精确字符串比较，而是「按顺序、支持正则与变量捕获」的松散匹配，专门为 IR/汇编这种长输出设计。
- **reproducer（复现器）**：MLIR 在某些崩溃场景会打印一段带 `external_resources` / `mlir_reproducer` 标记的完整 IR，外加一行 `{-# ... #-}` 元数据（描述当时跑的 pass pipeline）。把这段原样存成文件，就能用 `--run-reproducer` 离线复现崩溃。

如果你对「方言 / pass / ConversionTarget」这些 MLIR 概念还陌生，建议先读 [u3-l1 的「前置知识」](u3-l1-pass-plugin-skeleton.md)。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 职责 |
|------|------|
| [third_party/tileir/tools/triton-cuda-tile-opt/triton-cuda-tile-opt.cpp](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/tools/triton-cuda-tile-opt/triton-cuda-tile-opt.cpp) | **工具入口**：仅 14 行的 `main`，注册方言与 pass 后调用 `MlirOptMain` |
| [third_party/tileir/tools/triton-cuda-tile-opt/RegisterTritonCudaTileDialects.h](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/tools/triton-cuda-tile-opt/RegisterTritonCudaTileDialects.h) | **方言/pass 注册表**：`registerTritonCudaTileDialects` 声明把哪些方言、哪些 pass 装进 registry |
| [third_party/tileir/tools/triton-cuda-tile-opt/CMakeLists.txt](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/tools/triton-cuda-tile-opt/CMakeLists.txt) | **构建脚本**：用 `add_llvm_executable` 产出二进制，链接 MLIROptLib 与各转换静态库 |
| [third_party/tileir/test/CMakeLists.txt](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/CMakeLists.txt) | **lit 套件注册**：声明 `check-triton-cuda-tile` 目标及其依赖、FileCheck 路径 |
| [test/lit.cfg.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/test/lit.cfg.py) | **主 lit 配置**（上游 Triton）：把 `<build>/bin` 加入 PATH、设定 FileCheck 选项——工具靠它被「找得到」 |
| [third_party/tileir/test/FileCheck/op-conversion.mlir](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion.mlir) | **测试样例（最简）**：只有一行 RUN，是学习 pipeline 文本的最佳起点 |
| [third_party/tileir/test/FileCheck/op-conversion-barrier.mlir](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-barrier.mlir) | **测试样例（带 CHECK）**：展示 `CHECK-LABEL`/`CHECK-NOT`/变量捕获的完整用法 |
| [third_party/tileir/test/FileCheck/op-conversion-modifiers.mlir](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-modifiers.mlir) | **测试样例（多前缀）**：一个文件里用多条 RUN + `--check-prefix` 测多组 pass 选项 |
| [AGENTS.md](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/AGENTS.md) | **工作流约定**：构建目录推导、lit 运行、reproducer 复现的官方说明 |

## 4. 核心概念与源码讲解

### 4.1 opt 工具入口：triton-cuda-tile-opt

#### 4.1.1 概念说明

MLIR 生态有一个非常省力的约定：**你不需要从零写一个命令行工具**。官方把 `mlir-opt` 的全部「脚手架」——参数解析、读入 `.mlir` 文本、按 `--pass-pipeline` 构造并运行 PassManager、打印结果、支持 `-split-input-file` / `| FileCheck` / `--run-reproducer` 等——打包成一个可复用的库函数 `mlir::MlirOptMain`。任何项目只要做两件事，就能得到一个功能完整的、属于自家的 opt 工具：

1. 准备一个 `DialectRegistry`，把自家方言和 pass 注册进去；
2. 在 `main` 里调用 `MlirOptMain(argc, argv, "描述", registry)`。

`triton-cuda-tile-opt` 就是 TileIR 后端的这样一个工具。它的全部源码只有 14 行。它的存在意义在于：把 [u3-l1](u3-l1-pass-plugin-skeleton.md) 里那些「被 pybind 包进 `libtriton`、只能通过 Python JIT 间接触发」的 C++ 转换 pass，**解放成一个可独立运行的命令行程序**，从而可以拿任意一份 `.mlir` 当输入、肉眼观察转换前后的 IR 差异。这正是开发与调试转换 pass 的标准姿势。

#### 4.1.2 核心流程

从「一份 `.mlir` 输入」到「转换后的 IR 输出」，工具内部只做三步：

```
  triton-cuda-tile-opt foo.mlir --pass-pipeline="builtin.module(convert-triton-to-cuda-tile,...)"
          │
          ▼
  ① 构造 DialectRegistry
     - registerTritonCudaTileDialects(registry)   ← 装入 triton/cuda_tile/scf/cf/... 方言
                                                   + 注册 convert/assume/memtoken 三个 pass
     - registerFuseFMAPass() / registerLoopSplitPass()  ← 额外注册 cuda_tile 专属 pass
          │
          ▼
  ② MlirOptMain(argc, argv, "Triton-Cuda-Tile test driver\n", registry)
     - 解析命令行：读 %s 指向的文件、解析 --pass-pipeline 文本
     - -split-input-file：按 "// -----" 把文件切成多个独立用例
     - 依次构造并运行 PassManager
     - 打印变换后的 IR 到 stdout（供 | FileCheck 匹配）
          │
          ▼
  ③ 退出码：成功为 0，失败非 0（lit 据此判定用例通过与否）
```

注意第 ① 步里有个**区分**：`convert-triton-to-cuda-tile`、`rewrite-assume-with-cuda-tile`、`auto-gen-memory-token` 这三个 pass 是在 `registerTritonCudaTileDialects` 内部（通过 `registerAllPasses()` + 三个显式注册调用）装好的；而 `fuse-fma` 与 `loop-split` 这两个 cuda_tile 级 pass 是在 `main` 里**单独**显式注册的（[triton-cuda-tile-opt.cpp:9-10](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/tools/triton-cuda-tile-opt/triton-cuda-tile-opt.cpp#L9-L10)）。原因是它们来自 NVIDIA 的 `cuda_tile` 库，注册函数 `registerFuseFMAPass`/`registerLoopSplitPass` 由 cuda_tile 提供，需要单独调用——这也呼应了 [u3-l7](u3-l7-fma-fusion-and-bytecode.md) 里「fuse-fma 默认挂载、loop-split 预留未挂」的结论：在 opt 工具里两者都被暴露，方便单独试验。

#### 4.1.3 源码精读

**① 工具的全部源码。** [triton-cuda-tile-opt.cpp:6-14](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/tools/triton-cuda-tile-opt/triton-cuda-tile-opt.cpp#L6-L14) 就是这个工具的完整 `main`：

```cpp
int main(int argc, char **argv) {
  mlir::DialectRegistry registry;
  registerTritonCudaTileDialects(registry);
  mlir::cuda_tile::registerFuseFMAPass();
  mlir::cuda_tile::registerLoopSplitPass();

  return mlir::asMainReturnCode(mlir::MlirOptMain(
      argc, argv, "Triton-Cuda-Tile test driver\n", registry));
}
```

逐行看：第 7 行造一个空的 registry；第 8 行调用我们自己的注册函数把方言与 pass 灌进去；第 9–10 行补注册两个 cuda_tile pass；第 12–13 行把控制权交给 `MlirOptMain`，它的返回值经 `asMainReturnCode` 转成进程退出码。**没有任何业务逻辑**——所有真正的转换规则都在 pass 里，工具只是一个「能跑 pass 的壳」。

**② 注册函数装了什么。** [RegisterTritonCudaTileDialects.h:31-43](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/tools/triton-cuda-tile-opt/RegisterTritonCudaTileDialects.h#L31-L43) 的 `registerTritonCudaTileDialects` 做两件事：注册 pass、插入方言：

```cpp
inline void registerTritonCudaTileDialects(mlir::DialectRegistry &registry) {
  mlir::registerAllPasses();
  mlir::triton::registerConvertTritonToCudaTilePass();
  mlir::triton::registerRewriteAssumeWithCudaTilePass();
  mlir::triton::registerAutoGenMemoryTokenPass();
  registry.insert<mlir::cuda_tile::CudaTileDialect>();
  registry.insert<mlir::triton::TritonDialect, mlir::cf::ControlFlowDialect,
                  mlir::math::MathDialect, mlir::arith::ArithDialect,
                  mlir::scf::SCFDialect, mlir::gpu::GPUDialect,
                  mlir::LLVM::LLVMDialect, mlir::NVVM::NVVMDialect,
                  mlir::ub::UBDialect>();
}
```

- 第 32 行 `registerAllPasses()` 把 MLIR 内置的通用 pass（如 `reconcile-unrealized-casts`、`inline`、`cse` 等）全部注册——这就是为什么 RUN 行里能直接用 `reconcile-unrealized-casts`。
- 第 34–36 行注册 TileIR 自研的三个 pass（对应 [u3-l2](u3-l2-core-conversion-pass.md)、[u3-l5](u3-l5-rewrite-assume.md)、[u3-l6](u3-l6-memory-token.md)）。
- 第 37–42 行 `registry.insert<...>()` 声明「这个工具认识这些方言」。注意 `cuda_tile` 单独一行插入，其余 8 个方言（triton/cf/math/arith/scf/gpu/LLVM/NVVM/ub）一起插入——这正是转换前后会出现的全部方言集合：源端是 triton+arith+scf+cf 等，目标端是 cuda_tile，中间过渡还会用到 ub（[u3-l4](u3-l4-lift-cf-to-scf.md) 的不可达占位）。

**③ 怎么被构建出来。** [tools/.../CMakeLists.txt:4](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/tools/triton-cuda-tile-opt/CMakeLists.txt#L4) 用 LLVM 的 `add_llvm_executable` 把它编成可执行文件：

```cmake
add_llvm_executable(triton-cuda-tile-opt triton-cuda-tile-opt.cpp PARTIAL_SOURCES_INTENDED)
```

紧接着 [CMakeLists.txt:8-21](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/tools/triton-cuda-tile-opt/CMakeLists.txt#L8-L21) 的 `target_link_libraries` 链接了一串静态库，其中 `MLIROptLib` 提供 `MlirOptMain`，`TritonToTileIR` / `TritonTileIRTransforms` / `TritonTransforms` / `TritonNvidiaGPUTransforms` 提供各 pass 的实现，`MLIRRegisterAllDialects` / `MLIRRegisterAllPasses` 提供方言与内置 pass。而 `add_llvm_executable` 会把产物落到构建目录的 `bin/` 下——具体路径是 `<build>/bin/triton-cuda-tile-opt`（由 [third_party/tileir/CMakeLists.txt:99](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/CMakeLists.txt#L99) 的 `TRITON_CUDA_TILE_BINARY_DIR=${CMAKE_BINARY_DIR}/bin` 标定）。记住这个位置，4.2 节会用到。

#### 4.1.4 代码实践

**实践目标**：在不依赖 Python、不依赖 GPU 的前提下，亲手用 `triton-cuda-tile-opt` 跑通一次转换，观察 triton 方言被 lowering 成 cuda_tile 方言。

**操作步骤**：

1. 先确认构建目录在哪。按 [AGENTS.md:7](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/AGENTS.md#L7) 的方法推导 `BUILD_DIR`：

   ```bash
   # 在仓库根目录执行
   BUILD_DIR=$(PYTHONPATH="./python" python3 -c 'from build_helpers import get_cmake_dir; print(get_cmake_dir())')
   echo "$BUILD_DIR"
   ```

2. 只构建本工具（比全量 `make` 快得多）：

   ```bash
   cd "$BUILD_DIR"
   ninja triton-cuda-tile-opt
   ```

3. 拿仓库自带的 `op-conversion.mlir` 当输入，手动跑一遍。把它的 RUN 行（[op-conversion.mlir:1](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion.mlir#L1)）里的 `%s` 换成实际路径：

   ```bash
   bin/triton-cuda-tile-opt \
     third_party/tileir/test/FileCheck/op-conversion.mlir \
     -split-input-file \
     --pass-pipeline="builtin.module(convert-triton-to-cuda-tile,cuda_tile.module(cuda_tile.experimental\$func(fuse-fma)),reconcile-unrealized-casts)"
   ```

**需要观察的现象**：stdout 会打印出转换后的 IR——你会看到 `tt.func` / `tt.load` 等 triton 方言算子变成了 `cuda_tile` 方言的算子，并且外层多出了 `cuda_tile.module` 与 `cuda_tile.experimental$func` 两个容器 op（这正是 [u3-l7](u3-l7-fma-fusion-and-bytecode.md) 讲的「主转换插入容器」的直观证据）。

**预期结果**：命令退出码为 0，屏幕上是一段合法的 cuda_tile IR。如果你想看到「转换前」的对照，去掉整个 `--pass-pipeline=...` 参数再跑一次，工具会原样把输入 IR 打印出来（`MlirOptMain` 不带 pipeline 时只做 round-trip 打印）。

> 如果尚未本地构建，无法确认上述命令的确切输出，请标注「待本地验证」后再下结论——不要假设具体算子名。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `triton-cuda-tile-opt.cpp` 只有 14 行，却能支持 `-split-input-file`、`--pass-pipeline`、`| FileCheck`、`--run-reproducer` 这么多特性？

**参考答案**：因为这些特性的实现全部在 MLIR 的 `MlirOptMain`（由 `MLIROptLib` 提供）里。本工具只负责「注册自家方言与 pass」，然后把 `argc/argv` 原样交给 `MlirOptMain`，所有命令行能力都是它自带的。

**练习 2**：RUN 行里能直接写 `reconcile-unrealized-casts`（一个 MLIR 内置 pass），却没在 `main` 里显式 `register`，为什么它能被识别？

**参考答案**：因为 [RegisterTritonCudaTileDialects.h:32](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/tools/triton-cuda-tile-opt/RegisterTritonCudaTileDialects.h#L32) 调用了 `mlir::registerAllPasses()`，它一次性注册了 MLIR 全部内置 pass，`reconcile-unrealized-casts` 包含在内。而 `fuse-fma` / `loop-split` 属于 cuda_tile 库、不在 `registerAllPasses()` 范围，故需在 `main` 里单独注册。

---

### 4.2 lit 测试框架的配置与运行

#### 4.2.1 概念说明

有了工具，还需要一套机制把它和成百上千个 `.mlir` 测试用例串起来批量执行——这就是 **lit**（LLVM Integrated Tester）。lit 的核心思想极其简单：**每个测试文件就是一个小 shell 脚本**，文件里的 `// RUN: <命令>` 行就是lit 要执行的命令；命令退出码为 0 则该用例通过，非 0 则失败。lit 只负责发现文件、并发执行、汇总结果。

一个 `.mlir` 文件通常长这样：

```mlir
// RUN: triton-cuda-tile-opt %s -split-input-file --pass-pipeline="..." | FileCheck %s

module @my_kernel {
  tt.func public @my_kernel(...) { ... }
}

// CHECK-LABEL: @my_kernel
// CHECK: cuda_tile.xxx
```

- `%s` 是 lit 的内置替换，代表「当前测试文件自身的路径」。
- `| FileCheck %s` 把工具的 stdout 喂给 FileCheck，FileCheck 又从**同一个文件** `%s` 里读 `// CHECK:` 行当期望。
- 末尾的 `// CHECK:` 注释既是文档，也是断言。

本讲关心的三个层面是：lit 怎么**找到** `triton-cuda-tile-opt` 这个二进制（PATH 配置）、怎么**批量**运行它们（CMake 目标）、以及一份文件怎么**拆成**多个用例（`-split-input-file`）。

#### 4.2.2 核心流程

lit 运行一个 FileCheck 测试的完整链路：

```
  lit 发现 foo.mlir（后缀 .mlir/.ll 才算测试文件）
          │
          ▼
  解析 // RUN: 行，做 % 替换：
     %s        → foo.mlir 自身路径
     triton-cuda-tile-opt → 在 PATH 里查找（PATH 含 <build>/bin）
          │
          ▼
  执行：triton-cuda-tile-opt foo.mlir --pass-pipeline="..." | FileCheck foo.mlir
          │                                   │
          ▼                                   ▼
  工具读 .mlir、跑 pass、打印变换后 IR     FileCheck 读同一文件的 // CHECK 行做匹配
          │
          ▼
  管道退出码（FileCheck 的退出码）→ lit 判定用例 PASS / FAIL
```

关键点在「PATH 里查找」这一步。本仓库的主 lit 配置 [test/lit.cfg.py:53-57](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/test/lit.cfg.py#L53-L57) 把构建产物目录加进了 PATH：

```python
tool_dirs = [config.triton_tools_dir, config.llvm_tools_dir, config.filecheck_dir]
for d in tool_dirs:
    llvm_config.with_environment('PATH', d, append_path=True)
```

其中 [test/lit.cfg.py:45](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/test/lit.cfg.py#L45) 定义 `config.triton_tools_dir = os.path.join(config.triton_obj_root, 'bin')`，即 `<build>/bin`。而 4.1.3 节已确认 `triton-cuda-tile-opt` 正好落在 `<build>/bin`——所以 RUN 行里直接写裸命令名就能被找到，**不需要**写 `%triton-cuda-tile-opt` 这样的 lit 替换符号（对比 [test/lit.cfg.py:58-64](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/test/lit.cfg.py#L58-L64) 里登记的 `triton-opt` 等，那些是 `%`-替换工具；本工具靠 PATH）。

> **关于 tileir 专属 lit 配置的一个实情**：[third_party/tileir/test/lit.cfg.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/lit.cfg.py) 与 [third_party/tileir/test/lit.site.cfg.py.in](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/lit.site.cfg.py.in) 在当前 HEAD 下是**空文件**（0 字节）。这意味着 tileir 测试套件的独立 lit 配置尚未填充，工具的 PATH 解析实际上依赖上游主配置 `test/lit.cfg.py` 的机制。因此在本地复现单个 FileCheck 用例时，**最稳妥、不依赖 lit 配置是否完备的方式是直接调用 `bin/triton-cuda-tile-opt`**（见 4.1.4 实践），或使用 4.2.3 节的 CMake 聚合目标。

#### 4.2.3 源码精读

**① CMake 怎么注册 lit 套件。** [third_party/tileir/test/CMakeLists.txt:11-15](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/CMakeLists.txt#L11-L15) 声明测试依赖的三个工具：

```cmake
set(TRITON_CUDA_TILE_TEST_DEPENDS
  triton-cuda-tile-opt
  triton-opt
  triton-llvm-opt
)
```

接着 [CMakeLists.txt:17-18](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/CMakeLists.txt#L17-L18) 指定 FileCheck 的位置并把它作为 lit 参数传入：

```cmake
set(FILECHECK_PATH "${LLVM_LIBRARY_DIR}/../bin/FileCheck")
set(LIT_ARGS "-Dfilecheck=${FILECHECK_PATH}")
```

最后用两个标准宏把套件注册成 ninja 目标——[CMakeLists.txt:28-31](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/CMakeLists.txt#L28-L31) 注册聚合目标 `check-triton-cuda-tile`，[CMakeLists.txt:34-36](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/CMakeLists.txt#L34-L36) 扫描源码目录收集全部 `.mlir` 用例：

```cmake
add_lit_testsuite(check-triton-cuda-tile "Running the triton-cuda-tile tests"
  ${CMAKE_CURRENT_BINARY_DIR}
  DEPENDS ${TRITON_CUDA_TILE_TEST_DEPENDS})
```

`DEPENDS` 保证了「跑测试前先编出 `triton-cuda-tile-opt` 等工具」。另外 [CMakeLists.txt:24-26](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/CMakeLists.txt#L24-L26) 还有一个 `check-triton-cuda-tile-build-only` 目标，**只构建依赖、不执行测试**——CI 拆分「构建」与「测试」两阶段时用它。

**② 怎么手动跑单个用例。** 官方工作流见 [AGENTS.md:8](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/AGENTS.md#L8)，核心是「进构建目录、用 lit 指向用例路径」：

```bash
cd BUILD_DIR
ninja triton-cuda-tile-opt          # 先把工具编出来
lit -v third_party/tileir/test/FileCheck/op-conversion.mlir
```

`-v` 表示 verbose，会打印每个用例实际执行的的那条 RUN 命令，非常利于调试。[AGENTS.md:9](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/AGENTS.md#L9) 特别强调：「Lit tests can be run locally (no GPU required).」——这也是本讲全部实践不需要 GPU 的根据。

**③ `-split-input-file` 与 `// -----`。** 这是几乎每个 tileir 测试都用到的机制。它让一个 `.mlir` 文件可以装**多个独立用例**：工具遇到 `// -----` 分隔符就把文件切断，对每一段分别跑完整 pipeline，最后把各段输出拼起来。好处是一个文件能集中展示同一 pass 在多种输入下的行为（例如 [op-conversion-barrier.mlir](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-barrier.mlir) 里就用 `// -----`（[第 34 行](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-barrier.mlir#L34)）隔开了 `test_barrier_add_kernel` 与 `test_barrier_layer_norm_bwd` 两个 kernel）。对应的 FileCheck 期望也按段写、靠 `CHECK-LABEL` 锚定到具体函数名。

#### 4.2.4 代码实践

**实践目标**：用 CMake 聚合目标一键构建并运行整个 tileir FileCheck 套件，理解「构建目标」与「测试目标」的关系。

**操作步骤**：

1. 推导 `BUILD_DIR` 并进入（同 4.1.4 步骤 1）。
2. 只构建依赖（不跑测试），验证工具链齐全：

   ```bash
   cd "$BUILD_DIR"
   ninja check-triton-cuda-tile-build-only
   ```

3. 构建并运行整个套件：

   ```bash
   ninja check-triton-cuda-tile
   ```

**需要观察的现象**：ninja 先编译 `triton-cuda-tile-opt` / `triton-opt` / `triton-llvm-opt`（因为 `DEPENDS`），随后 lit 扫描 `third_party/tileir/test/` 下所有 `.mlir`，逐个执行 RUN 行并汇总 PASS/FAIL 计数。

**预期结果**：全部用例 PASS，末尾形如 `Expected Passes    : N`。若某条失败，加 `-v` 重跑（`lit -v <失败用例路径>`）可看到实际执行的命令与 FileCheck 的 mismatch 详情。

> 若本地尚未配置好 cuda-tile 依赖（见 [u4-l4](u4-l4-build-and-cuda-tile-deps.md)），`ninja check-triton-cuda-tile` 可能无法完成构建；此时退回到 4.1.4 的「直接调用 `bin/triton-cuda-tile-opt`」单条命令，不依赖 lit 套件配置。结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：RUN 行写的是裸命令 `triton-cuda-tile-opt`，而不是 `%triton-cuda-tile-opt`。lit 凭什么能找到它？

**参考答案**：主 lit 配置 [test/lit.cfg.py:56-57](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/test/lit.cfg.py#L56-L57) 把 `config.triton_tools_dir`（即 `<build>/bin`）追加进了环境变量 `PATH`，而该工具由 `add_llvm_executable` 落在 `<build>/bin`（[CMakeLists.txt:99](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/CMakeLists.txt#L99)）。裸命令经 PATH 命中，无需 `%` 替换。

**练习 2**：`check-triton-cuda-tile-build-only` 与 `check-triton-cuda-tile` 的区别是什么？什么场景下会用前者？

**参考答案**：前者只构建测试依赖的工具（`DEPENDS` 那三个），不执行任何测试；后者构建后还跑全部 lit 用例。CI 想把「构建阶段」和「测试阶段」拆到不同机器/步骤时，用 `build-only` 先确保产物就绪，再在测试节点跑完整目标。

---

### 4.3 FileCheck 匹配语法

#### 4.3.1 概念说明

`MlirOptMain` 把变换后的 IR 打到 stdout 后，怎么判断「转换结果正确」？答案是 **FileCheck**——一个「按行顺序、支持正则与变量」的输出匹配器。它从测试文件里读所有 `// CHECK: <模式>` 行，依次在 stdout 里查找；每条 `CHECK` 必须按出现顺序、在输出里**后续**位置命中，否则报错。

FileCheck 不是逐行精确比对（那样 IR 一改就全挂），而是「松散有序」的：中间可以有任意未匹配的行，只要每条 `CHECK` 都能在前一条命中点之后找到即可。这套语法是 LLVM 全家桶通用的，掌握了它在任何 MLIR/LLVM 项目里都通用。

#### 4.3.2 核心流程

FileCheck 的工作过程：

```
  被测程序 stdout（变换后的 IR，长篇文本）
          │
          ▼
  FileCheck 读取 foo.mlir 里的 // CHECK 行，逐条处理：
    CHECK / CHECK-LABEL / CHECK-NOT / CHECK-NEXT / CHECK-SAME ...
          │
          ▼
  对每条 CHECK：在「上一条命中点之后」的输出里查找模式
    - 普通文本：字面匹配
    - {{正则}}：按正则匹配
    - %[[NAME:.*]]：正则捕获并命名
    - %[NAME]   ：引用之前捕获的值
          │
          ▼
  全部命中 → 退出码 0（用例 PASS）；任一未命中 → 退出码非 0 + 打印 mismatch 行
```

一个关键环境变量：主 lit 配置 [test/lit.cfg.py:51](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/test/lit.cfg.py#L51) 设了 `config.environment["FILECHECK_OPTS"] = "--enable-var-scope"`。它的含义见下文「变量作用域」。

#### 4.3.3 源码精读（结合真实测试）

**① 最常用三件套：`CHECK-LABEL` + `CHECK` + 变量捕获。** 看 [op-conversion-barrier.mlir:28-32](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-barrier.mlir#L28-L32)：

```
// CHECK-LABEL: @test_barrier_add_kernel
// CHECK-NOT: gpu.barrier
// CHECK: %[[RESULT1:.*]], %[[TOKEN1:.*]] = load_ptr_tko
// CHECK: %[[RESULT2:.*]], %[[TOKEN2:.*]] = load_ptr_tko {{.*}}token=%[[TOKEN1]]
// CHECK: %[[TOKEN3:.*]] = store_ptr_tko {{.*}}token=%[[TOKEN2]]
```

逐条解读：

- `CHECK-LABEL`：一个「分界锚」。它通常匹配函数/模块名（这里是 `@test_barrier_add_kernel`），作用是**把它之后的 CHECK 限定在该 label 命中的输出区段内**。配合 `--enable-var-scope`，跨过一个新的 `CHECK-LABEL` 后，上一段定义的 `%[[变量]]` 会失效，避免不同用例间变量串扰。
- `CHECK-NOT: gpu.barrier`：断言「从本条到下一条正向 CHECK 之间，输出里**不得出现** `gpu.barrier`」。这正是 [u3-l6](u3-l6-memory-token.md) 讲的「debug_barrier 的 `gpu.barrier` 被 auto-gen-memory-token 删除」的可验证证据。
- `CHECK: %[[RESULT1:.*]], %[[TOKEN1:.*]] = load_ptr_tko`：`%[[NAME:.*]]` 是**正则捕获**——`.*` 匹配任意文本，并把匹配到的值绑定到名字 `RESULT1` / `TOKEN1`。这里断言转换后生成了形如 `<结果>, <token> = load_ptr_tko ...` 的 op。
- `CHECK: ... token=%[[TOKEN1]]`：`%[[TOKEN1]]`（无 `:`）是**引用**——要求此处输出的值等于之前捕获的 `TOKEN1`。这条断言把「第二个 load 的输入 token = 第一个 load 的输出 token」钉死，从而验证了 memory token 的串行化接力（[u3-l6](u3-l6-memory-token.md) 的核心语义）。
- `{{.*}}`：裸正则块（不捕获），用来跳过不关心的片段（如其它参数）。

**② 多前缀：一份文件测多组 pass 选项。** [op-conversion-modifiers.mlir:1-5](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-modifiers.mlir#L1-L5) 用 5 条 RUN，每条带不同的 pass 选项、对应不同的 `--check-prefix`：

```
// RUN: triton-cuda-tile-opt %s ... --pass-pipeline="builtin.module(convert-triton-to-cuda-tile{approx-modifier=true flush-to-zero-modifier=true},...)" | FileCheck --check-prefix=APPROX_FTZ %s
// RUN: ... --pass-pipeline="builtin.module(convert-triton-to-cuda-tile{approx-modifier=true},...)"            | FileCheck --check-prefix=APPROX %s
// RUN: ... --pass-pipeline="builtin.module(convert-triton-to-cuda-tile{flush-to-zero-modifier=true},...)"      | FileCheck --check-prefix=FTZ %s
// RUN: ... --pass-pipeline="builtin.module(convert-triton-to-cuda-tile{compute-capability=100 num-cta-in-cga=2},...)" | FileCheck --check-prefix=HINT-100 %s
// RUN: ... --pass-pipeline="builtin.module(convert-triton-to-cuda-tile{compute-capability=120 num-cta-in-cga=4},...)" | FileCheck --check-prefix=HINT-120 %s
```

这里有两个要点：

- **pass 选项的内联语法**：`convert-triton-to-cuda-tile{key=value key=value}` 把 [u3-l1](u3-l1-pass-plugin-skeleton.md) 讲的 `Passes.td` 选项（`approx-modifier` / `flush-to-zero-modifier` / `compute-capability` / `num-cta-in-cga`）在 pipeline 文本里直接赋值——这正是 opt 工具相对 Python JIT 的优势：**不必走环境变量，命令行就能逐选项切换**。
- **`--check-prefix=APPROX_FTZ`**：让 FileCheck 只读 `// APPROX_FTZ:` 开头的期望行（而非默认的 `// CHECK:`）。于是同一份输入 IR、同一组期望，可以分别验证「开 approx+ftz」「只开 approx」「只开 ftz」等多种组合，互不干扰。这 5 个选项对应 [u2-l2](u2-l2-options-and-env-config.md) 讲的 `enable_approx` / `enable_ftz` 与 [u2-l3](u2-l3-compile-stages.md) 讲的 occupancy/num_ctas 等旋钮。

**③ 不走 FileCheck、改走「断言报错」。** [op-conversion-xfailure.mlir:1](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-xfailure.mlir#L1) 是另一类写法：

```
// RUN: triton-cuda-tile-opt %s -split-input-file --pass-pipeline="..." -verify-diagnostics
```

注意它**没有 `| FileCheck %s`**，而是加了 `-verify-diagnostics`。这是 MLIR 的「预期诊断」机制：开启后，文件里的 `// expected-error{{...}}` / `// expected-warning{{...}}` 注释会被当作「应当在此处报这条错」的断言；pass 主动触发的错误必须与这些注解一一对应，多报或少报都算失败。它用于测试「不合法的输入应当被拒绝」这类负向用例（比如残留非法方言时 [u3-l1](u3-l1-pass-plugin-skeleton.md) 讲的 `applyFullConversion` 报错）。`-verify-diagnostics` 由 `MlirOptMain` 内置提供。

#### 4.3.4 代码实践

**实践目标**：亲手读懂一份带 `CHECK` 的测试，并用 opt 工具复现它、解释 RUN 行 pass-pipeline 的串联顺序。

**操作步骤**：

1. 打开 [op-conversion.mlir:1](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion.mlir#L1)，其 RUN 行是：

   ```
   builtin.module(
     convert-triton-to-cuda-tile,
     cuda_tile.module(
       cuda_tile.experimental$func(
         fuse-fma
       )
     ),
     reconcile-unrealized-casts
   )
   ```

2. 把 pipeline 的串联顺序写成一段文字说明（这就是本实践的交付物）：

   - 最外层 `builtin.module(...)`：在最顶层 builtin 模块上依次跑括号里的 pass。
   - **第 1 个** `convert-triton-to-cuda-tile`：主转换。把 triton 方言 lowering 成 cuda_tile 方言，并**插入** `cuda_tile.module` 容器与 `cuda_tile.experimental$func` 入口 op（[u3-l2](u3-l2-core-conversion-pass.md) / [u3-l7](u3-l7-fma-fusion-and-bytecode.md)）。这一步之后 IR 已经「搬家」到 cuda_tile 容器里了。
   - **第 2 个** `cuda_tile.module(...)`：**下沉**进刚刚插入的 `cuda_tile.module` 容器内部继续跑 pass——不这样写，pass 就找不到目标 op。
   - **第 3 个** `cuda_tile.experimental$func(...)`：再**下沉一层**进每个 kernel 入口（助记符 `experimental$func`）。`fuse-fma` 必须在这一层跑，因为它操作的是 kernel 内部的算术（[u3-l7](u3-l7-fma-fusion-and-bytecode.md)）。两层 `nest` 对应两层 `容器op(...)` 嵌套。
   - **第 4 个** `reconcile-unrealized-casts`：回到 builtin.module 层（已退出两层嵌套），清理转换过程留下的 `unrealized_conversion_cast` 桥接 op（[u3-l5](u3-l5-rewrite-assume.md) 提到过它作类型跳板）。这是 MLIR 内置 pass（靠 `registerAllPasses()`）。
   - 注意 `experimental\$func` 里的反斜杠：在 shell 双引号字符串里 `$func` 会被当成变量展开，故用 `\$` 转义，传给工具的实际是 `experimental$func`。

3. 对照 [op-conversion-barrier.mlir:1](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-barrier.mlir#L1)，它的 pipeline 末尾多挂了 `auto-gen-memory-token`——说明 token 生成必须在主转换之后、且不需要再下沉（它在 cuda_tile.module 这层对全部访存造 token，故紧跟在 `reconcile-unrealized-casts` 后即可）。

**需要观察的现象**：你会理解「pipeline 文本里的嵌套括号 = PassManager 的 `nest<>()` 下沉」，并能解释为什么 `fuse-fma` 必须写在内层、而 `reconcile-unrealized-casts` 写在最外层。

**预期结果**：能用自己的话讲清四个 pass 各自的层位与先后理由。这是看懂所有 tileir lit 测试 RUN 行的万能钥匙。

> 若要在机器上验证理解，可把 [op-conversion-barrier.mlir](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-barrier.mlir) 喂给 4.1.4 构建出的 `bin/triton-cuda-tile-opt`（带上它的 RUN 行 pipeline），肉眼对照输出与 `CHECK` 行是否吻合。具体输出「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`%[[TOKEN1:.*]]` 和 `%[[TOKEN1]]` 在 FileCheck 里有什么区别？

**参考答案**：带 `:.*` 的是**定义**——用正则 `.*` 匹配一段文本并把它绑定到名字 `TOKEN1`；不带 `:.*` 的是**引用**——要求此处输出的值必须等于先前绑定的 `TOKEN1` 的值。在本讲例子里，前者捕获第一次 load 产出的 token 名，后者断言第二次 load 的输入 token 就是它，从而验证 token 接力。

**练习 2**：为什么 `--enable-var-scope`（[test/lit.cfg.py:51](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/test/lit.cfg.py#L51)）默认开启？如果不开会怎样？

**参考答案**：它让 `%[[变量]]` 的作用域局限在两个 `CHECK-LABEL` 之间，跨过新 label 即失效。不开的话，变量会跨整个文件存活，容易在多个用例间意外复用同名捕获、造成「假通过」。MLIR 官方测试默认开启正是为此；若确需跨 label 复用，可用 `$` 前缀（如 `%[[$GLOBAL]]`）显式声明全局变量。

**练习 3**：[op-conversion-modifiers.mlir](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-modifiers.mlir) 里 5 条 RUN 都没有用默认的 `// CHECK:`，而是 `// APPROX_FTZ:` 等。为什么同一文件能并存 5 套互不干扰的期望？

**参考答案**：每条 RUN 用 `--check-prefix=XXX` 指定 FileCheck 只识别 `// XXX:` 注释。于是 5 条 RUN 各自只匹配自己前缀的期望行，等价于把 5 个独立测试塞进一个文件，共用同一份输入 IR。

## 5. 综合实践

把本讲三块知识（opt 工具、lit 运行、FileCheck）串起来，完成下面这个**端到端复现任务**。

**场景**：你改了 `convert-triton-to-cuda-tile` 的某个 lowering pattern，想确认改动没破坏现有的算子转换行为。

**任务**：

1. 按 [AGENTS.md:7-8](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/AGENTS.md#L7-L8) 推导 `BUILD_DIR`，执行 `cd "$BUILD_DIR" && ninja triton-cuda-tile-opt` 构建工具。
2. 选取 [op-conversion-barrier.mlir](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/test/FileCheck/op-conversion-barrier.mlir) 作为目标，先**直接调用工具**（不走 lit）观察输出：

   ```bash
   cd "$BUILD_DIR"
   bin/triton-cuda-tile-opt third_party/tileir/test/FileCheck/op-conversion-barrier.mlir \
     -split-input-file \
     --pass-pipeline="builtin.module(convert-triton-to-cuda-tile,cuda_tile.module(cuda_tile.experimental\$func(fuse-fma)),reconcile-unrealized-casts,auto-gen-memory-token)"
   ```

3. 在输出里人工核对 4.3.3 节那 5 条 `CHECK`：确认 `gpu.barrier` 已消失、`load_ptr_tko` / `store_ptr_tko` 带着接力 token 依次出现。
4. 再用 lit 跑同一文件，让 FileCheck 自动裁定：

   ```bash
   lit -v third_party/tileir/test/FileCheck/op-conversion-barrier.mlir
   ```

5. **解释 RUN 行 pipeline 的串联顺序**：写出 4 个 pass（`convert-triton-to-cuda-tile` → `cuda_tile.module(...)` → `cuda_tile.experimental$func(fuse-fma)` → `reconcile-unrealized-casts` → `auto-gen-memory-token`）各自在哪一层、为什么必须这个顺序（参考 4.3.4 步骤 2 的要点）。

**交付物**：(a) 上面两条命令的执行记录；(b) 一段文字解释 pipeline 串联顺序与各 pass 层位；(c) 若有任何 mismatch，定位是哪个 `CHECK` 没命中并推测原因。全部「待本地验证」直至你真正在机器上跑过。

**进阶（reproducer 复现）**：按 [AGENTS.md:10](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/AGENTS.md#L10) 的方法——若某次 Python JIT 编译崩溃并打印了带 `mlir_reproducer` 的完整 IR，把它连同 `{-# ... #-}` 元数据存为 `/tmp/repro.mlir`，再用 `bin/triton-opt /tmp/repro.mlir --run-reproducer` 离线复现崩溃点（注意复现 MLIR reproducer 用的是 `triton-opt`，不是 `triton-cuda-tile-opt`；二者共享同一套 `MlirOptMain` 骨架）。

## 6. 本讲小结

- `triton-cuda-tile-opt` 的全部源码只有 14 行：造一个 `DialectRegistry`、调用 `registerTritonCudaTileDialects` 装入 triton/cuda_tile 等方言与三个自研 pass、再补注册 `fuse-fma`/`loop-split`，最后交给 MLIR 的 `MlirOptMain`——所有命令行能力（`--pass-pipeline`、`-split-input-file`、`| FileCheck`、`-verify-diagnostics`）都由 `MlirOptMain` 内置。
- 它由 `add_llvm_executable` 构建并落到 `<build>/bin`（[CMakeLists.txt:99](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/CMakeLists.txt#L99)），lit 通过主配置把该目录加入 PATH 来定位它（裸命令名即可，无需 `%` 替换）。
- lit 把每个 `.mlir` 当独立 shell 脚本跑其 `// RUN` 行；CMake 用 `check-triton-cuda-tile`（跑测试）与 `check-triton-cuda-tile-build-only`（只编工具）两个目标聚合，依赖 `triton-cuda-tile-opt`/`triton-opt`/`triton-llvm-opt`；FileCheck 二进制由 `${LLVM_LIBRARY_DIR}/../bin/FileCheck` 提供。全部 lit 测试**不需要 GPU**。
- `-split-input-file` 配合 `// -----` 让一份文件装多个独立用例，`CHECK-LABEL` 锚定到具体函数名划定匹配区段，`--enable-var-scope` 限制变量跨 label 失效。
- FileCheck 语法核心：`%[[NAME:.*]]` 捕获、`%[[NAME]]` 引用、`{{正则}}` 跳过、`CHECK-NOT` 断言「不出现」、`--check-prefix=XXX` 让一份文件并存多套期望、`{key=value}` 在 pipeline 文本里直接给 pass 选项赋值。
- pipeline 文本的嵌套括号即 PassManager 的 `nest<>()` 下沉：主转换 `convert-triton-to-cuda-tile` 插入 `cuda_tile.module`/`experimental$func` 容器后，`fuse-fma` 须下沉两层进 kernel 内、`reconcile-unrealized-casts` 与 `auto-gen-memory-token` 则回到外层——顺序由各 pass 作用的方言与所在 IR 层位决定。

## 7. 下一步学习建议

本讲建立的是「怎么测」的能力，接下来建议：

1. **续读 [u4-l2 性能调优实践](u4-l2-performance-tuning.md)**：本讲的 `--check-prefix` 多组测试写法，正是 autotune 多组旋钮对比的雏形；u4-l2 会把 occupancy/num_ctas/num_stages 这些旋钮放到真实 kernel 里讲。
2. **续读 [u4-l3 fallback 容错](u4-l3-fallback-mechanism.md)**：本讲的 `triton-cuda-tile-opt` 用于「编译期 pass 调试」，u4-l3 讲「运行期编译失败如何回退 PTX」，二者一前一后覆盖了 TileIR 的全部容错面。
3. **续读 [u4-l4 构建系统](u4-l4-build-and-cuda-tile-deps.md)**：本讲频繁出现的 `<build>/bin`、`add_llvm_executable`、静态库链接、FileCheck 路径，其背后的 cuda-tile 克隆/补丁/链接细节在 u4-l4 系统讲解。
4. **动手扩展一个 lit 用例**：挑一个尚未被 FileCheck 覆盖的算子（参考 [u1-l1](u1-l1-project-overview.md) 列出的「已知不支持算子」反例），仿照 `op-conversion-barrier.mlir` 写一个最小 `.mlir`，用本讲的 pipeline 跑出转换结果、加 `CHECK` 锁定关键 op，作为你对转换 pass 的第一个回归测试。
