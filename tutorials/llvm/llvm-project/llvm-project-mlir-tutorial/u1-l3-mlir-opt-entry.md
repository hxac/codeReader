# 工具链入口 mlir-opt 与运行第一个程序

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `mlir-opt` 这个命令行工具的作用：加载一段 MLIR 文本/字节码，可选地跑一组 pass，再把结果打印出来。
- 读懂 [`tools/mlir-opt/mlir-opt.cpp`](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/tools/mlir-opt/mlir-opt.cpp) 里的 `main` 函数，理清它「注册 dialect / pass → 构造 registry → 进入 `MlirOptMain`」的执行顺序。
- 理解 dialect（方言）和 pass（变换）的「注册」到底注册了什么、为什么必须先注册。
- 掌握在命令行用 `mlir-opt` 处理一个 `.mlir` 文件的常见用法，包括 `--pass-pipeline`、`--help`、`--emit-bytecode` 等关键参数。
- 能把完整的 `mlir-opt` 与最小化版本 `mlir-minimal-opt` 做对比，说清楚后者「精简了什么」。

本讲只读源码、不改源码。所有命令行示例若你尚未构建 MLIR，对应位置标注为「待本地验证」。

## 2. 前置知识

在进入源码前，先用大白话把几个本讲会反复出现的概念交代清楚（精确定义留给后续讲义）。

- **IR（中间表示）**：编译器在「源码」和「机器码」之间用的内部数据结构。MLIR 的 IR 在内存里是一棵由 Operation 组成的树，落到磁盘上可以写成「人能读的文本」（`.mlir` 文件），也可以写成「紧凑的二进制」（字节码）。
- **dialect（方言）**：一组 Operation / Type / Attribute 的命名空间，比如 `arith`（算术）、`func`（函数）、`llvm`（对应 LLVM IR）。在第一讲里已经建立过这个心智模型。
- **pass（变换/优化 pass）**：一个「吃进 IR、吐出改写后 IR」的处理单元，比如 `cse`（公共子表达式消除）、`canonicalize`（规范化）。本讲不关心 pass 内部怎么写，只关心它如何被「注册」和「被命令行调度」。
- **registry（注册表）**：一个记录「本工具认识哪些 dialect」的容器。`mlir-opt` 在解析输入文件前，必须先告诉它「你能解析哪些方言」，否则遇到陌生操作会报错。
- **main 函数 / 入口**：一个 C++ 程序运行时第一个被执行的函数。本讲的核心就是追踪 `mlir-opt` 的 `main` 一路调用了什么。

> 名词对照：`mlir-opt` 既是「一个可执行文件」（构建产物 `build/bin/mlir-opt`），也是「一类工具的统称」。任何调用 `MlirOptMain` 的二进制（比如 `standalone-opt`、`mlir-minimal-opt`）都遵循同一套入口约定。本讲在需要区分时会写明。

## 3. 本讲源码地图

本讲涉及的文件按「从外到内」的调用顺序排列：

| 文件 | 作用 |
| --- | --- |
| [tools/mlir-opt/mlir-opt.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/tools/mlir-opt/mlir-opt.cpp) | `mlir-opt` 这个二进制的 `main` 函数，负责「注册 + 进入 `MlirOptMain`」。本讲主角。 |
| [include/mlir/InitAllDialects.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/InitAllDialects.h) | 声明 `registerAllDialects`，把 MLIR 自带的所有 dialect 加进 registry。 |
| [include/mlir/InitAllPasses.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/InitAllPasses.h) | 声明 `registerAllPasses`，把自带的所有 pass 注册进**全局** pass 注册表。 |
| [include/mlir/Tools/mlir-opt/MlirOptMain.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/Tools/mlir-opt/MlirOptMain.h) | 声明 `MlirOptMain`、`asMainReturnCode` 和 `MlirOptMainConfig`，是「类 mlir-opt 工具」的公共入口契约。 |
| [lib/Tools/mlir-opt/MlirOptMain.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/Tools/mlir-opt/MlirOptMain.cpp) | `MlirOptMain` 的真正实现：解析命令行、读输入文件、跑 pass、打印输出。 |
| [examples/minimal-opt/mlir-minimal-opt.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/examples/minimal-opt/mlir-minimal-opt.cpp) | 最小化的 opt 入口，用来对比「什么才是 mlir-opt 真正必不可少的骨架」。 |
| [examples/standalone/standalone-opt/standalone-opt.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/examples/standalone/standalone-opt/standalone-opt.cpp) | out-of-tree（项目外）方言自带 opt 工具的范例，演示如何只注册「自己需要的」dialect。 |

记住一条主线：**`main` → 注册 → `MlirOptMain` → 解析/跑 pass/打印**。后面三个模块都在拆这条主线。

## 4. 核心概念与源码讲解

### 4.1 mlir-opt 的定位与入口骨架

#### 4.1.1 概念说明

`mlir-opt` 是 MLIR 自带的「命令行试验台」。它的职责非常克制：

1. 从文件或标准输入读入一段 MLIR IR（文本或字节码）；
2. **可选地**在这段 IR 上跑一组 pass（也可以一个都不跑）；
3. 把结果 IR 打印回标准输出（或写文件）。

它本身**不是**一个完整编译器，不产生可执行文件。官方文档原话：它是一个「为单元测试设计的工具」。换句话说，它是开发者验证「我的 IR 写得对不对」「我这个 pass 跑出来是什么样」的最快路径。

