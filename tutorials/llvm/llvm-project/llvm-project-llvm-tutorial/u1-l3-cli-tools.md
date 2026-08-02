# 命令行工具入口：opt / llc / lli / llvm-as / llvm-dis

## 1. 本讲目标

在 [u1-l1](u1-l1-project-overview.md) 里，我们用一句话记住了五个工具的分工；在 [u1-l2](u1-l2-build-and-layout.md) 里，我们知道了 CMake 把它们构建到 `build/bin/`。本讲要更进一步，**打开这些工具的 `main()` 看一看它们到底做了什么**。

学完本讲，你应当能够：

- 说清 `opt`、`llc`、`lli`、`llvm-as`、`llvm-dis` 各自吃进什么、吐出什么；
- 理解「工具只是 `lib/` 上一薄层壳（thin shell）」这句话在源码层面是怎么体现的；
- 用一条数据流把它们串起来：`.ll` 文本 →（`llvm-as`）→ `.bc` 位码 →（`opt`）→ 优化后的 `.bc` →（`llc`）→ 汇编 / 目标文件，或被 `lli` 直接执行；
- 在命令行上完成一次「写一段 IR → 转位码 → 跑 `mem2reg` → 看变化」的完整操作。

## 2. 前置知识

### 2.1 LLVM IR 有两种存储格式

同一段 LLVM IR，既能写成**人类可读的文本**（文件后缀 `.ll`），也能存成**紧凑的二进制位码**（后缀 `.bc`，读作 bitcode）。两者表达的语义完全等价，只是编码不同：

| 格式 | 后缀 | 特点 | 典型用途 |
|------|------|------|----------|
| 文本汇编（assembly） | `.ll` | 可读、可手写、可 diff | 学习、调试、写测试 |
| 位码（bitcode） | `.bc` | 紧凑、可被工具快速加载 | 前端/优化器/后端之间传递、磁盘存储 |

`llvm-as`（assembler）和 `llvm-dis`（disassembler）就是这两者之间的「翻译器」，正反向各一个。

### 2.2 「薄壳工具」是什么意思

回顾 [u1-l2](u1-l2-build-and-layout.md)：LLVM 的真正逻辑都放在 `lib/` 下的组件库里，`tools/` 下的工具只负责**解析命令行参数 → 调用 `lib/` 里的函数 → 把结果写到文件/标准输出**。本讲读源码时你会反复看到这个模式：工具的 `main()` 很短，真正干活的是它调用的库函数。

> 术语速查：`cl::opt` / `cl::ParseCommandLineOptions` 是 LLVM 自带的命令行解析库（`llvm/Support/CommandLine.h`），几乎所有工具都用它来声明和解析参数；`Module` 是一段 IR 在内存里的根对象（见后续 [u2-l1](u2-l1-ir-hierarchy.md)）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `tools/opt/opt.cpp` | `opt` 工具的入口 `main()`，仅负责把控制权交给 `optMain` |
| `tools/opt/optdriver.cpp` | `opt` 真正的驱动逻辑（参数定义、加载 IR、跑流水线），编译成 `LLVMOptDriver` 库 |
| `tools/opt/NewPMDriver.cpp` | 「新 Pass 管理器」流水线的实际执行 `runPassPipeline` |
| `tools/llc/llc.cpp` | 静态编译器 `llc` 的入口与 `compileModule` 核心逻辑 |
| `tools/lli/lli.cpp` | 解释器 / JIT 执行器 `lli` 的入口，含 `runOrcJIT` |
| `tools/llvm-as/llvm-as.cpp` | 把 `.ll` 转成 `.bc` |
| `tools/llvm-dis/llvm-dis.cpp` | 把 `.bc` 转回 `.ll` |
| `docs/CommandGuide/*.rst` | 每个工具的官方命令手册（man page 风格） |

下面按**数据流向**讲解：先讲格式互转（4.1），再讲优化（4.2），再讲代码生成（4.3），最后讲执行（4.4）。这个顺序就是 IR 在工具链中流动的真实顺序。

