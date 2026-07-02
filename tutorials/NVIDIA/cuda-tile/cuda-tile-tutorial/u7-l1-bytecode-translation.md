# 字节码翻译管线与工具入口

## 1. 本讲目标

u1-l3 已经带读者从命令行层面跑通了「文本 MLIR → 字节码 `.tilebc` → cubin」的端到端流程，知道了 `cuda-tile-translate` 的存在和 `--bytecode-version` 等参数。本讲**下沉到源码**，回答三个工程问题：

1. `cuda-tile-translate` 这个可执行程序的入口到底做了什么？为什么它的 `main` 只有几行？
2. `mlir-to-cudatilebc` 与 `cudatilebc-to-mlir` 这两个「翻译（Translation）」在 C++ 层面是如何被注册进工具的？二者互逆的对称结构长什么样？
3. `--bytecode-version`、`--list-versions`、`--Wunsupported-hints`、`--Werr-hints` 这些命令行选项在哪一段代码里被注册，又如何被翻译流程消费？

学完本讲，你应该能够：

- 看懂 `BytecodeTranslation.cpp` 中两个 `Registration` 的对称结构，并能说清 `TranslateFromMLIRRegistration` 与 `TranslateToMLIRRegistration` 各自「从哪来、到哪去」。
- 解释 `cuda-tile-translate` 的 `main` 为何必须「先注册、后进入主循环」，以及注册顺序的硬约束从何而来。
- 在不查文档的情况下，从 `CommandLineOptions.cpp` 推断 `--bytecode-version` 的取值范围、默认值与非法值报错格式。

本讲是整个「专家·字节码」单元（u7）的**入口**：u7-l2（写入器）、u7-l3（读取器）、u7-l4（版本兼容）都建立在「翻译管线先把 ModuleOp 交给写入器、再把字节码交给读取器」这个调度骨架之上。

## 2. 前置知识

本讲假设你已经具备 u1-l3 建立的认知，以下术语不再重复解释，只做一句话定位：

- **三种程序形态**：文本 `.mlir`、字节码 `.tilebc`、机器码 `.cubin`。本讲只关注前两者之间的转换。
- **MLIR 的 `mlir-translate` 框架**：上游 LLVM/MLIR 提供的一个通用「翻译」工具骨架。一个「翻译」就是一对函数：把某种输入转换成某种输出。`cuda-tile-translate` 本质就是这个骨架的 CUDA Tile 定制版。
- **Translation（翻译）**：MLIR 框架里对「一类输入→输出转换」的注册单位，靠一个字符串名字（如 `mlir-to-cudatilebc`）在命令行选中。
- **`cuda_tile.module` / `entry`**：CUDA Tile IR 的顶层容器操作与函数入口。
- **优化提示（Optimization Hints）**：u6-l1 讲过的、附加在操作上的调优属性，以及 `-Wunsupported-hints`/`-Werr-hints` 两个开关。本讲会看到这两个开关如何从命令行一路传到方言对象。

两个本讲新引入、但只需要建立直觉的概念：

- **Translation 的「视角」命名**：MLIR 里 `TranslateFromMLIRRegistration` 表示「**输入**是 MLIR 文本」，`TranslateToMLIRRegistration` 表示「**输出**是 MLIR 文本」。这里的 from/to 都是相对于 MLIR 文本说的——记住「MLIR 文本」这一端，方向就不会搞反。
- **命令行选项的「外部存储（external storage）」**：LLVM 的 `cl::opt` 默认把选项值存在自己内部；用 `cl::location(某全局变量)` 可以让它把值写进一个外部全局变量，从而让其它翻译单元（如 `BytecodeTranslation.cpp`）通过 getter 函数读取。本讲会大量看到这个模式。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 角色 |
| --- | --- |
| [tools/cuda-tile-translate/cuda-tile-translate.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-translate/cuda-tile-translate.cpp) | 工具可执行入口，`main` 所在地。只负责「注册一切，然后进入 MLIR 主循环」。 |
| [lib/Bytecode/Translation/BytecodeTranslation.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Translation/BytecodeTranslation.cpp) | 注册 `mlir-to-cudatilebc` 与 `cudatilebc-to-mlir` 两个翻译，是本讲主角。 |
| [include/cuda_tile/Bytecode/Translation/BytecodeTranslation.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Bytecode/Translation/BytecodeTranslation.h) | 只暴露一个函数 `registerTileIRTranslations()`。 |
| [include/cuda_tile/Bytecode/Common/CommandLineOptions.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Bytecode/Common/CommandLineOptions.h) | 声明本讲涉及的命令行选项的注册函数与取值 getter。 |
| [lib/Bytecode/Common/CommandLineOptions.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/CommandLineOptions.cpp) | 上述选项的实现：自定义解析器、外部存储变量、`--list-versions` 回调。 |
| [tools/cuda-tile-translate/test/RoundTripTestRegistration.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-translate/test/RoundTripTestRegistration.cpp) | 注册「进程内」往返翻译 `test-cudatile-roundtrip`，供测试使用。 |

> 说明：写入器 `writeBytecode` 与读取器 `readBytecode` 的内部实现分别在 u7-l2、u7-l3 精读；本讲只把它们当作两个被翻译管线调用的「黑盒函数」。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1** `cuda-tile-translate` 工具入口——`main` 的极简流程与「注册必须先于解析」的硬约束。
2. **4.2** `registerTileIRTranslations`——两个对称翻译的注册结构，以及优化提示如何被注入方言。
3. **4.3** 命令行选项注册——`--bytecode-version` / `--list-versions` / `--W*-hints` 的注册与取值机制。

最后用 4.4 把测试用的 `test-cudatile-roundtrip` 与跨进程往返脚本串起来，作为「翻译注册」这一概念的延伸。