理解这一点后你就明白：`mlir-opt` 的 `main` 不该有任何「编译逻辑」。它只做两件事——**把能力注册好**，然后**把控制权交给 `MlirOptMain`**。所有真正的活儿都在库里，`main` 只是个「装配工」。

#### 4.1.2 核心流程

`mlir-opt` 二进制 `main` 的执行流程可以概括为下面四步：

```text
main(argc, argv)
  │
  ├─① registerAllPasses()             // 把所有 pass 注册进「全局 pass 注册表」
  │     （可选）registerTestPasses()    // 仅在开启测试构建时
  │
  ├─② 构造 DialectRegistry registry
  │     registerAllDialects(registry)        // 注册所有 dialect
  │     registerAllExtensions(registry)      // 注册方言扩展
  │     registerAllGPUToLLVMIRTranslations(registry)
  │
  └─③ MlirOptMain(argc, argv, "MLIR modular optimizer driver\n", registry)
           │
           └─④ asMainReturnCode(...)   // LogicalResult → 进程退出码 (0 / 1)
```

注意第 ① 步和第 ② 步注册的是**两个不同的东西**：pass 注册进的是一个**全局静态注册表**（不带参数），dialect 注册进的是**你手里这个 registry 对象**（带参数）。这个区别在 4.2 节会展开。

#### 4.1.3 源码精读

整个 `main` 函数非常短。下面是去除测试相关分支后的核心：