```text
  手写 / 前端产出
       .ll ──llvm-as──▶ .bc ──opt──▶ 优化后的 .bc ──llc──▶ .s / .o
                         │                              （汇编 / 目标文件）
                         └────lli────▶ 直接在内存里执行
```

## 4. 核心概念与源码讲解

### 4.1 先认识 IR 的两种表示：llvm-as 与 llvm-dis

#### 4.1.1 概念说明

`llvm-as` 和 `llvm-dis` 是一对最简单的工具，**只做格式转换、不做任何优化**：

- `llvm-as`：读入 `.ll` 文本 → 解析成内存里的 `Module` → 用 `BitcodeWriter` 序列化成 `.bc`；
- `llvm-dis`：读入 `.bc` 位码 → 用 `BitcodeReader` 反序列化成 `Module` → 调用 `Module::print` 输出成 `.ll`。

它们是观察 IR 最趁手的「放大镜」，也是后续 `opt`、`llc`、`lli` 加载 IR 的同款机制的简化版。先把这对工具搞懂，后面三个工具的「加载 IR」环节你就能举一反三。

#### 4.1.2 核心流程

**llvm-as 的流程**（`main` → `WriteOutputFile`）：

1. 用 `cl::ParseCommandLineOptions` 解析命令行，得到输入文件名；
2. 创建一个 `LLVMContext`（IR 的上下文/宿主）；
3. 调用 `parseAssemblyFileWithIndex` 把 `.ll` 解析成 `Module`；
4. 用 `verifyModule` 检查 IR 是否合法；
5. 调用 `WriteBitcodeToFile` 把 `Module` 写成 `.bc`。

**llvm-dis 的流程**（`main`）：

1. 解析命令行；
2. 对每个输入文件，用 `MemoryBuffer::getFileOrSTDIN` 把字节读进来；
3. 用 `getBitcodeFileContents` 解析位码，再 `materializeAll` 物化成 `Module`；
4. 调用 `M->print(...)` 把 `Module` 打印成文本 `.ll`。

两者正好是互逆过程，一个调 `WriteBitcodeToFile`，一个调 `print`。

#### 4.1.3 源码精读

**llvm-as 的 `main`（精简后）**：

```cpp
int main(int argc, char **argv) {
  InitLLVM X(argc, argv);
  cl::ParseCommandLineOptions(argc, argv, "llvm .ll -> .bc assembler\n");
  LLVMContext Context;
  SMDiagnostic Err;
  ...
  ParsedModuleAndIndex ModuleAndIndex;
  ...
  ModuleAndIndex = parseAssemblyFileWithIndex(InputFilename, Err, Context,
                                              nullptr, SetDataLayout);
  std::unique_ptr<Module> M = std::move(ModuleAndIndex.Mod);
  ...
  if (verifyModule(*M, &OS)) { /* 报错退出 */ }
  ...
  if (!DisableOutput)
    WriteOutputFile(M.get(), Index.get());
  return 0;
}
```