### 4.1 cuda-tile-translate 工具入口：main 的极简流程

#### 4.1.1 概念说明

`cuda-tile-translate` 不是从零写的命令行程序，而是 MLIR 上游 `mlir-translate` 工具的一个**薄壳**。MLIR 的 `mlir-translate` 提供了一个标准入口 `mlirTranslateMain`：它负责解析命令行、根据用户选中的翻译名字（如 `-mlir-to-cudatilebc`）查表、读输入文件、调用对应翻译、把结果写到输出。

这意味着 CUDA Tile 侧只需要做两件事：

1. **把自己提供的翻译和命令行选项注册进全局表**；
2. **把控制权交给 `mlirTranslateMain`**。

注册必须是「静态构造期 + main 开头显式调用」的组合：MLIR 的翻译表用 `Translation::GlobalCommand()` 持有，命令行选项则用 LLVM 的 `cl::opt` 静态对象登记。**所有这些注册都必须发生在 `mlirTranslateMain` 开始解析命令行之前**——否则用户传入的 `-mlir-to-cudatilebc`、`--bytecode-version=13.1` 会因为「选项未注册」而被拒绝。这就是 u1-l3 提到的「所有注册必须在解析命令行前完成」在源码层的具体落实。

#### 4.1.2 核心流程

`main` 的执行可以画成下面这条线：

```
main(argc, argv)
  │
  ├─ registerTileIRBytecodeVersionOption()      // 注册 --bytecode-version
  ├─ registerTileIROptimizationHintsOptions()   // 注册 -Wunsupported-hints / -Werr-hints
  ├─ registerListVersionsOption()               // 注册 --list-versions
  ├─ registerTileIRTranslations()               // 注册两个翻译（mlir-to-/cudatilebc-to-）
  ├─ registerTileIRTestTranslations()           // 注册 test-cudatile-roundtrip（仅测试构建）
  │
  └─ mlirTranslateMain(argc, argv, "CUDA Tile Translation Tool")
        │
        ├─ 解析命令行（此时上面注册的选项/翻译都已就位）
        ├─ 根据 -<translation-name> 选中翻译
        ├─ 读输入文件 / 写输出文件
        └─ 返回状态码
```

注意 `registerTileIRTestTranslations()` 注册的 `test-cudatile-roundtrip` 只在开启测试（`TILE_IR_INCLUDE_TESTS`）的构建里存在，所以生产环境的 `cuda-tile-translate` 并不认这个翻译名。

#### 4.1.3 源码精读

工具入口确实只有这些：