[tools/mlir-opt/mlir-opt.cpp:323-347](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/tools/mlir-opt/mlir-opt.cpp#L323-L347) —— 这是 `mlir-opt` 的 `main`，先注册 pass，再构造并填充 `registry`，最后把一切交给 `MlirOptMain`：

```cpp
int main(int argc, char **argv) {
  registerAllPasses();
#ifdef MLIR_INCLUDE_TESTS
  registerTestPasses();
#endif
  DialectRegistry registry;
  registerAllDialects(registry);
  registerAllExtensions(registry);

  // TODO: Remove this and the corresponding ... dependency when a safe
  // dialect interface registration mechanism is implemented ...
  registerAllGPUToLLVMIRTranslations(registry);

#ifdef MLIR_INCLUDE_TESTS
  ::test::registerIrdlTestDialect(registry);
  ...
#endif
  return mlir::asMainReturnCode(mlir::MlirOptMain(
      argc, argv, "MLIR modular optimizer driver\n", registry));
}
```

逐行解释：

- 第 324 行 `registerAllPasses();`：注册所有自带 pass（详见 4.2）。
- 第 328 行 `DialectRegistry registry;`：创建一个**空的**方言注册表。此刻它什么 dialect 都不认识。
- 第 329–335 行：往这个 `registry` 里塞东西——所有 dialect、所有方言扩展、以及 GPU→LLVM IR 的翻译接口。
- 第 345–346 行：调用 `MlirOptMain`，把命令行参数 `argc/argv`、一段用作 `--help` 标题的字符串、以及填满的 `registry` 一起传进去；再用 `asMainReturnCode` 把它的返回值转成进程退出码。

`asMainReturnCode` 是个极小的内联辅助函数，定义在头文件里，作用是把 MLIR 的 `LogicalResult`（成功/失败）翻译成 C 的退出码：

[include/mlir/Tools/mlir-opt/MlirOptMain.h:425-437](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/Tools/mlir-opt/MlirOptMain.h#L425-L437) —— `asMainReturnCode` 把 `LogicalResult` 映射为 `EXIT_SUCCESS`/`EXIT_FAILURE`：

```cpp
inline int asMainReturnCode(LogicalResult r) {
  return r.succeeded() ? EXIT_SUCCESS : EXIT_FAILURE;
}
```

> 为什么单独抽一个 `asMainReturnCode`？因为 `MlirOptMain` 返回的是 `LogicalResult`（MLIR 内部通用的「成功/失败」类型），而 C 的 `main` 必须返回 `int`。这个函数就是两者之间的适配器。任何想「复刻一个 mlir-opt」的工具，`main` 的最后一句几乎都是这个套路。

#### 4.1.4 代码实践

**实践目标**：建立「`main` 极薄、真正逻辑在 `MlirOptMain`」的直觉。

**操作步骤**：

1. 打开 [tools/mlir-opt/mlir-opt.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/tools/mlir-opt/mlir-opt.cpp)。
2. 数一数 `main` 函数体里**真正干活**的语句有几条（提示：去掉 `#ifdef MLIR_INCLUDE_TESTS` 包起来的测试分支后，核心就 5 条左右）。
3. 注意到 `main` 里**没有任何**读文件、解析、打印 IR 的代码——这些都藏在 `MlirOptMain` 里。

**需要观察的现象**：`main` 里能看到的动词几乎只有 `register...`（注册）和最后那个 `MlirOptMain(...)` 调用。

**预期结果**：你会确认 `mlir-opt` 的 `main` 是一个「装配函数」——它的全部价值在于「决定这个二进制认识哪些 dialect / pass」，而不是「怎么处理 IR」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `main` 里的 `registerAllDialects(registry);` 这一行删掉（保留其余不变），再用这个二进制去解析一段含 `arith.addi` 的 `.mlir`，会发生什么？

> **参考答案**：因为 `registry` 是空的，工具不认识 `arith` 方言。默认情况下解析会报「使用了未注册的方言」的错误（除非命令行加了 `--allow-unregistered-dialect`，参见 4.4）。这正是「必须先注册才能解析」的体现。

**练习 2**：`asMainReturnCode(success())` 和 `asMainReturnCode(failure())` 分别返回什么？

> **参考答案**：分别返回 `EXIT_SUCCESS`（即 0）和 `EXIT_FAILURE`（即 1），对应 shell 里「命令成功/失败」的退出码约定。

---

### 4.2 dialect 与 pass 的注册流程

#### 4.2.1 概念说明

「注册（register）」这个词在 MLIR 里有两层含义，初学者最容易混淆。本节把它们彻底分清。

**(A) dialect 的注册——填一个 `DialectRegistry` 对象。**

`DialectRegistry` 是一个「dialect 名 → 如何创建该 dialect」的映射表。当你把 `arith` 注册进 `registry`，并不是立刻创建出 `arith` 的对象，而是登记了一条「以后遇到需要 `arith` 时，按这个方式把它造出来」的规则。这种「先登记、用到才加载」的机制叫**延迟加载（lazy loading）**，能避免启动时把几十个方言全部初始化。

为什么要注册 dialect？因为 `mlir-opt` 要解析输入文件，而解析时遇到 `arith.addi` 这种操作，必须能查到「`arith` 是什么、`addi` 合不合法」。不注册就等于不认识。

**(B) pass 的注册——填一个「全局」pass 注册表。**

注意 `registerAllPasses()` **不带任何参数**。它把 pass 注册进一个进程级的全局表。这个全局表存在的唯一理由是：**让命令行能通过名字找到 pass**。比如你在命令行写 `--cse`，工具就需要在某张表里查「`cse` 这个名字对应哪个 pass 的构造函数」。如果不注册，`--cse` 会被识别为未知参数。

> 关键区别一句话总结：**dialect 注册是为了「能解析输入」，pass 注册是为了「能用命令行按名字调用」。** 前者针对数据（IR 里的方言），后者针对工具的命令行调度能力。

#### 4.2.2 核心流程

两个注册函数的声明都很朴素。`registerAllDialects` 接收一个 `DialectRegistry &`，把所有 dialect 装进去；`registerAllPasses` 不接收参数，操作全局表。

```text
注册阶段
├─ registerAllPasses()                 // 全局 pass 表（无参）
│     └─ 命令行 "--xxx" 能解析为对应 pass
└─ DialectRegistry registry;
   registerAllDialects(registry)        // 填 registry（带参）
   registerAllExtensions(registry)      // 填方言扩展
   registerAllGPUToLLVMIRTranslations(registry)
        └─ 之后 MlirOptMain 解析输入时，能认出这些 dialect
```

#### 4.2.3 源码精读

先看两个注册函数的声明，理解它们的签名差异：

[include/mlir/InitAllPasses.h:17-28](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/InitAllPasses.h#L17-L28) —— `registerAllPasses` 无参数，注册到全局表，注释明确点出「全局注册表是为命令行工具服务的」：

```cpp
// This function may be called to register the MLIR passes with the
// global registry.
// If you're building a compiler, you likely don't need this: you would build a
// pipeline programmatically without the need to register with the global
// registry ...
// The global registry is interesting to interact with the command-line tools.
void registerAllPasses();
```

注意头注释里那句「如果你在写一个编译器，多半不需要这个」——因为真正写编译器时你会直接用 C++ 代码组装 `PassManager`，不必走「按名字查找」这条路。`mlir-opt` 之所以需要它，正是因为它是个命令行工具。

[include/mlir/InitAllDialects.h:17-27](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/InitAllDialects.h#L17-L27) —— `registerAllDialects` 有两个重载，一个填 `DialectRegistry`，一个直接填某个 `MLIRContext`：

```cpp
/// Add all the MLIR dialects to the provided registry.
void registerAllDialects(DialectRegistry &registry);

/// Append all the MLIR dialects to the registry contained in the given context.
void registerAllDialects(MLIRContext &context);
```

`mlir-opt` 的 `main` 用的是第一个重载（填 `registry`），因为此刻还没有 `MLIRContext`——`MLIRContext` 是在 `MlirOptMain` 内部、为每段输入创建的（见 4.3）。

再看 `main` 里实际调用它们的位置，并和「最小化」做法对比。完整的官方 `mlir-opt` 注册了一大堆东西（含测试 pass）：

[tools/mlir-opt/mlir-opt.cpp:328-335](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/tools/mlir-opt/mlir-opt.cpp#L328-L335) —— 填充 `registry` 的三步：dialect、extensions、GPU→LLVM IR 翻译：

```cpp
  DialectRegistry registry;
  registerAllDialects(registry);
  registerAllExtensions(registry);

  // TODO: Remove this and the corresponding ... dependency when a safe
  // dialect interface registration mechanism is implemented ...
  registerAllGPUToLLVMIRTranslations(registry);
```

而 out-of-tree 的 `standalone-opt` 范例展示了「只注册自己需要的几个 dialect」的写法，对比鲜明：

[examples/standalone/standalone-opt/standalone-opt.cpp:20-34](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/examples/standalone/standalone-opt/standalone-opt.cpp#L20-L34) —— 只注册 `Standalone`、`Arith`、`Func` 三个 dialect，注释提醒「只需注册会被**解析**的 dialect，不必注册只生成不解析的」：

```cpp
int main(int argc, char **argv) {
  mlir::registerAllPasses();
  mlir::standalone::registerPasses();

  mlir::DialectRegistry registry;
  registry.insert<mlir::standalone::StandaloneDialect,
                  mlir::arith::ArithDialect, mlir::func::FuncDialect>();
  // Add the following to include *all* MLIR Core dialects, or selectively
  // include what you need like above. You only need to register dialects that
  // will be *parsed* by the tool, not the one generated
  // registerAllDialects(registry);

  return mlir::asMainReturnCode(
      mlir::MlirOptMain(argc, argv, "Standalone optimizer driver\n", registry));
}
```

这段代码非常有教学意义：它用 `registry.insert<...>()` 只插了三个 dialect，并留了一句注释「你只需注册会被解析的 dialect，而不需要注册那些只生成、不解析的」。这揭示了 registry 的本质——**它是为「解析输入」服务的白名单**。

#### 4.2.4 代码实践

**实践目标**：对比「全量注册」与「按需注册」两种风格。

**操作步骤**：

1. 打开 [tools/mlir-opt/mlir-opt.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/tools/mlir-opt/mlir-opt.cpp) 的 `main`，记录它对 `registry` 调用了哪几个 `register...` 函数。
2. 打开 [examples/standalone/standalone-opt/standalone-opt.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/examples/standalone/standalone-opt/standalone-opt.cpp)，记录它用 `registry.insert<...>()` 注册了哪几个 dialect。
3. 思考：为什么 `standalone-opt` 选择只插三个，而不是也调用 `registerAllDialects`？（注释已给提示。）

**需要观察的现象**：两个 `main` 的「骨架」几乎一样（都是 `registerAllPasses` + 构造 `registry` + `MlirOptMain`），差别只在 `registry` 里塞了多少 dialect。

**预期结果**：你能用自己的话解释——`standalone-opt` 只处理自己那一种 IR，所以只注册必要的 dialect，二进制更小、启动更快；`mlir-opt` 是通用试验台，所以要全量注册。

#### 4.2.5 小练习与答案

**练习 1**：`registerAllPasses()` 和 `registerAllDialects(registry)` 一个有参数、一个没有，为什么？

> **参考答案**：pass 注册进的是进程级**全局表**（不需要你提供容器），所以无参；dialect 注册进的是你手里那个**具体的 `registry` 对象**（之后会传给 `MlirOptMain` 决定能解析什么），所以要把这个对象作为参数传入。

**练习 2**：注释里说「只需注册会被**解析**的 dialect，不必注册只生成不解析的」。请结合 `mlir-opt` 的工作方式解释这句话。

> **参考答案**：`mlir-opt` 第一步是「读入并解析输入文件」，只有出现在输入文件里、需要被解析的 dialect 才必须注册。而 pass 在改写 IR 时「新造出来」的 dialect 不需要预先注册到 registry——它们是 pass 在内存里直接构造的，不经过文本解析这一关。

---

### 4.3 MlirOptMain 的内部执行流程

#### 4.3.1 概念说明

`main` 把控制权交给 `MlirOptMain` 后，真正的工作才开始。`MlirOptMain` 是一个被各种 `*-opt` 工具共享的库函数，它封装了「类 mlir-opt 工具」的完整流程：解析命令行 → 读输入 → 建 context → 跑 pass → 打印输出。

理解 `MlirOptMain` 的最大价值在于：**它是「写一个自己的编译器驱动」的模板**。无论你将来做 in-tree 还是 out-of-tree 开发，只要你的工具需要「读 IR、改 IR、写 IR」，几乎都会复用这套逻辑。

`MlirOptMain.h` 里提供了 `MlirOptMain` 的几个重载，对应不同使用方式：

- 传 `argc/argv` + 工具名 + `registry`：最常见，命令行工具直接用（`mlir-opt` 走这条）。
- 传 `argc/argv` + 输入文件名 + 输出文件名 + `registry`：当你想先拿到命令行选项再做处理时用。
- 传一个已读好的 `MemoryBuffer` + `registry` + `config`：最底层，适合嵌入到别的程序里（比如测试框架直接喂内存内容）。

#### 4.3.2 核心流程

从「`main` 调用 `MlirOptMain(argc, argv, toolName, registry)`」开始，到 IR 被打印出来，经历的关键阶段如下（对应源码里的几个函数）：

```text
MlirOptMain(argc, argv, toolName, registry)        // 入口重载
  │
  ├─ registerAndParseCLIOptions(argc, argv, ...)    // 注册并解析命令行
  │     └─ 得到 inputFilename, outputFilename
  │
  └─ MlirOptMain(argc, argv, inputFilename, outputFilename, registry)
        │
        ├─ openInputFile(inputFilename)             // 打开输入文件
        ├─ openOutputFile(outputFilename)           // 准备输出
        │
        └─ MlirOptMain(outputStream, buffer, registry, config)   // 最底层重载
              │
              ├─ splitAndProcessBuffer(...)         // 支持 --split-input-file
              │     └─ 对每个分片调用 processBuffer(...)
              │
              └─ processBuffer(...)
                    ├─ 为本分片创建 MLIRContext(registry, ...)
                    │
                    └─ performActions(os, sourceMgr, context, config)
                          ├─ ① parseSourceFileForTool(...)   // 解析文本→Operation*
                          ├─ ② 构造 PassManager、装配流水线
                          ├─ ③ pm.run(*op)                   // 跑 pass 流水线
                          └─ ④ 把结果打印/写字节码到 os       // 序列化输出
```

最底层那个接受 `MemoryBuffer` 的重载才是「干活的核心」，上面两个重载都是「先准备参数、再调底层」的包装。

#### 4.3.3 源码精读

**入口重载**——`mlir-opt` 的 `main` 实际调用的就是这个签名：

[include/mlir/Tools/mlir-opt/MlirOptMain.h:409-413](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/Tools/mlir-opt/MlirOptMain.h#L409-L413) —— 最常用的 `MlirOptMain` 重载，参数为命令行参数、工具名、registry：

```cpp
/// Implementation for tools like `mlir-opt`.
/// - toolName is used for the header displayed by `--help`.
/// - registry should contain all the dialects that can be parsed in the source.
LogicalResult MlirOptMain(int argc, char **argv, llvm::StringRef toolName,
                          DialectRegistry &registry);
```

注释里的 `registry should contain all the dialects that can be parsed` 直接呼应 4.2 节：registry 决定了「能解析哪些方言」。

**入口重载的实现**——它只做「注册并解析命令行」，然后转给下一个重载：

[lib/Tools/mlir-opt/MlirOptMain.cpp:843-852](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/Tools/mlir-opt/MlirOptMain.cpp#L843-L852) —— 入口重载：注册并解析命令行，得到输入/输出文件名后转发：

```cpp
LogicalResult mlir::MlirOptMain(int argc, char **argv, llvm::StringRef toolName,
                                DialectRegistry &registry) {
  // Register and parse command line options.
  std::string inputFilename, outputFilename;
  std::tie(inputFilename, outputFilename) =
      registerAndParseCLIOptions(argc, argv, toolName, registry);
  return MlirOptMain(argc, argv, inputFilename, outputFilename, registry);
}
```

而 `registerAndParseCLIOptions` 做的事在另一处，它把各种 CLI 选项（printer、context、pass manager、timing 等）一并注册，再调用 LLVM 的 `cl::ParseCommandLineOptions`：

[lib/Tools/mlir-opt/MlirOptMain.cpp:697-714](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/Tools/mlir-opt/MlirOptMain.cpp#L697-L714) —— 注册各类命令行选项，并用 registry 里的方言名拼出 `--help` 的头部：

```cpp
std::string mlir::registerCLIOptions(llvm::StringRef toolName,
                                     DialectRegistry &registry) {
  MlirOptMainConfig::registerCLOptions(registry);
  registerAsmPrinterCLOptions();
  registerMLIRContextCLOptions();
  registerPassManagerCLOptions();
  registerDefaultTimingManagerCLOptions();
  tracing::DebugCounter::registerCLOptions();

  // Build the list of dialects as a header for the --help message.
  std::string helpHeader = (toolName + "\nAvailable Dialects: ").str();
  ...
  return helpHeader;
}
```

**核心三步**——真正处理 IR 的 `performActions`，清晰展现了「解析 → 跑 pass → 打印」三段式。下面摘取这三段的关键行：

[lib/Tools/mlir-opt/MlirOptMain.cpp:526-530](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/Tools/mlir-opt/MlirOptMain.cpp#L526-L530) —— ① 解析输入文件，把文本变成内存里的 `Operation*`：

```cpp
  TimingScope parserTiming = timing.nest("Parser");
  OwningOpRef<Operation *> op = parseSourceFileForTool(
      sourceMgr, parseConfig, !config.shouldUseExplicitModule());
  parserTiming.stop();
  if (!op)
    return failure();
```

[lib/Tools/mlir-opt/MlirOptMain.cpp:585-597](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/Tools/mlir-opt/MlirOptMain.cpp#L585-L597) —— ② 构造 `PassManager` 并装配流水线，③ 运行流水线（`pm.run(*op)`）：

```cpp
  PassManager pm(op.get()->getName(), PassManager::Nesting::Implicit);
  pm.enableVerifier(config.shouldVerifyPasses());
  if (failed(applyPassManagerCLOptions(pm)))
    return failure();
  pm.enableTiming(timing);
  ...
  if (failed(config.setupPassPipeline(pm)))
    return failure();

  // Run the pipeline.
  if (failed(pm.run(*op)))
    return failure();
```

[lib/Tools/mlir-opt/MlirOptMain.cpp:608-631](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/Tools/mlir-opt/MlirOptMain.cpp#L608-L631) —— ④ 序列化输出：若 `--emit-bytecode` 则写字节码，否则打印文本 IR：

```cpp
  TimingScope outputTiming = timing.nest("Output");
  if (config.shouldEmitBytecode()) {
    ...
    return writeBytecodeToFile(op.get(), os, writerConfig);
  }
  ...
  AsmState asmState(op.get(), ...);
  os << OpWithState(op.get(), asmState) << '\n';
  ...
  return success();
```

> 把这三段连起来看，你就理解了 `mlir-opt` 的本质：它就是 `performActions` 这三步的命令行外壳。`main` 负责「配好 registry 和 pass 全局表」，`MlirOptMain` 负责「读文件、建 context」，`performActions` 负责「解析 → 跑 → 打印」。

#### 4.3.4 代码实践

**实践目标**：通过源码阅读，确认「`mlir-opt` 处理 IR 的核心就三步」。

**操作步骤**：

1. 打开 [lib/Tools/mlir-opt/MlirOptMain.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/Tools/mlir-opt/MlirOptMain.cpp)，定位 `performActions` 函数（约第 500 行起）。
2. 在函数体里找到三处关键调用：`parseSourceFileForTool`（解析）、`pm.run`（跑 pass）、最后的 `os << ...`（打印）。
3. 注意第 661 行附近 `processBuffer` 里这句 `MLIRContext context(registry, MLIRContext::Threading::DISABLED);`——确认 registry 最终被传给了 `MLIRContext`。

**需要观察的现象**：`performActions` 的函数体虽然不短，但骨架就是「解析→跑→打印」，其余都是「选项处理、计时、remark 配置」等增强项。

**预期结果**：你能在不看笔记的情况下，画出「`MlirOptMain` → `processBuffer` → `performActions`（解析/跑/打印）」这条调用链。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `MLIRContext` 是在 `processBuffer` 里创建（每段输入一个），而不是在 `main` 里创建一个全局的？

> **参考答案**：因为 `mlir-opt` 支持 `--split-input-file`，把一个大文件切成多段独立处理；并且每段处理都希望有一个干净、独立的 context（隔离错误、便于并行、解析时还特意关闭多线程以减少同步开销，见源码第 510–511 行的 `disableMultithreading`）。所以 context 是「按输入分片」创建的，而不是全局共享的。

**练习 2**：`MlirOptMain` 在头文件里有三个重载，分别适合什么场景？

> **参考答案**：① `(argc, argv, toolName, registry)`——标准命令行工具直接用；② `(argc, argv, inputFilename, outputFilename, registry)`——想先自行解析命令行、拿到文件名后再进入主流程；③ `(outputStream, buffer, registry, config)`——最底层，输入已是内存 buffer，适合嵌入测试或其他程序。`mlir-opt` 的 `main` 走的是第①种。

---

### 4.4 命令行参数与运行方式（含 minimal-opt 对比）

#### 4.4.1 概念说明

光懂源码还不够，本节带你「真正用起来」`mlir-opt`。最常用的几个能力都对应到 4.3 节里的某个代码分支：

- **跑 pass**：用 `--pass-pipeline="..."` 描述要跑的 pass 序列（推荐），或用单个 pass 的裸 flag（如 `--cse`，不推荐）。
- **看帮助**：`--help` 列出所有 flag（接近 1000 个）；`--list-passes` 列出所有注册的 pass；`--show-dialects` 列出所有注册的 dialect。
- **改输出格式**：`--emit-bytecode` 输出字节码而非文本。
- **调试**：`--mlir-print-ir-after-all` 在每个 pass 之后打印 IR；`--mlir-timing` 显示各 pass 耗时；`--dump-pass-pipeline` 打印将要运行的流水线。

这些 flag 之所以「能用」，正是因为 4.2 节里 `registerCLIOptions` 把它们注册成了 LLVM 命令行选项（`cl::opt`）。换句话说，你在源码里能找到每一个 flag 的定义。

#### 4.4.2 核心流程

一次典型的 `mlir-opt` 调用：

```bash
mlir-opt [flags] [--pass-pipeline="..."] <input.mlir> [-o output.mlir]
```

不带任何 pass 时，`mlir-opt` 等价于「解析 → 校验 → 原样打印」，这是验证一段 IR 是否合法的最快方法。

`--pass-pipeline` 的文本语法（本讲只做认识，不讲细节）形如：

```text
builtin.module(pass1,pass2{option=value},pass3)
```

外层 `builtin.module(...)` 是「锚点」，表示这些 pass 作用在 `builtin.module` 这个操作上；括号内是按顺序执行的 pass 列表，`{key=value}` 是该 pass 的选项。

#### 4.4.3 源码精读

命令行的输入/输出文件名是通过两个「位置/选项」参数解析的：

[lib/Tools/mlir-opt/MlirOptMain.cpp:716-726](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/Tools/mlir-opt/MlirOptMain.cpp#L716-L726) —— `<input file>` 是位置参数（默认 `-` 即标准输入），`-o` 指定输出文件（默认也是标准输出）：

```cpp
std::pair<std::string, std::string>
mlir::parseCLIOptions(int argc, char **argv, llvm::StringRef helpHeader) {
  static cl::opt<std::string> inputFilename(
      cl::Positional, cl::desc("<input file>"), cl::init("-"));
  static cl::opt<std::string> outputFilename("o", cl::desc("Output filename"),
                                             cl::value_desc("filename"),
                                             cl::init("-"));
  cl::ParseCommandLineOptions(argc, argv, helpHeader);
  return std::make_pair(inputFilename.getValue(), outputFilename.getValue());
}
```

这就解释了为什么 `mlir-opt foo.mlir` 能读文件、`mlir-opt < foo.mlir` 也能读、`mlir-opt foo.mlir -o out.mlir` 能写文件——都是这两行 `cl::opt` 在起作用。

而 `--show-dialects`、`--list-passes` 这些「列完即退出」的 flag，定义在配置类里，并在最底层 `MlirOptMain` 开头被检查：

[lib/Tools/mlir-opt/MlirOptMain.cpp:748-757](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/Tools/mlir-opt/MlirOptMain.cpp#L748-L757) —— 若带了 `--show-dialects` 或 `--list-passes`，打印后立即返回，不再处理输入：

```cpp
LogicalResult mlir::MlirOptMain(llvm::raw_ostream &outputStream,
                                std::unique_ptr<llvm::MemoryBuffer> buffer,
                                DialectRegistry &registry,
                                const MlirOptMainConfig &config) {
  if (config.shouldShowDialects())
    return printRegisteredDialects(registry);
  if (config.shouldListPasses())
    return printRegisteredPassesAndReturn();
  ...
```

**和最小化版本对比**——这是本讲的实践任务核心。看一眼 `mlir-minimal-opt` 的 `main`，它只有 4 行有效代码：

[examples/minimal-opt/mlir-minimal-opt.cpp:11-18](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/examples/minimal-opt/mlir-minimal-opt.cpp#L11-L18) —— 最小 opt 入口：连一个 dialect/pass 都不注册，直接进入 `MlirOptMain`：

```cpp
/// This test includes the minimal amount of components for mlir-opt, that is
/// the CoreIR, the printer/parser, the bytecode reader/writer, the
/// passmanagement infrastructure and all the instrumentation associated with it.
int main(int argc, char **argv) {
  mlir::DialectRegistry registry;
  return mlir::asMainReturnCode(mlir::MlirOptMain(
      argc, argv, "Minimal Standalone optimizer driver\n", registry));
}
```

它**什么都没注册**——`registry` 是空的，也没调用 `registerAllPasses()`。注释里写明它只包含「CoreIR + printer/parser + bytecode + pass 管理 + 插桩」这套最小骨架。它的用途是衡量 MLIR 框架本身的最小二进制体积（见 `README.md` 里的体积对比表）。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：对比 `mlir-opt` 与 `mlir-minimal-opt`，说清楚「最小化版本精简了哪些注册步骤」，并构造一个最简 opt 入口思路。

**操作步骤**：

1. 读 [tools/mlir-opt/mlir-opt.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/tools/mlir-opt/mlir-opt.cpp) 的 `main`，列出它做的全部「注册」类调用。
2. 读 [examples/minimal-opt/mlir-minimal-opt.cpp](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/examples/minimal-opt/mlir-minimal-opt.cpp)，确认它除了构造空 `registry` 和调 `MlirOptMain` 外**什么注册都没做**。
3. 完成下面的「精简对照表」。

**精简对照表（请在阅读后自行补全）**：

| 注册动作 | `mlir-opt` 是否做 | `mlir-minimal-opt` 是否做 |
| --- | --- | --- |
| `registerAllPasses()` | 是 | 否 |
| `registerTestPasses()`（测试构建下） | 是 | 否 |
| `registerAllDialects(registry)` | 是 | 否 |
| `registerAllExtensions(registry)` | 是 | 否 |
| `registerAllGPUToLLVMIRTranslations(registry)` | 是 | 否 |
| 构造 `DialectRegistry` 并调用 `MlirOptMain` | 是 | 是 |

4. **构造一个「最简 opt 入口」思路**（伪代码）。假设你想写一个只认识 `func` + `arith` 两个方言、并且只想让命令行能用 `--canonicalize` 这个 pass 的小工具，参考 `standalone-opt` 的写法，伪代码如下（**示例代码，非项目原有文件**）：

```cpp
// 示例代码：一个只支持 func+arith、只暴露 canonicalize 的最简 opt
#include "mlir/Dialect/Arith/IR/Arith.h"
#include "mlir/Dialect/Func/IR/FuncOps.h"
#include "mlir/Transforms/Transforms.h"            // registerCanonicalizer
#include "mlir/Tools/mlir-opt/MlirOptMain.h"

int main(int argc, char **argv) {
  // 1) 只注册我需要的 pass（让 --canonicalize 可用）
  mlir::registerCanonicalizer();

  // 2) 只注册会被解析的 dialect
  mlir::DialectRegistry registry;
  registry.insert<mlir::arith::ArithDialect, mlir::func::FuncDialect>();

  // 3) 进入共享主流程
  return mlir::asMainReturnCode(
      mlir::MlirOptMain(argc, argv, "My tiny optimizer\n", registry));
}
```

> 注意：上面是说明「思路」的示例代码，`registerCanonicalizer` 的确切头文件以你本地源码为准；若想严格验证，请用 `Grep` 在 `include/mlir/Transforms/` 下确认其声明后再使用。本步骤标注为「待本地验证」。

**需要观察的现象**：无论注册多少东西，最后一句「`asMainReturnCode(MlirOptMain(...))`」的形状完全一致——这就是「类 mlir-opt 工具」的统一收尾。

**预期结果**：你能用一句话回答实践任务——「`mlir-minimal-opt` 精简掉了 `registerAllPasses`、`registerAllDialects`、`registerAllExtensions` 以及所有 GPU→LLVM IR 翻译和测试 pass 的注册，只保留了『空 registry + MlirOptMain』的最小骨架」。

**关于真实运行**（待本地验证）：如果你已按 [docs/Tutorials/MlirOpt.md](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Tutorials/MlirOpt.md) 构建了 MLIR，可以这样验证一段 IR 是否合法（不跑任何 pass，仅做「解析→校验→打印」的往返）：

```bash
build/bin/mlir-opt path/to/your.mlir
```

若输出与输入等价的 IR 且无报错，说明 IR 合法。带 pass 的例子可参考该教程文档里的 `--pass-pipeline="builtin.module(convert-math-to-llvm)"` 示例。

#### 4.4.5 小练习与答案

**练习 1**：`mlir-minimal-opt` 因为没有注册任何 dialect，用它去解析一段含 `func.func` 的 `.mlir` 会怎样？如何让它「不报错地吞下」未知方言？

> **参考答案**：默认会报「使用了未注册的方言」错误。可以加 `--allow-unregistered-dialect` 让它跳过方言校验（该 flag 的定义见 `MlirOptMain.cpp` 第 79–82 行的 `allowUnregisteredDialects` 选项）。官方也注明这个选项「仅为测试方便，不推荐常规使用」。

**练习 2**：`--show-dialects` 列出的方言列表，是哪个对象决定的？

> **参考答案**：是你传给 `MlirOptMain` 的那个 `registry` 决定的。`mlir-opt` 因为调了 `registerAllDialects(registry)`，所以会列出所有自带方言；`mlir-minimal-opt` 传的是空 registry，因此基本列不出什么。

**练习 3**：为什么官方教程推荐用 `--pass-pipeline` 而不是裸 flag（如 `--cse`）来跑 pass？

> **参考答案**：`--pass-pipeline` 用文本明确描述了「锚点 + pass 序列 + 各 pass 选项」，能表达嵌套和带选项的复杂流水线，且语义无歧义；裸 flag 无法表达嵌套锚点和选项组合，容易出错。此外，嵌套锚点还能让 pass 在 IR 子集上并行执行，性能更好。

---

## 5. 综合实践

把本讲串起来，完成下面这个「追踪一次 `mlir-opt` 调用」的小任务。

**任务**：假设用户执行了下面这条命令（待本地验证）：

```bash
mlir-opt --pass-pipeline="builtin.module(canonicalize)" foo.mlir -o out.mlir
```

请你**只阅读源码**，按时间顺序写出这条命令在 MLIR 内部经过了哪些关键函数、各自做了什么。要求至少覆盖以下检查点：

1. `main` 里哪几行先执行？它们做了什么注册？（对应 [mlir-opt.cpp:323-347](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/tools/mlir-opt/mlir-opt.cpp#L323-L347)）
2. `--pass-pipeline` 这个字符串是在哪里被解析成 pass 流水线的？（提示：`MlirOptMainConfig::setPassPipelineParser` / `config.setupPassPipeline(pm)`，见 [MlirOptMain.cpp:358-375](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/Tools/mlir-opt/MlirOptMain.cpp#L358-L375) 与 [MlirOptMain.cpp:592](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/Tools/mlir-opt/MlirOptMain.cpp#L592)）
3. `foo.mlir` 是在哪一行被读入、又在哪一行被解析成 `Operation*` 的？（对应 [MlirOptMain.cpp:824](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/Tools/mlir-opt/MlirOptMain.cpp#L824) 的 `openInputFile` 与 [MlirOptMain.cpp:526](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/Tools/mlir-opt/MlirOptMain.cpp#L526) 的 `parseSourceFileForTool`）
4. `canonicalize` 这个 pass 在哪一行被真正执行？（对应 [MlirOptMain.cpp:596](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/Tools/mlir-opt/MlirOptMain.cpp#L596) 的 `pm.run(*op)`）
5. `out.mlir` 的内容是在哪一行被写出的？（对应 [MlirOptMain.cpp:608-631](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/lib/Tools/mlir-opt/MlirOptMain.cpp#L608-L631) 的输出序列化）

**产出**：一张「函数调用时间线」表，左列是函数名（含文件:行号），右列是它在本条命令里承担的职责。完成后，你应该能闭着眼睛复述 `mlir-opt` 从命令行到输出文件的完整链路。

## 6. 本讲小结

- `mlir-opt` 是一个「读 IR → 可选跑 pass → 写 IR」的命令行试验台，本身不是完整编译器；它的 `main` 极薄，只负责「注册 + 调用 `MlirOptMain`」。
- `main` 的执行顺序固定为：`registerAllPasses()` → 构造 `DialectRegistry` → `registerAllDialects/Extensions/...` → `asMainReturnCode(MlirOptMain(...))`。
- **dialect 注册**填的是「具体的 registry 对象」（决定能解析哪些方言）；**pass 注册**填的是「全局表」（决定命令行能按名字调用哪些 pass）——两者目标不同。
- `MlirOptMain` 是「类 mlir-opt 工具」的共享入口，其核心 `performActions` 是「解析（`parseSourceFileForTool`）→ 跑流水线（`pm.run`）→ 打印」三段式。
- 常用 CLI 都对应到源码里某个 `cl::opt`：输入/输出文件名、`--show-dialects`、`--list-passes`、`--emit-bytecode`、`--pass-pipeline` 等均可在 `MlirOptMain.cpp` 中找到定义。
- 最小化版本 `mlir-minimal-opt` 省掉了所有注册，只保留「空 registry + `MlirOptMain`」骨架；`standalone-opt` 则展示了「按需注册少数 dialect」的 out-of-tree 范式。

## 7. 下一步学习建议

- **本讲只看了「入口怎么跑起来」**，但还没真正读懂输入文件里的文本语法。下一讲 [u1-l4 文本 IR 语法速览](./u1-l4-text-ir-syntax.md) 会带你读懂一段标准 `.mlir` 文本，建议接着学。
- 想深入了解「dialect 到底是什么」的精确定义，可提前浏览 [include/mlir/IR/Dialect.h](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/include/mlir/IR/Dialect.h)（对应第 3 单元 u3-l1）。
- 想了解 `PassManager` 的嵌套与锚点机制，可读 [docs/PassManagement.md](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/PassManagement.md)（对应第 5 单元 u5-l1）。
- 如果你想立刻「跑一个 pass 看效果」，官方教程 [docs/Tutorials/MlirOpt.md](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Tutorials/MlirOpt.md) 给了 `convert-math-to-llvm`、`affine-loop-fusion` 等完整可复现示例，是本讲命令行部分的最佳补充。