这几行就是「薄壳工具」的标准骨架。它实际调用的是 [tools/llvm-as/llvm-as.cpp:110-161](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/llvm-as/llvm-as.cpp#L110-L161) 这段。其中：

- `parseAssemblyFileWithIndex`（[tools/llvm-as/llvm-as.cpp:128-129](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/llvm-as/llvm-as.cpp#L128-L129)）来自 `lib/AsmParser/`，负责把文本 `.ll` 解析成内存 IR；
- `verifyModule`（[tools/llvm-as/llvm-as.cpp:142](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/llvm-as/llvm-as.cpp#L142)）是「验证器」，确保 IR 语义合法（SSA 正确、类型匹配等），非法 IR 直接报错退出；
- 真正写出位码的是 `WriteBitcodeToFile`（[tools/llvm-as/llvm-as.cpp:98-99](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/llvm-as/llvm-as.cpp#L98-L99)），它来自 `lib/Bitcode/Writer/`。

> 注意输出文件名的推断规则（[tools/llvm-as/llvm-as.cpp:67-75](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/llvm-as/llvm-as.cpp#L67-L75)）：输入 `x.ll` 则输出 `x.bc`，输入是 `-`（标准输入）则输出也走标准输出。

**llvm-dis 的核心是「读位码 + print」**：

```cpp
BitcodeFileContents IF = ExitOnErr(llvm::getBitcodeFileContents(*MB));
...
M = ExitOnErr(MB.getLazyModule(Context, MaterializeMetadata, SetImporting));
...
ExitOnErr(M->materializeAll());
...
if (!DontPrint) {
  if (M) M->print(Out->os(), Annotator.get(), /* ShouldPreserveUseListOrder */ false);
  ...
}
```

- `getBitcodeFileContents`（[tools/llvm-dis/llvm-dis.cpp:204](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/llvm-dis/llvm-dis.cpp#L204)）来自 `lib/Bitcode/Reader/`，把二进制位码解析出来；
- `M->print`（[tools/llvm-dis/llvm-dis.cpp:266-267](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/llvm-dis/llvm-dis.cpp#L266-L267)）把内存里的 `Module` 重新格式化成 `.ll` 文本。

#### 4.1.4 代码实践：亲手做一次往返转换

**实践目标**：验证 `.ll` 与 `.bc` 可以无损互转，并理解位码是人类不可读的二进制。

**操作步骤**：

1. 把下面这段最小 IR（含一个函数 `@f`，使用现代的「不透明指针」`ptr` 语法）存成 `demo.ll`：

   ```llvm
   ; demo.ll：把参数加 1 后返回
   define i32 @f(i32 %x) {
   entry:
     %a = alloca i32          ; 在栈上分配一个 i32
     store i32 %x, ptr %a     ; 把参数存进去
     %b = load i32, ptr %a    ; 再读出来
     %c = add i32 %b, 1       ; 加 1
     ret i32 %c
   }
   ```

2. 转成位码：`llvm-as demo.ll -o demo.bc`（成功后无输出，生成 `demo.bc`）。
3. 看一眼位码是二进制：用文本工具打开 `demo.bc` 会看到一堆不可读字符（前几个字节是 bitcode 的「魔数」）。
4. 转回文本：`llvm-dis demo.bc -o demo.roundtrip.ll`。

**需要观察的现象**：`demo.roundtrip.ll` 的内容与 `demo.ll` 语义一致（可能只是格式/注释略有差异）。

**预期结果**：第 4 步得到的 `.ll` 里，函数 `@f` 的逻辑与原来完全相同——证明 `.ll ⇄ .bc` 是无损双向转换。

> 如果你的机器上还没有 `llvm-as` / `llvm-dis`，可参考 [u1-l2](u1-l2-build-and-layout.md) 完成 CMake + Ninja 构建；下列命令的行为均为「待本地验证」，请以你本机实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：`llvm-as` 默认把 `demo.ll` 输出到哪个文件名？想让它输出到标准输出该加什么参数？
**答案**：默认输出 `demo.bc`（把 `.ll` 后缀换成 `.bc`）。加 `-o -` 即可输出到标准输出。

**练习 2**：如果 `.ll` 里有语法错误，`llvm-as` 在哪一步会失败？依据源码说明。
**答案**：在 `parseAssemblyFileWithIndex`（解析）阶段就会失败并打印 `SMDiagnostic` 错误，根本走不到写出位码那一步。

---

### 4.2 opt：把优化流水线跑在 IR 上

#### 4.2.1 概念说明

`opt` 是 **LLVM 的模块化优化器与分析器**（见 [docs/CommandGuide/opt.rst:14-19](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/docs/CommandGuide/opt.rst#L14-L19)）。它吃进 IR（`.ll` 或 `.bc` 都行），在上面跑一组「优化 pass（变换）」或「分析 pass」，再吐出 IR。

关键点：**`opt` 的输入和输出都是 IR**，它不产生汇编或目标文件——那是 `llc` 的活。`opt` 是三段式模型里「优化器」这一段的命令行化身。

现代 `opt` 用「新 Pass 管理器（New PM）」，通过 `-passes=...` 用一段文本描述要跑的流水线，例如 `-passes=mem2reg,instcombine`（详见 [u3-l3](u3-l3-pass-pipeline.md)）。

#### 4.2.2 核心流程

`opt` 的源码现在被拆成了「极小的入口 + 一个驱动库」：

- `tools/opt/opt.cpp`：只有 `main`，转调 `optMain`；
- `tools/opt/optdriver.cpp`：实现 `optMain`，这才是真正的驱动。

`optMain` 的流程（[tools/opt/optdriver.cpp:401-1000](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/opt/optdriver.cpp#L401-L1000)）：

1. 初始化所有目标（`InitializeAllTargets` 等）；
2. 解析命令行，拿到 `-passes=...` 流水线字符串与输入文件；
3. 用 `parseIRFile` 把 IR 加载成 `Module`，并跑一次验证器；
4. 根据是否提供 `-passes`，决定走「新 PM」还是「旧 PM（legacy）」；
5. 新 PM 路径：调用 `runPassPipeline`，把文本流水线交给 `PassBuilder` 解析并执行；
6. 把优化后的 `Module` 写回 `.bc`（默认）或 `.ll`（加 `-S`）。

#### 4.2.3 源码精读

**入口 `opt.cpp` 极简，只做转发**（[tools/opt/opt.cpp:23-27](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/opt/opt.cpp#L23-L27)）：

```cpp
extern "C" int
optMain(int argc, char **argv,
        ArrayRef<std::function<void(PassBuilder &)>> PassBuilderCallbacks);

int main(int argc, char **argv) { return optMain(argc, argv, {}); }
```

之所以把真正的逻辑放进 `LLVMOptDriver` 库（[tools/opt/CMakeLists.txt:34-52](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/opt/CMakeLists.txt#L34-L52)），是为了让「下游变体」也能复用同一套驱动逻辑——这正是「薄壳」设计的好处。

**`-passes` 选项的定义**（[tools/opt/optdriver.cpp:82-92](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/opt/optdriver.cpp#L82-L92)）：

```cpp
static cl::opt<std::string> PassPipeline(
    "passes",
    cl::desc("A textual (comma separated) description of the pass pipeline e.g.,"
             "-passes=\"foo,bar\", ..."));
static cl::alias PassPipeline2("p", cl::aliasopt(PassPipeline),
                               cl::desc("Alias for -passes"));
```

也就是说，`-passes=mem2reg` 和 `-p=mem2reg` 等价（`-p` 是简写别名）。

**决定走新 PM 还是旧 PM**（[tools/opt/optdriver.cpp:466-467](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/opt/optdriver.cpp#L466-L467)）：

```cpp
const bool UseNPM = !shouldForceLegacyPM() || PassPipeline.getNumOccurrences() > 0;
```

只要你写了 `-passes=...`，就一定走新 PM。新 PM 的实际执行入口是（[tools/opt/optdriver.cpp:805-811](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/opt/optdriver.cpp#L805-L811)：

```cpp
if (!runPassPipeline(argv[0], *M, TM.get(), &TLII, Out.get(), ThinLinkOut.get(),
                     RemarksFile.get(), Pipeline, PluginList,
                     PassBuilderCallbacks, OK, VK, ...))
  return 1;
```

`runPassPipeline` 实现在 `NewPMDriver.cpp`，它会把 `Pipeline`（比如 `"mem2reg"`）交给 `PassBuilder` 解析成真正的 pass 对象序列并在 `Module` 上运行。

> 顺带一提：`-O0`/`-O1`/`-O2`/`-O3` 这些「优化级别」开关（[tools/opt/optdriver.cpp:158-180](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/opt/optdriver.cpp#L158-L180)）等价于 `-passes="default<O2>"` 这样的预置流水线——优化级别的本质就是「一组预先编排好的 pass 序列」。

#### 4.2.4 代码实践：用 opt 跑 mem2reg，观察 IR 变化

**实践目标**：直观感受一个优化 pass 到底「优化」了什么。这里选 `mem2reg`（把可提升的栈分配 `alloca` 提升为 SSA 寄存器），它是 [u4](u4-l1-instcombine-sccp.md) 标量优化的经典一员。

**操作步骤**：

1. 复用 4.1.4 里的 `demo.ll`（它故意用了 `alloca`/`store`/`load`，是 `mem2reg` 的典型输入）。
2. 转位码：`llvm-as demo.ll -o demo.bc`。
3. 跑优化并输出为文本：`opt -passes=mem2reg -S demo.bc -o demo.opt.ll`。
   - `-passes=mem2reg` 指定流水线；
   - `-S` 让输出写成可读 `.ll` 而不是位码。

**需要观察的现象**：对比 `demo.ll` 与 `demo.opt.ll`，`alloca`、`store`、`load` 这三条指令应该全部消失，`%b` 不再存在，`add` 直接用参数 `%x`。

**预期结果**（`mem2reg` 之后大致会变成）：

```llvm
define i32 @f(i32 %x) {
entry:
  %c = add i32 %x, 1
  ret i32 %c
}
```

这就是「优化」在 IR 层面最直白的体现：把低效的「存到内存再读出来」直接换成寄存器操作。具体输出形态以你本机为准（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`opt demo.bc -o demo2.bc`（不带 `-passes`）会发生什么？
**答案**：不跑任何 pass，相当于把 IR 原样读进来再写出去（仅做格式/校验处理），输出 IR 与输入语义一致。

**练习 2**：源码里 `-O2` 和 `-passes="default<O2>"` 是什么关系？依据是什么？
**答案**：等价。源码 [tools/opt/optdriver.cpp:166-167, 784-785](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/opt/optdriver.cpp#L166-L167) 里 `-O2` 的描述就写着 `Same as -passes="default<O2>"`，且 `optMain` 在指定 `-O2` 时会把 `Pipeline` 置为 `"default<O2>"`。

---

### 4.3 llc：把 IR 编译成目标汇编 / 目标文件

#### 4.3.1 概念说明

如果说 `opt` 是「优化器」段的化身，那么 `llc` 就是三段式模型里**「后端」段的命令行化身**——它把 IR 翻译成某个目标架构的汇编（`.s`）或目标文件（`.o`）。官方定义见 [docs/CommandGuide/llc.rst:14-17](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/docs/CommandGuide/llc.rst#L14-L17)。

`llc` 涉及的全部是「代码生成（Code Generation）」的内容：指令选择、寄存器分配、指令调度、汇编发射——这些会在第 5、6 单元深入。本讲只看它的**入口和整体骨架**，建立「从 IR 到目标代码」的全局印象。

#### 4.3.2 核心流程

`llc` 的 `main`（[tools/llc/llc.cpp:371-467](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/llc/llc.cpp#L371-L467)）流程：

1. 初始化所有目标后端（`InitializeAllTargets` 等）——`llc` 必须知道有哪些机器目标可用；
2. 解析命令行（`-march`、`-mcpu`、`-filetype`、`-O0..3` 等）；
3. 进入 `compileModule`。

`compileModule`（[tools/llc/llc.cpp:498-673](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/llc/llc.cpp#L498-L673)）的关键步骤：

1. 解析 IR 得到 `Module`（也支持 `.mir` 机器 IR 输入）；
2. 根据 IR 里的 target triple，用 `TargetRegistry::lookupTarget` 找到对应后端，创建 `TargetMachine`；
3. 用 `GetOutputStream` 决定输出文件（`.s` / `.o` / 标准输出）；
4. 跑验证器后，组装后端 pass 流水线并 `PM.run(*M)`，由后端逐阶段把 IR 降级为机器码并发射。

#### 4.3.3 源码精读

**初始化后端 + 解析参数**（[tools/llc/llc.cpp:371-414](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/llc/llc.cpp#L371-L414)）：

```cpp
int main(int argc, char **argv) {
  InitLLVM X(argc, argv);
  ...
  InitializeAllTargets();
  InitializeAllTargetMCs();
  InitializeAllAsmPrinters();
  InitializeAllAsmParsers();
  ...
  cl::ParseCommandLineOptions(argc, argv, "llvm system compiler\n");
  ...
  for (unsigned I = TimeCompilations; I; --I)
    if (int RetVal = compileModule(argv, PluginList, Context, OutputFilename))
      return RetVal;
  ...
}
```

注意 `llc` 初始化的是 **`InitializeAllTargets`**（全部目标），而后面 `lli` 初始化的是 **`InitializeNativeTarget`**（仅本机目标）——这个区别正是「静态编译给任意架构」与「在本机执行」的本质差异。

**选择输出文件类型**（[tools/llc/llc.cpp:330-343](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/llc/llc.cpp#L330-L343)）：`-filetype=asm` → `.s`，`-filetype=obj` → `.o`，`-filetype=null` → 不输出（仅做性能测量）。

**组装并运行后端流水线**（[tools/llc/llc.cpp:752-760](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/llc/llc.cpp#L752-L760)）：

```cpp
if (EnableNewPassManager || !PassPipeline.empty()) {
  return compileModuleWithNewPM(argv[0], std::move(M), ..., codegen::getFileType());
}
// 否则用 legacy PassManager
legacy::PassManager PM;
PM.add(new TargetLibraryInfoWrapperPass(TLII));
...
```

无论是新 PM 还是旧 PM，最终都是「把一串后端 pass 跑在 `Module` 上」。这些 pass 的具体内容（指令选择、寄存器分配等）会在第 5、6 单元展开。

#### 4.3.4 代码实践：从 IR 到汇编

**实践目标**：亲眼看到 IR 是如何变成某架构汇编的。

**操作步骤**：

1. 仍用 `demo.bc`（或 `demo.opt.bc`）。
2. 生成文本汇编：`llc demo.bc -o demo.s`（默认 `-filetype=asm`）。
3. 直接在终端看汇编：`llc demo.bc -o -`（`-o -` 表示输出到标准输出）。

**需要观察的现象**：`demo.s` 里会出现目标架构的汇编指令（在 x86 上大概是形如 `lea`/`add`/`ret` 的序列），函数名 `f` 会作为一个汇编标签。

**预期结果**：你得到一段可读的、面向本机架构的 `.s` 汇编。若想换架构，可加 `-march=`（如 `-march=riscv32`），但需要该后端被编译进 `llc`（见 [u1-l2](u1-l2-build-and-layout.md) 的 `LLVM_TARGETS_TO_BUILD`）。具体汇编内容待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`-filetype=obj` 和 `-filetype=asm` 的输出文件后缀分别是什么（在非 Windows 上）？
**答案**：`obj` → `.o`，`asm` → `.s`。依据见 `GetOutputStream`（[tools/llc/llc.cpp:330-343](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/llc/llc.cpp#L330-L343)）。

**练习 2**：为什么 `llc` 调用 `InitializeAllTargets()` 而 `lli` 只调用 `InitializeNativeTarget()`？用一句话解释。
**答案**：`llc` 要为**任意**指定架构生成代码，所以需要全部后端；`lli` 只在本机内存里执行，只需要本机后端。

---

### 4.4 lli：直接解释 / JIT 执行 IR

#### 4.4.1 概念说明

`lli` 的特别之处在于：**它不产出文件，而是直接把 IR 跑起来**。它用即时编译（JIT）或解释器在内存里把 IR 变成可执行机器码并调用，最后返回程序的退出码（见 [docs/CommandGuide/lli.rst:14-20](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/docs/CommandGuide/lli.rst#L14-L20)）。

重要约束（来自同一份文档）：`lli` **不是模拟器**，它只能为本机架构解释或 JIT，不能跨架构执行。所以它只初始化本机后端。

`lli` 支持几种执行方式（[tools/lli/lli.cpp:94](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/lli/lli.cpp#L94)）：

```cpp
enum class JITKind { MCJIT, Orc, OrcLazy };
```

默认是 `Orc`（现代 ORC JIT）。也可以用 `-force-interpreter` 强制走纯解释器。

#### 4.4.2 核心流程

`lli` 的 `main`（[tools/lli/lli.cpp:418-448](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/lli/lli.cpp#L418-L448)）的关键判断：

1. 初始化**本机**目标；
2. 解析命令行（含 `--entry-function`，默认 `main`）；
3. 若是 MCJIT 或强制解释器 → 走旧路径；否则 → `runOrcJIT`。

ORC 路径 `runOrcJIT`（[tools/lli/lli.cpp:917-1175](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/lli/lli.cpp#L917-L1175)）：

1. 把主模块解析成 `Module`；
2. 用 `LLLazyJITBuilder` 搭建 ORC JIT（设置目标机、CPU、特性等）；
3. 把模块加入 JIT；
4. 查找入口函数符号 `J->lookup(EntryFunc)`，拿到可执行地址；
5. 调用 `orc::runAsMain(MainFn, ...)` 真正执行，返回退出码。

#### 4.4.3 源码精读

**入口的「分流」逻辑**（[tools/lli/lli.cpp:426-448](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/lli/lli.cpp#L426-L448)）：

```cpp
InitializeNativeTarget();
InitializeNativeTargetAsmPrinter();
InitializeNativeTargetAsmParser();
cl::ParseCommandLineOptions(argc, argv, "llvm interpreter & dynamic compiler\n");
...
if (UseJITKind == JITKind::MCJIT || ForceInterpreter)
  disallowOrcOptions();
else
  return runOrcJIT(argv[0]);
```

**入口函数选项**（[tools/lli/lli.cpp:183-186](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/lli/lli.cpp#L183-L186)）：

```cpp
EntryFunc("entry-function",
          cl::desc("Specify the entry function (default = 'main') of the executable"),
          ...);
```

**ORC 路径里「查找 + 执行」的最后一步**（[tools/lli/lli.cpp:1173-1175](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/lli/lli.cpp#L1173-L1175)）：

```cpp
auto MainAddr = ExitOnErr(J->lookup(EntryFunc));
auto MainFn = MainAddr.toPtr<MainFnTy *>();
int Result = orc::runAsMain(MainFn, InputArgv, StringRef(InputFile));
```

这三行就是「JIT 执行」的临门一脚：`lookup` 触发对 `main` 的 JIT 编译并返回可调用地址，`runAsMain` 把它当 C 的 `main` 一样调用并返回退出码。ORC 的完整层式架构会在 [u8-l1](u8-l1-orc-jit.md) 详讲。

> 对比 `llc`：`llc` 把 IR 编译成**文件**（`.s`/`.o`）就结束；`lli` 把 IR 编译成**内存里的机器码**并**立即调用**。两者都用后端做代码生成，但目的不同。

#### 4.4.4 代码实践：让 lli 执行一段 IR

**实践目标**：用 `lli` 直接运行 IR，看到程序的「运行结果」。

**操作步骤**：

1. 写一个带 `main` 的 IR（`lli` 默认入口是 `main`），存为 `prog.ll`：

   ```llvm
   ; prog.ll
   define i32 @main() {
   entry:
     ret i32 42
   }
   ```

2. 转位码：`llvm-as prog.ll -o prog.bc`。
3. 执行：`lli prog.bc`。
4. 查看退出码：`echo $?`。

**需要观察的现象**：第 3 步无输出，第 4 步应打印 `42`（因为 `main` 返回了 42，`lli` 把它作为进程退出码）。

**预期结果**：退出码为 42。注意退出码只能是 0–255，所以别让 `main` 返回太大的数。行为待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`lli` 默认从哪个函数开始执行？想换成 `@my_start` 该怎么做？
**答案**：默认从 `main` 开始（[tools/lli/lli.cpp:183-186](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/lli/lli.cpp#L183-L186)）。加 `--entry-function=my_start`。

**练习 2**：`lli -force-interpreter prog.bc` 和默认 `lli prog.bc` 在执行机制上有什么不同？
**答案**：默认用 JIT（ORC）把 IR 编译成本机机器码再执行；`-force-interpreter` 则跳过 JIT，用纯解释器逐条解释 IR（见 [tools/lli/lli.cpp:104-107, 484-486](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/lli/lli.cpp#L104-L107)）。

---

## 5. 综合实践：把一段 IR 走完整条流水线

把本讲四个模块串起来：用同一段手写 IR，依次走完「转码 → 优化 → 代码生成 → 执行」全流程，亲手体验三段式模型的「优化器段 + 后端段」在命令行上是什么样子。

**输入 IR**（存为 `sum.ll`，故意写得低效，便于观察优化效果）：

```llvm
; sum.ll：计算 (n) + 1，但用 alloca/load/store 绕了一圈
define i32 @main() {
entry:
  %p = alloca i32
  store i32 7, ptr %p
  %v = load i32, ptr %p
  %r = add i32 %v, 1
  ret i32 %r
}
```

**操作步骤**：

1. **转位码**：`llvm-as sum.ll -o sum.bc`。
2. **优化**：`opt -passes=mem2reg -S sum.bc -o sum.opt.ll`，打开 `sum.opt.ll` 确认 `alloca/store/load` 已被消除（应只剩一个 `add` 和 `ret`）。
3. **再把优化结果转成位码**：`llvm-as sum.opt.ll -o sum.opt.bc`。
4. **代码生成**：`llc sum.opt.bc -o sum.s`，查看 `sum.s` 里的汇编。
5. **直接执行**：`lli sum.opt.bc` 后 `echo $?`，退出码应为 `8`（7 + 1）。

**预期结果**：

- 第 2 步：`main` 体内不再有 `alloca`/`store`/`load`；
- 第 4 步：得到一段本机汇编；
- 第 5 步：退出码 `8`。

**思考延伸**（不必动手）：如果第 2 步把 `-passes=mem2reg` 换成 `-O2`，IR 会变得更精简吗？为什么？（提示：`-O2` 是一整套预置流水线，`mem2reg` 只是其中一员，见 [u3-l3](u3-l3-pass-pipeline.md)。）以上命令的具体输出待本地验证。

## 6. 本讲小结

- LLVM IR 有两种等价格式：可读文本 `.ll` 和紧凑位码 `.bc`；`llvm-as` / `llvm-dis` 是它们之间的无损互转工具，且**只做格式转换、不做优化**。
- 这五个工具都是「薄壳」：`main` 负责解析参数，真正的逻辑都在 `lib/` 的组件库里（如 `lib/AsmParser/`、`lib/Bitcode/`、`lib/Passes/`、后端库）。
- `opt` = 优化器段：吃 IR、吐 IR，用 `-passes=...` 描述流水线；`-O2` 等价于 `-passes="default<O2>"`。
- `llc` = 后端段：把 IR 编译成汇编 `.s` 或目标文件 `.o`，可面向**任意**已编译进来的架构，故初始化 `InitializeAllTargets`。
- `lli` = 执行器：在**本机**内存里 JIT 或解释 IR 并直接运行（`InitializeNativeTarget`），默认入口函数是 `main`，默认走 ORC JIT。
- 完整数据流：`.ll →(llvm-as)→ .bc →(opt)→ 优化后 .bc →(llc)→ .s/.o`，分支 `(lli)→ 直接执行`。

## 7. 下一步学习建议

- 下一讲 [u1-l4 ModuleMaker](u1-l4-module-maker.md) 会换一个角度：不再用文本写 IR，而是用 **C++ 代码**调用 API 直接在内存里构造 `Module`/`Function`/`BasicBlock`，让你从「用工具」过渡到「编程产生 IR」。
- 想深入了解 IR 本身的结构（`Module`/`Function`/`BasicBlock`/`Instruction` 的包含关系），可直接跳到第 2 单元 [u2-l1 IR 层次结构](u2-l1-ir-hierarchy.md)。
- 对「`opt` 的流水线到底怎么被解析和执行」感兴趣，可在第 3 单元 [u3-l1 新 Pass 管理器](u3-l1-new-pass-manager.md) 中找到答案。
- 推荐随手翻阅 `docs/CommandGuide/` 下各工具的 `.rst`，它们是最权威的命令行参考；本讲引用的命令选项都能在那里查到完整说明。