[tools/cuda-tile-translate/cuda-tile-translate.cpp:18-28](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-translate/cuda-tile-translate.cpp#L18-L28) —— `main` 先按「选项 → 翻译 → 测试翻译」的顺序注册，再交棒给 `mlirTranslateMain`：

```cpp
int main(int argc, char **argv) {
  // Register command line options before parsing.
  mlir::cuda_tile::registerTileIRBytecodeVersionOption();
  mlir::cuda_tile::registerTileIROptimizationHintsOptions();
  mlir::cuda_tile::registerListVersionsOption();
  mlir::cuda_tile::registerTileIRTranslations();
  mlir::cuda_tile::registerTileIRTestTranslations();

  return mlir::failed(
      mlir::mlirTranslateMain(argc, argv, "CUDA Tile Translation Tool"));
}
```

第 19 行的注释 `Register command line options before parsing` 就是前面那条硬约束的来源——这一行注释解释了为什么这五个 `register*` 调用必须放在 `mlirTranslateMain` 之前。`main` 把 `mlirTranslateMain` 的返回值用 `mlir::failed` 包一层：MLIR 用 `LogicalResult` 表示成败，`failed(...)` 在失败时返回 `true`，于是程序以非零退出码结束，这正是命令行工具向 shell 报错的标准方式。

> 顺带一提：`main` 里没有 `#include` 任何「写入器/读取器」的头文件——它只 include 了「注册函数」的头文件。真正的字节码读写代码是**通过翻译闭包间接调用**的，这在 4.2 会看清。

#### 4.1.4 代码实践

**实践目标**：确认「注册顺序」是工具能否识别选项的前提。

**操作步骤**（源码阅读型，无需运行）：

1. 打开 [tools/cuda-tile-translate/cuda-tile-translate.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-translate/cuda-tile-translate.cpp)，确认五个 `register*` 调用都在 `mlirTranslateMain` 之前。
2. 在脑中删除 `registerTileIRBytecodeVersionOption()` 这一行（即模拟「忘记注册 `--bytecode-version`」），回答：此时运行 `cuda-tile-translate --bytecode-version=13.1 ...` 会出现什么？
3. 对比验证：观察 `registerListVersionsOption()` 与 `--list-versions` 的对应关系，理解「每一个用户可见的选项，都对应一处 `register*`」。

**需要观察的现象**：删掉任一 `register*` 后，对应的命令行选项会被 `mlirTranslateMain` 当成「未知选项」拒绝。

**预期结果**：第 2 步会得到类似 `Unknown command line argument '--bytecode-version'` 的报错（具体措辞来自 LLVM 的 `cl` 库，待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `registerTileIRTestTranslations()` 不放进生产构建？

**参考答案**：它注册的 `test-cudatile-roundtrip`（见 4.4）依赖测试专用操作（`testing$` 前缀，受 `TILE_IR_INCLUDE_TESTS` 宏保护，参见 u2-l2），是面向项目自身回归测试的「探针」，不应出现在下游集成的工具里。

**练习 2**：`main` 里为什么没有显式调用 `registerCudaTileDialect()` 之类的方言注册？

**参考答案**：方言注册由每个翻译的注册闭包负责——`registerFromTileIRBytecodeTranslation`/`registerToTileIRBytecodeTranslation` 各自带了一个 `[](DialectRegistry &registry){ registry.insert<CudaTileDialect>(); }` 回调（见 4.2），`mlirTranslateMain` 在构造 `MLIRContext` 时会调用它，因此方言按需被加载，无需在 `main` 里集中注册。

### 4.2 registerTileIRTranslations：两个翻译的对称注册

#### 4.2.1 概念说明

本模块是整篇讲义的核心。CUDA Tile 把「MLIR 文本 ↔ 字节码」这对互逆转换实现为**两个独立的 Translation**，再用一个聚合函数 `registerTileIRTranslations()` 一并注册：

| 翻译名 | 方向 | 输入 | 输出 | 注册类 |
| --- | --- | --- | --- | --- |
| `mlir-to-cudatilebc` | 序列化 | 解析后的 `Operation *`（MLIR 文本） | 写入 `raw_ostream` 的字节码 | `TranslateFromMLIRRegistration` |
| `cudatilebc-to-mlir` | 反序列化 | 字节码字符串 `StringRef` | 反序列化出的 `Operation *`（MLIR 文本） | `TranslateToMLIRRegistration` |

记住 2 节那条「视角命名」规则：`From` = 输入端是 MLIR 文本，`To` = 输出端是 MLIR 文本。于是：

- `mlir-to-cudatilebc`（输入 MLIR 文本）→ 用 `From`；
- `cudatilebc-to-mlir`（输出 MLIR 文本）→ 用 `To`。

两个翻译都做了一件相同的关键事：**把命令行里读到的优化提示开关，写进 `CudaTileDialect` 对象**。这样无论走哪条翻译路径，写入器/读取器在序列化或校验时都能拿到一致的 `-Wunsupported-hints` / `-Werr-hints` 行为（详见 u6-l1）。

#### 4.2.2 核心流程

序列化方向（`mlir-to-cudatilebc`）：

```
mlirTranslateMain 解析 .mlir → 得到一个顶层 Operation*
  │
  ├─ getCudaTileModuleOp(op)              // 找到 cuda_tile.module
  │     ├─ 直接是 cuda_tile::ModuleOp → 用之
  │     └─ 是 mlir::ModuleOp 且内含单个 cuda_tile.module → 取出内层
  │       （否则报错 "expected a single CUDA Tile IR module"）
  │
  ├─ 取目标版本 targetVersion = getCurrentBytecodeVersion()  // 默认 13.3
  ├─ dialect->setWarnUnsupportedHints(...)  // 注入 -Wunsupported-hints
  ├─ dialect->setErrorOnHints(...)          // 注入 -Werr-hints
  └─ writeBytecode(output, moduleOp, targetVersion)   // 交给 u7-l2 的写入器
```

反序列化方向（`cudatilebc-to-mlir`）：

```
mlirTranslateMain 读 .tilebc → 得到字节码字节串
  │
  ├─ context->getOrLoadDialect<CudaTileDialect>()
  ├─ dialect->setWarnUnsupportedHints(...)  // 注入 -Wunsupported-hints
  ├─ dialect->setErrorOnHints(...)          // 注入 -Werr-hints
  └─ readBytecode(bytecodeBufferRef, *context)  // 交给 u7-l3 的读取器
        └─ 返回 OwningOpRef<Operation *>     // mlirTranslateMain 负责打印成 MLIR 文本
```

注意 `getCudaTileModuleOp` 处理了一个常见陷阱：当**不带** `-no-implicit-module` 时，MLIR 解析器会自动给顶层套一个 `mlir::ModuleOp`，于是真正的 `cuda_tile.module` 成了它的唯一子操作。这段代码就是为了把内层 `cuda_tile.module` 「剥」出来，让两种调用方式都能工作。README 的官方命令（见 4.3.3 与综合实践）显式带了 `--no-implicit-module`，于是顶层直接就是 `cuda_tile.module`，走第一个分支。

#### 4.2.3 源码精读

先看反序列化翻译与其依赖的 `deserializeModule`：

[lib/Bytecode/Translation/BytecodeTranslation.cpp:25-33](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Translation/BytecodeTranslation.cpp#L25-L33) —— `deserializeModule` 先把字节码字符串包成 `MemoryBufferRef`，再加载方言、注入提示开关、调用读取器：

```cpp
static OwningOpRef<Operation *> deserializeModule(llvm::StringRef bytecodeStr,
                                                  MLIRContext *context) {
  llvm::MemoryBufferRef bytecodeBufferRef(bytecodeStr, "deserializeModuleBuffer");
  auto dialect = context->getOrLoadDialect<CudaTileDialect>();
  dialect->setWarnUnsupportedHints(getWarnUnsupportedHints());
  dialect->setErrorOnHints(getErrorUnsupportedHints());
  return readBytecode(bytecodeBufferRef, *context);
}
```

[lib/Bytecode/Translation/BytecodeTranslation.cpp:35-42](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Translation/BytecodeTranslation.cpp#L35-L42) —— `cudatilebc-to-mlir` 用 `TranslateToMLIRRegistration` 注册（输出端是 MLIR 文本）。第二个 lambda 是「方言注册回调」，确保 `MLIRContext` 里加载了 `CudaTileDialect`：

```cpp
static void registerFromTileIRBytecodeTranslation() {
  TranslateToMLIRRegistration fromBytecode(
      "cudatilebc-to-mlir", "Translate CUDA Tile IR bytecode to MLIR",
      [](llvm::StringRef bytecode, MLIRContext *context) {
        return deserializeModule(bytecode, context);
      },
      [](DialectRegistry &registry) { registry.insert<CudaTileDialect>(); });
}
```

再看序列化方向，先看 `getCudaTileModuleOp` 如何兼容「带/不带隐式 module」两种输入：

[lib/Bytecode/Translation/BytecodeTranslation.cpp:47-63](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Translation/BytecodeTranslation.cpp#L47-L63) —— 先尝试直接 cast 成 `cuda_tile::ModuleOp`；失败则检查是否是被 `mlir::ModuleOp` 包了一层（且必须只有一个 `cuda_tile.module` 子操作）：

```cpp
static FailureOr<cuda_tile::ModuleOp> getCudaTileModuleOp(Operation *op) {
  cuda_tile::ModuleOp moduleOp = dyn_cast<cuda_tile::ModuleOp>(op);
  if (moduleOp)
    return moduleOp;
  // Also support a CUDA Tile IR Module nested in a MLIR Module ...
  if (auto moduleOp = dyn_cast<mlir::ModuleOp>(op)) {
    if (!llvm::hasSingleElement(*moduleOp.getBody()) ||
        !llvm::isa<cuda_tile::ModuleOp>(moduleOp.getBody()->front())) {
      return op->emitError("expected a single CUDA Tile IR module in the MLIR module");
    }
    return cast<cuda_tile::ModuleOp>(moduleOp.getBody()->front());
  }
  return op->emitError("expected a CUDA Tile IR module, but got a " +
                       op->getName().getStringRef());
}
```

[lib/Bytecode/Translation/BytecodeTranslation.cpp:65-80](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Translation/BytecodeTranslation.cpp#L65-L80) —— `mlir-to-cudatilebc` 用 `TranslateFromMLIRRegistration` 注册（输入端是 MLIR 文本）。闭包里依次：取目标版本、定位 module、注入提示开关、调用写入器：

```cpp
static void registerToTileIRBytecodeTranslation() {
  TranslateFromMLIRRegistration toBytecode(
      "mlir-to-cudatilebc", "Translate MLIR to CUDA Tile IR bytecode",
      [](Operation *op, raw_ostream &output) {
        BytecodeVersion targetVersion = getCurrentBytecodeVersion();
        auto moduleOp = getCudaTileModuleOp(op);
        if (failed(moduleOp))
          return failure();
        auto dialect =
            cast<CudaTileDialect>(moduleOp->getOperation()->getDialect());
        dialect->setWarnUnsupportedHints(getWarnUnsupportedHints());
        dialect->setErrorOnHints(getErrorUnsupportedHints());
        return writeBytecode(output, *moduleOp, targetVersion);
      },
      [](DialectRegistry &registry) { registry.insert<CudaTileDialect>(); });
}
```

最后是聚合入口，它就是头文件里唯一导出的符号：

[lib/Bytecode/Translation/BytecodeTranslation.cpp:82-85](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Translation/BytecodeTranslation.cpp#L82-L85) —— `registerTileIRTranslations` 把两个翻译一并注册，这也是 `cuda-tile-translate` 的 `main` 调用的函数：

```cpp
void mlir::cuda_tile::registerTileIRTranslations() {
  registerFromTileIRBytecodeTranslation();
  registerToTileIRBytecodeTranslation();
}
```

对应头文件极简，只声明这一个函数：

[include/cuda_tile/Bytecode/Translation/BytecodeTranslation.h:13-17](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Bytecode/Translation/BytecodeTranslation.h#L13-L17) —— 对外只暴露 `registerTileIRTranslations`，把两个 `static` 注册函数藏在 `.cpp` 的匿名命名空间之外、文件作用域里，实现「最小暴露」：

```cpp
namespace mlir::cuda_tile {
void registerTileIRTranslations();
} // namespace mlir::cuda_tile
```

#### 4.2.4 代码实践

**实践目标**：观察 `getCudaTileModuleOp` 对「带隐式 module」与「不带隐式 module」两种输入的兼容。

**操作步骤**（源码阅读型）：

1. 准备一段最小 MLIR（示例代码，非项目原有）：

   ```
   cuda_tile.module @kernels {
     cuda_tile.entry @f() {
       %c = cuda_tile.constant <i32 : 0> : !cuda_tile.tile<i32>
       cuda_tile.return %c : !cuda_tile.tile<i32>
     }
   }
   ```
2. 阅读上面引用的 `getCudaTileModuleOp`（[L47-L63](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Translation/BytecodeTranslation.cpp#L47-L63)）。
3. 在脑中分别跟踪两种调用：
   - 带 `--no-implicit-module`：顶层 `op` 就是 `cuda_tile.module`，第 1 个 `if` 命中，直接返回。
   - 不带 `--no-implicit-module`：MLIR 解析器套了 `mlir::ModuleOp`，走第 2 个 `if`，取出内层唯一 `cuda_tile.module`。
4. 进一步假设输入是 `builtin.module { cuda_tile.module @a {} cuda_tile.module @b {} }`（两个 cuda_tile module），推断会命中哪条 `emitError`。

**需要观察的现象**：第 4 步会因 `hasSingleElement` 为假而报错 `expected a single CUDA Tile IR module in the MLIR module`。

**预期结果**：待本地验证——构造一个含两个 `cuda_tile.module` 的 `.mlir`（不带 `--no-implicit-module`）跑 `cuda-tile-translate -mlir-to-cudatilebc`，应得到上述报错。

#### 4.2.5 小练习与答案

**练习 1**：`writeBytecode` 的第三个参数 `targetVersion` 从哪里来？默认值是什么？

**参考答案**：来自 `getCurrentBytecodeVersion()`（定义在 `CommandLineOptions.cpp`，见 4.3），它读取 `--bytecode-version` 选项的值；该选项用 `cl::init(BytecodeVersion::kCurrentVersion)` 初始化，而 `kCurrentVersion` 在 [Version.cpp:46-50](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/Version.cpp#L46-L50) 定义为 `{13, 3, 0}`，即默认写出 13.3 字节码。

**练习 2**：两个翻译都调用了 `setWarnUnsupportedHints` / `setErrorOnHints`，为什么反序列化方向（`cudatilebc-to-mlir`）也需要它？

**参考答案**：反序列化重建出的 IR 在被打印/校验时，`OptimizationHintsAttr` 的逐操作校验（u6-l1 的 `verifyWithOp`）仍会根据这两个标志决定「静默 / 告警 / 报错」。因此即便只是把字节码读回来，也必须把命令行开关注入方言，否则读取出的旧字节码里那些「当前版本不再支持」的提示无法按用户意图处理。

### 4.3 命令行选项注册：--bytecode-version / --list-versions / --W*-hints

#### 4.3.1 概念说明

本模块回答「`main` 里那三个 `register*Option` 到底注册了什么、值存在哪、翻译流程怎么读到」。三个选项共享同一种实现套路：

- 用 LLVM `cl::opt` 注册一个命令行选项；
- 用「外部存储」把值放进文件作用域的全局变量；
- 提供一个 getter 函数（如 `getCurrentBytecodeVersion()`）让 `BytecodeTranslation.cpp` 读取。

`--bytecode-version` 是其中最复杂的一个，因为它接受的值是一个**结构化的版本号**（`major.minor.tag`），所以专门写了一个自定义解析器 `BytecodeVersionParser`，并能在非法值时给出「支持区间」提示。

#### 4.3.2 核心流程

`--bytecode-version` 的解析与取值：

```
用户命令行 --bytecode-version=13.1
   │
   ├─ BytecodeVersionParser::parse("13.1")
   │     ├─ consumeInteger → verMajor=13, "." → verMinor=1
   │     ├─ 无 ".tag" → tag=0
   │     ├─ BytecodeVersion::fromVersion(13,1,0)  // 查 TableGen 生成的白名单
   │     │     └─ 命中 → 写入选项值；未命中 → o.error("Invalid argument ...")
   │     └─ 写入静态 bytecodeVersionPtr 指向的 opt
   │
   └─ 翻译闭包调用 getCurrentBytecodeVersion()
         └─ 返回 *bytecodeVersionPtr（若无注册则回退 kCurrentVersion）
```

`--list-versions` 用了一个**回调式选项**：它不是用来「设置某个状态」，而是在被传入时**直接执行**一段逻辑（打印全部支持版本后 `exit(0)`），所以用户只要一加这个参数，工具就立刻列出版本并退出，根本不会进入翻译流程。

`-Wunsupported-hints` 与 `-Werr-hints` 则是两个布尔开关，用 `cl::location(...)` 把值写进 `warnUnsupportedHintsVar` / `errorUnsupportedHintsVar` 两个全局布尔量，再由 `getWarnUnsupportedHints()` / `getErrorUnsupportedHints()` 读出。

#### 4.3.3 源码精读

先看头文件里这组函数的声明——四个注册/取值函数成对出现：

[include/cuda_tile/Bytecode/Common/CommandLineOptions.h:18-38](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Bytecode/Common/CommandLineOptions.h#L18-L38) —— 声明三组注册函数（版本号、提示开关、列版本）与对应的 getter：

```cpp
/// Register command line options for Cuda Tile IR bytecode version.
void registerTileIRBytecodeVersionOption();
/// Register command line options for OptimizationHints diagnostics
void registerTileIROptimizationHintsOptions();
/// Get the current bytecode version ... default version if no option was set.
BytecodeVersion getCurrentBytecodeVersion();
/// Get the unsupported optimization hint warning flag ... default (false).
bool getWarnUnsupportedHints();
/// Get the unsupported optimization hint error flag ... default (false).
bool getErrorUnsupportedHints();
/// Register command line option to list supported bytecode versions.
void registerListVersionsOption();
```

接着看实现。先是自定义版本解析器——注意它在非法值时把 `kMinSupportedVersion` 与 `kCurrentVersion` 拼进报错信息：

[lib/Bytecode/Common/CommandLineOptions.cpp:28-60](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/CommandLineOptions.cpp#L28-L60) —— 解析 `major.minor[.tag]`，并交 `BytecodeVersion::fromVersion` 校验是否在白名单内：

```cpp
bool parse(llvm::cl::Option &o, StringRef /*argName*/, StringRef arg,
           BytecodeVersion &v) {
  StringRef versionStr = arg;
  uint8_t verMajor, verMinor;
  if (versionStr.consumeInteger(10, verMajor) ||
      !versionStr.consume_front(".") ||
      versionStr.consumeInteger(10, verMinor))
    return o.error("Invalid argument '" + arg + "'");
  uint16_t tag = 0;
  if (versionStr.consume_front(".") && versionStr.consumeInteger(10, tag))
    return o.error("Invalid argument '" + arg + "'");
  if (!versionStr.empty())
    return o.error("Invalid argument '" + arg + "'");
  std::optional<BytecodeVersion> version =
      BytecodeVersion::fromVersion(verMajor, verMinor, tag);
  if (!version) {
    return o.error(
        llvm::formatv("Invalid argument '{0}': the supported versions are [{1} - {2}]",
                      arg, BytecodeVersion::kMinSupportedVersion,
                      BytecodeVersion::kCurrentVersion).str());
  }
  v = *version;
  return false;
}
```

[lib/Bytecode/Common/CommandLineOptions.cpp:73-87](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/CommandLineOptions.cpp#L73-L87) —— 注册 `--bytecode-version`，默认值是 `kCurrentVersion`（13.3），并把选项地址存进 `bytecodeVersionPtr` 供 getter 读取：

```cpp
void mlir::cuda_tile::registerTileIRBytecodeVersionOption() {
  static llvm::cl::opt<BytecodeVersion, /*ExternalStorage=*/false,
                       BytecodeVersionParser>
      bytecodeVersion("bytecode-version",
                      llvm::cl::desc("Bytecode version to use for translation"),
                      llvm::cl::init(BytecodeVersion::kCurrentVersion));
  bytecodeVersionPtr = &bytecodeVersion;
}

BytecodeVersion mlir::cuda_tile::getCurrentBytecodeVersion() {
  return bytecodeVersionPtr ? *bytecodeVersionPtr
                            : BytecodeVersion::kCurrentVersion;
}
```

注意 `getCurrentBytecodeVersion` 的三元判断：若没人调用过 `registerTileIRBytecodeVersionOption()`（例如库被嵌入别的程序而没注册此选项），它就安全回退到 `kCurrentVersion`，避免空指针解引用。

再看提示开关与「列版本」回调：

[lib/Bytecode/Common/CommandLineOptions.cpp:89-101](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/CommandLineOptions.cpp#L89-L101) —— 用 `cl::location(...)` 把两个布尔选项的值写进全局变量，对应 README/u6-l1 的 `-Wunsupported-hints` 与 `-Werr-hints`：

```cpp
void mlir::cuda_tile::registerTileIROptimizationHintsOptions() {
  static llvm::cl::opt<bool, true> warnUnsupportedHints(
      "Wunsupported-hints",
      llvm::cl::desc("Enable warnings for unsupported/invalid optimization hints."),
      llvm::cl::location(warnUnsupportedHintsVar), llvm::cl::init(false));
  static llvm::cl::opt<bool, true> errorOnHints(
      "Werr-hints",
      llvm::cl::desc("Treat unsupported/invalid optimization hints as errors."),
      llvm::cl::location(errorUnsupportedHintsVar), llvm::cl::init(false));
}
```

[lib/Bytecode/Common/CommandLineOptions.cpp:111-123](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/CommandLineOptions.cpp#L111-L123) —— `--list-versions` 是一个回调选项：一旦传入就调用 `getSupportedVersions()` 打印全部支持版本并 `exit(0)`：

```cpp
void mlir::cuda_tile::registerListVersionsOption() {
  static llvm::cl::opt<bool> listVersions(
      "list-versions",
      llvm::cl::desc("List all supported bytecode versions and exit"),
      llvm::cl::init(false), llvm::cl::callback([](const bool &val) {
        if (val) {
          auto versions = getSupportedVersions();
          for (const auto &version : versions)
            llvm::outs() << version.toString() << "\n";
          exit(0);
        }
      }));
}
```

这里出现的 `kCurrentVersion`、`kMinSupportedVersion`、`kCurrentCompatibilityVersion` 三个常量都定义在 Version.cpp：

[lib/Bytecode/Common/Version.cpp:39-64](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/Version.cpp#L39-L64) —— 三个关键版本常量：兼容版 13.1、当前版 13.3、最低支持版 13.1。这解释了为什么「默认写 13.3、面向广泛兼容应显式指定 13.1」：

```cpp
const BytecodeVersion BytecodeVersion::kCurrentCompatibilityVersion = {13, 1, 0};
const BytecodeVersion BytecodeVersion::kCurrentVersion             = {13, 3, 0};
const BytecodeVersion BytecodeVersion::kMinSupportedVersion        = {13, 1, 0};
```

> 对应到 README 的官方用法：`cuda-tile-translate example.mlir --bytecode-version=13.1 --mlir-to-cudatilebc --no-implicit-module -o example.tilebc`（见 [README.md:328](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/README.md#L328)）。这里 `--bytecode-version=13.1` 显式锁到兼容版，正是 `kCurrentCompatibilityVersion` 的用途。

#### 4.3.4 代码实践

**实践目标**：亲手验证三个选项的实际行为，并对照源码确认报错文案。

**操作步骤**（命令行型，需要已构建 `cuda-tile-translate`）：

1. 把 4.2.4 的最小 MLIR 存为 `ex.mlir`。
2. 列出全部支持版本：`cuda-tile-translate --list-versions`。
3. 编译为 13.1 字节码：`cuda-tile-translate ex.mlir --mlir-to-cudatilebc --no-implicit-module --bytecode-version=13.1 -o ex.tilebc`。
4. 反向翻译回 MLIR：`cuda-tile-translate --cudatilebc-to-mlir ex.tilebc -o ex.rt.mlir`。
5. 故意指定一个不存在的版本：`cuda-tile-translate ex.mlir --mlir-to-cudatilebc --no-implicit-module --bytecode-version=12.0 -o /dev/null`。

**需要观察的现象**：

- 第 2 步应打印若干行版本号（生产构建为 `13.1`、`13.2`、`13.3`，开启测试时还多出 `250.0`、`250.1`——见 [test/Bytecode/list_versions.mlir:4-10](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Bytecode/list_versions.mlir#L4-L10)）。
- 第 3、4 步应都成功；`ex.rt.mlir` 与 `ex.mlir` 在去掉空行后内容等价（round-trip）。
- 第 5 步应失败，报错形如 `Invalid argument '12.0': the supported versions are [13.1 - 13.3]`（见 [test/Bytecode/unsupportedVersionTest.mlir:1-2](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Bytecode/unsupportedVersionTest.mlir#L1-L2)）。

**预期结果**：上面三条都可在本地验证；若环境未构建工具，则第 5 步的报错文案可由该测试用例直接确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `--list-versions` 用 `cl::callback` + `exit(0)`，而不是设一个布尔标志让主循环去判断？

**参考答案**：因为「列出版本」是一个**与翻译无关**的自包含动作——用户只想知道支持哪些版本，根本不打算提供输入文件或选择翻译。用回调在解析到该选项的瞬间就打印并退出，可以避免「没给输入文件」「没选翻译」等后续无关报错干扰输出。

**练习 2**：若有人把本库嵌入自己的程序、只调用了 `registerTileIRTranslations()` 却忘了 `registerTileIRBytecodeVersionOption()`，运行翻译时 `--bytecode-version` 还能用吗？写出字节码时版本会是多少？

**参考答案**：`--bytecode-version` 不可用（选项未注册，会被 `cl` 当未知参数拒绝）。但由于 `getCurrentBytecodeVersion()` 有空指针保护（`bytecodeVersionPtr ? *bytecodeVersionPtr : kCurrentVersion`），翻译仍能跑通，写出的是默认的 13.3 字节码。这是一个「安全回退」设计。

### 4.4 延伸：测试用的 test-cudatile-roundtrip 与跨进程往返脚本

#### 4.4.1 概念说明

第 4.1 节提到 `main` 还调了 `registerTileIRTestTranslations()`，它注册了一个**第三个翻译** `test-cudatile-roundtrip`，专门用于回归测试。它和前两个翻译的区别是：前两个是「MLIR→字节码」或「字节码→MLIR」的单向转换，而 `test-cudatile-roundtrip` 在**同一个进程内**先把 module 序列化成字节码、立刻又反序列化回来，再把结果打印出来——一次调用就走完一个完整往返。

这种「进程内往返」用于快速验证「写出来再读回去」不丢信息。但真实的端到端往返（生成磁盘上的 `.tilebc` 文件、换一次进程再读）是由测试脚本 `test/round_trip_test.py` 用**两次** `cuda-tile-translate` 调用拼接出来的。两者服务于不同测试粒度，但底层都依赖 4.2 注册的那两个翻译。

#### 4.4.2 核心流程

进程内往返（`test-cudatile-roundtrip`）：

```
mlirTranslateMain 解析 .mlir → cuda_tile::ModuleOp op
  │
  └─ roundTripModule(op, output, version, useGenericForm)
        ├─ writeBytecode(rvo, op, version)        // 写入内存 buffer
        ├─ readBytecode(bufferRef, *context)      // 从同一 buffer 读回
        └─ deserializedModule->print(output, flags)  // 打印读回后的 IR
```

跨进程往返（`round_trip_test.py`）：

```
cuda-tile-translate -mlir-to-cudatilebc in.mlir -o out.tilebc   # 第 1 次：写磁盘
cuda-tile-translate -cudatilebc-to-mlir out.tilebc -o rt.mlir   # 第 2 次：读磁盘
cuda-tile-opt in.mlir -o ref.mlir                                # 生成参考
diff(-B) ref.mlir rt.mlir                                         # 比较（忽略空行）
```

#### 4.4.3 源码精读

[tools/cuda-tile-translate/test/RoundTripTestRegistration.cpp:30-60](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-translate/test/RoundTripTestRegistration.cpp#L30-L60) —— `roundTripModule` 把写、读两步串在一个函数里，复用同一个 `MLIRContext`；`--generic-form` 控制是否以 MLIR 通用形式打印：

```cpp
static LogicalResult roundTripModule(cuda_tile::ModuleOp op, raw_ostream &output,
                                     BytecodeVersion version, bool useGenericForm) {
  SmallVector<char, 4096> bytecodeBuffer;
  llvm::raw_svector_ostream rvo(bytecodeBuffer);
  MLIRContext *context = op->getContext();
  auto dialect = cast<CudaTileDialect>(op->getDialect());
  dialect->setWarnUnsupportedHints(getWarnUnsupportedHints());
  dialect->setErrorOnHints(getErrorUnsupportedHints());
  if (failed(writeBytecode(rvo, op, version)))
    return failure();
  llvm::MemoryBufferRef bytecodeBufferRef(
      llvm::StringRef(bytecodeBuffer.data(), bytecodeBuffer.size()),
      "roundTripModuleBuffer");
  OwningOpRef<cuda_tile::ModuleOp> deserializedModule =
      readBytecode(bytecodeBufferRef, *context);
  if (!deserializedModule) { op->emitError("Failed to deserialize bytecode"); return failure(); }
  OpPrintingFlags flags;
  if (useGenericForm) flags.printGenericOpForm();
  deserializedModule->print(output, flags);
  output << "\n";
  return success();
}
```

[tools/cuda-tile-translate/test/RoundTripTestRegistration.cpp:62-75](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-translate/test/RoundTripTestRegistration.cpp#L62-L75) —— 注册 `test-cudatile-roundtrip`，同样用 `TranslateFromMLIRRegistration`（输入是 MLIR 文本），并复用 `getCurrentBytecodeVersion()`：

```cpp
void mlir::cuda_tile::registerTileIRTestTranslations() {
  static llvm::cl::opt<bool> useGenericForm(
      "generic-form", llvm::cl::desc("Print operations in generic form"),
      llvm::cl::init(false));
  TranslateFromMLIRRegistration roundtrip(
      "test-cudatile-roundtrip",
      "Test bytecode serialization and deserialization round-trip",
      [](cuda_tile::ModuleOp op, llvm::raw_ostream &output) {
        return roundTripModule(op, output, getCurrentBytecodeVersion(),
                               useGenericForm);
      },
      [](DialectRegistry &registry) { registry.insert<CudaTileDialect>(); });
}
```

对比两个机制：进程内 `test-cudatile-roundtrip` 不落盘、不换进程，适合在一条 `RUN` 行里同时触发「写+读」并断言；而 `round_trip_test.py` 落盘两次调用，更贴近真实「先编译、后加载」的使用方式。字节码版本相关测试大量用前者，例如 [test/Bytecode/versioning/test_forward_compatibility.mlir:4-5](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Bytecode/versioning/test_forward_compatibility.mlir#L4-L5) 用 `-test-cudatile-roundtrip -bytecode-version=250.0` 在不同目标版本下做往返。

## 5. 综合实践

**任务**：自己当一次「翻译管线」——从一段 MLIR 出发，走完整条「序列化 → 反序列化 → 比较」链路，并把每一步对应到本讲引用的源码函数。

**步骤**：

1. 准备输入 `sum.mlir`（示例代码）：

   ```
   cuda_tile.module @kernels {
     cuda_tile.entry @addone(%a: !cuda_tile.tile<4xf32>) -> !cuda_tile.tile<4xf32> {
       %one = cuda_tile.constant <f32: [1.0, 1.0, 1.0, 1.0]> : !cuda_tile.tile<4xf32>
       %r = cuda_tile.addf %a, %one : !cuda_tile.tile<4xf32>
       cuda_tile.return %r : !cuda_tile.tile<4xf32>
     }
   }
   ```

2. **序列化**（对应 `registerToTileIRBytecodeTranslation` 闭包）：

   ```
   cuda-tile-translate sum.mlir --mlir-to-cudatilebc --no-implicit-module --bytecode-version=13.1 -o sum.tilebc
   ```

   在脑中标注：`--bytecode-version=13.1` 由 `BytecodeVersionParser` 解析 → `getCurrentBytecodeVersion()` 返回 13.1 → `writeBytecode(out, module, 13.1)`。

3. **反序列化**（对应 `registerFromTileIRBytecodeTranslation` 闭包）：

   ```
   cuda-tile-translate --cudatilebc-to-mlir sum.tilebc -o sum.rt.mlir
   ```

   标注：字节码字符串 → `deserializeModule` → `readBytecode` → 打印成 `sum.rt.mlir`。

4. **比较**：用 `diff -B sum.mlir sum.rt.mlir`（或参照 `round_trip_test.py` 去掉空行后比较）确认两者等价。

5. **进程内往返**（可选）：换用 `cuda-tile-translate sum.mlir --test-cudatile-roundtrip --no-implicit-module --bytecode-version=13.1`，观察它一次调用就输出往返后的 IR。

6. **对照源码**：在 `BytecodeTranslation.cpp` 里逐一指出第 2、3 步分别走了哪段代码（[L65-L80](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Translation/BytecodeTranslation.cpp#L65-L80) 与 [L35-L42](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Translation/BytecodeTranslation.cpp#L35-L42)），并说明 `setWarnUnsupportedHints` / `setErrorOnHints` 在两条路径上都已被调用。

**预期结果**：`sum.rt.mlir` 与 `sum.mlir` 去空行后一致；进程内往返输出与跨进程往返结果一致。若未构建工具，则按「源码阅读型实践」逐行对照上述函数即可。

## 6. 本讲小结

- `cuda-tile-translate` 是 MLIR `mlir-translate` 的薄壳；`main` 只做两件事——**先注册所有选项与翻译，再把控制权交给 `mlirTranslateMain`**。注册顺序的硬约束来自「命令行解析必须看到已注册的选项」。
- `registerTileIRTranslations()` 注册两个对称翻译：`mlir-to-cudatilebc`（`TranslateFromMLIRRegistration`，输入 MLIR 文本→输出字节码）与 `cudatilebc-to-mlir`（`TranslateToMLIRRegistration`，输入字节码→输出 MLIR 文本）。记住 from/to 都是相对 MLIR 文本一端而言。
- 序列化路径用 `getCudaTileModuleOp` 兼容「带/不带隐式 `mlir::ModuleOp` 包装」两种输入；两条路径都把 `-Wunsupported-hints` / `-Werr-hints` 通过 `setWarnUnsupportedHints` / `setErrorOnHints` 注入方言对象，保证写入器/读取器看到一致的提示行为。
- `--bytecode-version` 由自定义 `BytecodeVersionParser` 解析 `major.minor.tag`，默认值 `kCurrentVersion`=13.3，非法值报错带出支持区间 `[13.1 - 13.3]`；`--list-versions` 用回调在解析期直接打印并 `exit(0)`；两个提示开关用 `cl::location` 外部存储。
- 测试侧还有第三个翻译 `test-cudatile-roundtrip`，在进程内把「写→读」串起来；跨进程往返则由 `round_trip_test.py` 用两次工具调用拼接。两者底层都复用本讲的两个翻译。
- 命令行 getter（如 `getCurrentBytecodeVersion`）都有「未注册则安全回退默认值」的空指针保护，方便库被嵌入未注册全部选项的第三方程序。

## 7. 下一步学习建议

本讲只把 `writeBytecode` 与 `readBytecode` 当黑盒。建议按以下顺序继续：

- **u7-l2 字节码写入器**：精读 `lib/Bytecode/Writer/BytecodeWriter.cpp`，看清 `writeBytecode` 如何把 ModuleOp 拆成 magic + 各 Section（String/Type/Constant/Func/Debug/Global/Producer），以及变长整数编码。
- **u7-l3 字节码读取器**：精读 `lib/Bytecode/Reader/BytecodeReader.cpp`，看清 `readBytecode` 如何校验 magic、惰性构建类型表、逐指令重建操作。
- **u7-l4 字节码版本与兼容性**：精读 `Version.cpp` 与 `BytecodeOpcodes.td`，理解 `kCurrentVersion`/`kCurrentCompatibilityVersion`/`kMinSupportedVersion` 三者的演进意义与 `sinceVersion` 如何驱动读写器的前后向兼容判断。
- 读完 u7 全单元后，可回头对比 u8-l1（`cuda-tile-tblgen` 字节码代码生成），理解本讲读写器用到的 opcode 枚举与版本白名单其实都是 TableGen 自动生成的。
