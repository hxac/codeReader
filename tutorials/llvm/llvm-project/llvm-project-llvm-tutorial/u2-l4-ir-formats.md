# IR 的文本与位码格式

## 1. 本讲目标

在前几讲里，我们用 `IRBuilder` 构造 IR（u2-l3），也用命令行工具 `llvm-as`/`llvm-dis` 在 `.ll` 与 `.bc` 之间互转（u1-l3）。本讲要回答一个更深的问题：**一段 LLVM IR，到底有几种「长相」？它们分别用什么源码去读、去写？**

学完本讲，你应当能够：

- 说清楚 `.ll`（人类可读文本）和 `.bc`（紧凑二进制位码）的本质区别，以及它们各自如何无损地表达同一棵 IR 树。
- 解释 `llvm-as` / `llvm-dis` 在「薄壳」背后分别调用了哪些库函数。
- 理解 `IRReader` 作为「统一读取门面」如何靠文件头的魔数（magic bytes）自动判断格式并分发。
- 读懂「文本解析链路（AsmParser）」和「位码读写链路（Bitcode Reader/Writer）」各自的入口与核心流程。
- 知道什么是「惰性加载（lazy loading）」以及为什么位码格式天然支持它。

本讲是承接「生产 IR」之后的关键一环：只有理解了 IR 的两种序列化形态，后续学习 Pass 流水线（u3）时，才能明白 `opt`、`lli` 这些工具是如何「读进来一个 Module、跑完 Pass、再写出去一个 Module」的。

## 2. 前置知识

### 2.1 同一棵 IR 树，两种序列化方式

在内存里，IR 是一棵树（u2-l1 讲过）：`Module → Function → BasicBlock → Instruction`。一旦程序退出，内存就没了，所以要把这棵树**序列化（serialize）**成字节流存到磁盘上；反过来，读文件就是把字节流**反序列化（deserialize）**重建为内存里的树。

LLVM 为同一棵树提供了两套等价的序列化方案：

| 方案 | 后缀 | 面向 | 可读性 | 体积 | 是否支持惰性加载 |
|------|------|------|--------|------|------------------|
| 文本汇编（LLVM Assembly） | `.ll` | 人 | 高（直接 `cat` 就能看） | 大 | 否（一次全解析） |
| 位码（LLVM Bitcode） | `.bc` | 机器 | 低（二进制） | 小（位级压缩） | 是 |

二者表达力完全等价，可以无损互转。你可以把 `.ll` 想成「带缩进的源代码」，把 `.bc` 想成「把这棵树按固定规则打包成的字节流」。

### 2.2 格式探测：靠文件头几个字节

操作系统不会告诉我们一个文件是 `.ll` 还是 `.bc`（后缀名可以随便改）。真正可靠的判断依据是**文件开头的魔数（magic bytes）**——一段约定好的、能唯一标识格式的字节序列。位码格式的魔数就藏在源码里，本讲会带你找到它。IRReader 正是「读前 4 个字节」来决定走哪条解析链路的。

### 2.3 位流（Bitstream）：位级打包的容器格式

`.bc` 文件并不是「一条指令几个字节」的简单堆叠，而是套在一个叫 **Bitstream** 的容器里。Bitstream 把数据压到**比特（bit）**粒度：常用的小整数只占几位，并允许定义「缩写（abbreviation）」来给高频记录做定制编码。它的内容组织成嵌套的「块（block）」和「记录（record）」，类似 XML/JSON 的层级结构，但是二进制的。本讲会展示 Module 是如何被装进一个个 block 里的。

### 2.4 惰性加载（lazy loading）：用的时候才解码

位码格式有一个文本格式做不到的本事：**不必一次把整个文件解码完**。它可以只读 Module 级的「目录信息」（有哪些函数、全局变量），把每个函数的指令体先跳过，等真正用到某个函数时再回到文件里把它对应的那段字节解码出来。这就是「惰性加载」，是 LTO、JIT 等场景下节省内存和启动时间的关键。

### 2.5 门面模式（Facade）

「门面模式」指用一个统一入口把背后的多个子系统藏起来。`IRReader` 就是门面：调用者只管说「给我读这个文件，返回一个 `Module`」，至于背后是 AsmParser 还是 BitcodeReader，由它自动判断。理解这个分工，能帮你把本讲的两条链路串成一张图。

## 3. 本讲源码地图

本讲涉及的关键文件与职责：

| 文件 | 所属组件 | 职责 |
|------|----------|------|
| [lib/IRReader/IRReader.cpp](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/IRReader/IRReader.cpp) | IRReader | **统一门面**：靠魔数分发到 AsmParser 或 BitcodeReader |
| [lib/AsmParser/Parser.cpp](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/AsmParser/Parser.cpp) | AsmParser | 文本入口：创建 `LLParser` 并驱动解析 |
| [lib/AsmParser/LLParser.cpp](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/AsmParser/LLParser.cpp) | AsmParser | 文本解析主体：递归下降地解析 `.ll` 语法 |
| [lib/Bitcode/Writer/BitcodeWriter.cpp](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Bitcode/Writer/BitcodeWriter.cpp) | Bitcode Writer | 把 Module 序列化成位流块结构 |
| [lib/Bitcode/Reader/BitcodeReader.cpp](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Bitcode/Reader/BitcodeReader.cpp) | Bitcode Reader | 把位流反序列化回 Module，支持惰性加载 |
| [include/llvm/Bitcode/BitcodeReader.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/Bitcode/BitcodeReader.h) | Bitcode Reader | 公开 API 与魔数判定函数 |
| [include/llvm/IRReader/IRReader.h](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IRReader/IRReader.h) | IRReader | 门面函数声明 |
| [tools/llvm-as/llvm-as.cpp](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/llvm-as/llvm-as.cpp) | 工具 | `.ll → .bc`：AsmParser 读入 + BitcodeWriter 写出 |
| [tools/llvm-dis/llvm-dis.cpp](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/llvm-dis/llvm-dis.cpp) | 工具 | `.bc → .ll`：BitcodeReader 读入 + Module::print 写出 |

一张总图（数据流）：

```
                 读 .ll/.bc                      写 .ll/.bc
   文件 ──────────────► Module（内存 IR 树）──────────────► 文件
            ▲                       │                       ▲
            │                       │                       │
   ┌────────┴────────┐              │              ┌────────┴────────┐
   │    IRReader     │              │              │  (写出门面)     │
   │  (门面/分发)    │              │              │                 │
   └──┬──────────┬───┘              │              └──┬───────────┬──┘
      │文本      │位码              │                 │文本       │位码
  AsmParser   BitcodeReader     Module 操作       (M->print)  BitcodeWriter
  (Parser.cpp)(BitcodeReader.cpp)              (IR Assembly)  (BitcodeWriter.cpp)
      │          │
  LLParser    parseModule / parseFunctionBody
  递归下降    惰性解码 block
```

## 4. 核心概念与源码讲解

### 4.1 IRReader：统一读取入口（门面 + 分发）

#### 4.1.1 概念说明

很多调用方（`opt`、`lli`、C API、二次开发的程序）都需要「读一个 IR 文件」，但它们并不关心文件到底是 `.ll` 还是 `.bc`。`IRReader` 组件就是为消除这种烦扰而存在的**门面**：它提供 `parseIRFile`、`getLazyIRFileModule` 等少数几个函数，对外的语义统一是「给路径，还你 `Module`」。

门面内部的决策规则非常简单：**读文件头几个字节，看是不是位码魔数**。是 → 走 BitcodeReader；不是 → 当文本走 AsmParser。这样调用方完全不用感知格式差异。

#### 4.1.2 核心流程

`parseIRFile` 的执行过程（对应源码 `parseIRFile → parseIR`）：

1. 用 `MemoryBuffer::getFileOrSTDIN` 把文件内容整体读进内存（得到一段连续字节 + 文件名标识）。
2. 调 `parseIR`，进入分发判断。
3. `parseIR` 调 `isBitcode(...)`，检查缓冲区起始的魔数字节。
4. 分支：
   - **是位码** → 调 `parseBitcodeFile`（BitcodeReader，详见 4.3）。
   - **不是位码** → 调 `parseAssembly`（AsmParser，详见 4.2）。
5. 把得到的 `Module`（或错误信息 `SMDiagnostic`）返回。

注意 `parseIR` 与 `getLazyIRModule` 有一处重要区别：`getLazyIRModule` 在「是位码」时返回的是**惰性 Module**（函数体按需解码），而 `parseIR` 在「是位码」时走的是 `parseBitcodeFile`（一次性解码完）。但两者在「是文本」时都必须把文本完整解析——因为文本格式无法跳读。

#### 4.1.3 源码精读

先看门面的对外声明，体会「自动探测格式」的设计意图：

[include/llvm/IRReader/IRReader.h:L9-L12](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/IRReader/IRReader.h#L9-L12) —— 头文件注释直接点明：「支持 Bitcode 与 Assembly，自动探测输入格式」。

核心分发逻辑在 `getLazyIRModule` 里最清楚（位码分支 → BitcodeReader，否则 → AsmParser）：

[lib/IRReader/IRReader.cpp:L35-L49](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/IRReader/IRReader.cpp#L35-L49)

```cpp
if (isBitcode((const unsigned char *)Buffer->getBufferStart(),
              (const unsigned char *)Buffer->getBufferEnd())) {
  Expected<std::unique_ptr<Module>> ModuleOrErr = getOwningLazyBitcodeModule(
      std::move(Buffer), Context, ShouldLazyLoadMetadata);
  // ... 错误处理 ...
  return std::move(ModuleOrErr.get());
}
return parseAssembly(Buffer->getMemBufferRef(), Err, Context);
```

这段就是整条链路的「十字路口」：`isBitcode` 看前几个字节决定左转还是右转。位码出错时，它会把底层 `Error` 转成统一的 `SMDiagnostic`，这样无论走哪条路，错误对外都是同一种格式。

再看急切版入口 `parseIR`（`opt` 默认走这种），分支判断完全对称：

[lib/IRReader/IRReader.cpp:L75-L92](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/IRReader/IRReader.cpp#L75-L92)

```cpp
if (isBitcode(/*...*/ Buffer.getBufferStart(), Buffer.getBufferEnd())) {
  Expected<std::unique_ptr<Module>> ModuleOrErr =
      parseBitcodeFile(Buffer, Context, Callbacks);
  // ... 错误处理 ...
}
return parseAssembly(Buffer, Err, Context, nullptr,
                     Callbacks.DataLayout.value_or(...), ParserContext);
```

> 小贴士：`parseIR` 多了一个 `ParserCallbacks` 参数，其中的 `DataLayout` 回调允许调用方**覆盖**文件里写的 data layout（u5 会讲 data layout）。这是工具链里很有用的钩子，比如 LTO 时按目标重新指定布局。

文件读取发生在 `parseIRFile`：

[lib/IRReader/IRReader.cpp:L99-L108](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/IRReader/IRReader.cpp#L99-L108) —— 用 `getFileOrSTDIN(..., /*IsText=*/true)` 读文件，再交给 `parseIR`。打开失败时把系统错误码包装成 `SMDiagnostic` 返回 `nullptr`。

#### 4.1.4 代码实践

**实践目标**：亲手验证 IRReader 的「格式分发」行为——同一个文件被当成文本或位码时，链路不同。

**操作步骤**（源码阅读型 + 工具验证）：

1. 准备一段最小 IR 文本，存为 `add.ll`：

   ```llvm
   define i32 @add(i32 %a, i32 %b) {
     %r = add i32 %a, %b
     ret i32 %r
   }
   ```

2. 在本地用 `llvm-as add.ll -o add.bc` 生成位码（这一步内部就是 4.1.1 说的「AsmParser 读 + BitcodeWriter 写」）。

3. 用十六进制工具查看两个文件开头：

   ```bash
   head -c 8 add.ll | xxd
   head -c 8 add.bc | xxd
   ```

**需要观察的现象 / 预期结果**（待本地验证具体字节，但规律是确定的）：

- `add.ll` 开头是 ASCII 字母 `d`、`e`、`f`...（即 `define` 的前几个字母）。
- `add.bc` 开头四个字节应是 `42 43 c0 de`（即 `'B','C',0xc0,0xde`）。这正是 `isBitcode` 判定的「原始位码」魔数（见 4.3.3）。

4. 对照源码确认：`IRReader.cpp` 第 35 行的 `isBitcode(...)` 读的正是这两个文件的前几个字节。如果你把 `add.bc` 改名成 `add.ll`，IRReader 依然会正确地把它当位码读——**因为判定看的是内容而非后缀**。

#### 4.1.5 小练习与答案

**练习 1**：`getLazyIRModule` 在「输入是文本」时，能否返回惰性 Module？为什么？

> **参考答案**：不能。文本格式没有可跳读的结构化索引，必须从头到尾完整解析才能重建 IR 树。所以 `getLazyIRModule` 在文本分支直接调 `parseAssembly` 一次性解析完。惰性加载是位码格式（有 block 偏移记录）独享的能力。

**练习 2**：为什么 IRReader 在位码出错时要把底层 `Error` 转成 `SMDiagnostic`？

> **参考答案**：为了对调用方屏蔽「格式差异」。无论走 AsmParser 还是 BitcodeReader，调用方都只面对一种错误类型 `SMDiagnostic`（它还能附带源码位置、友好打印），这样上层代码（如 `opt`）的错误处理逻辑只有一套。

### 4.2 AsmParser：文本格式（.ll）的解析

#### 4.2.1 概念说明

文本格式 `.ll` 用一套类似汇编的语法来表达 IR（你已经在前面几讲见过 `define`、`add`、`ret`、`@函数名`、`%局部名` 等）。AsmParser 组件负责把这段文本翻译回内存里的 IR 树。它本质上是一个**递归下降（recursive descent）解析器**：先做词法分析（lexer）把字符切成 token，再按语法规则一条条匹配、构造出 `Function`/`BasicBlock`/`Instruction`。

AsmParser 由两层文件构成：

- `Parser.cpp`：对外入口（`parseAssembly` / `parseAssemblyFile` / `parseAssemblyString`），负责建 `Module`、建 `LLParser` 并驱动。
- `LLParser.cpp`（及其头 `LLParser.h`）：真正的解析逻辑主体。

#### 4.2.2 核心流程

文本解析的主流程（`parseAssembly → parseAssemblyInto → LLParser::Run`）：

1. `parseAssembly` 先 `make_unique<Module>` 建一个空 Module（用文件名作为 module 标识）。
2. `parseAssemblyInto` 把文本包成 `MemoryBuffer`，挂到一个 `SourceMgr`（源码管理器，负责定位行列号、生成错误信息），然后 `new LLParser(...)` 并调它的 `.Run()`。
3. `LLParser::Run`：先用 `Lex.Lex()` 推动词法器读第一个 token；再 `parseTargetDefinitions`（解析 `target datalayout` / `target triple`）；再 `parseTopLevelEntities`（解析所有顶层实体）；最后 `validateEndOfModule` 做整体验证。
4. `parseTopLevelEntities` 是一个循环 + `switch`，根据当前 token 的关键字分派：遇到 `define` → `parseDefine`（函数定义）；遇到 `declare` → `parseDeclare`（函数声明）；遇到 `@全局` → 解析全局变量；等等。

#### 4.2.3 源码精读

入口三件套在 `Parser.cpp`。先看 `parseAssemblyInto` 如何「建好环境再调 Run」：

[lib/AsmParser/Parser.cpp:L29-L38](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/AsmParser/Parser.cpp#L29-L38)

```cpp
SourceMgr SM;
std::unique_ptr<MemoryBuffer> Buf = MemoryBuffer::getMemBuffer(F);
SM.AddNewSourceBuffer(std::move(Buf), SMLoc());
// ...
return LLParser(F.getBuffer(), SM, Err, M, Index,
                M ? M->getContext() : OptContext.emplace(), Slots,
                ParserContext)
    .Run(UpgradeDebugInfo, DataLayoutCallback);
```

注意 `LLParser` 的构造参数把「源码、错误输出、目标 Module、Context」全部串起来——解析器直接把构造出来的 IR 节点挂进传入的 `Module`。`SourceMgr` 是关键：它让解析器在任何出错位置都能生成带行列号的 `SMDiagnostic`。

`parseAssembly` 负责先建空 Module，再解析进去：

[lib/AsmParser/Parser.cpp:L50-L62](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/AsmParser/Parser.cpp#L50-L62) —— `make_unique<Module>(F.getBufferIdentifier(), Context)` 用「文件名」作为 Module 名，然后 `parseAssemblyInto` 把文本填进去；失败返回 `nullptr`。

真正驱动解析的 `LLParser::Run` 在主体文件里：

[lib/AsmParser/LLParser.cpp:L76-L93](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/AsmParser/LLParser.cpp#L76-L93)

```cpp
bool LLParser::Run(bool UpgradeDebugInfo, DataLayoutCallbackTy DataLayoutCallback) {
  Lex.Lex();                                  // 推进词法器，读首个 token
  if (Context.shouldDiscardValueNames())
    return error(Lex.getLoc(),
        "Can't read textual IR with a Context that discards named Values");
  if (M) {
    if (parseTargetDefinitions(DataLayoutCallback))   // target datalayout / triple
      return true;
  }
  return parseTopLevelEntities() || validateEndOfModule(UpgradeDebugInfo) ||
         validateEndOfIndex();
}
```

注意那个 `shouldDiscardValueNames()` 检查：如果 Context 被设成「丢弃值名字」模式，文本 IR 就**没法解析**——因为 `.ll` 里函数名、变量名（`@main`、`%r`）是解析时定位值的关键，丢了名字就建不出 def-use 关系。位码则不受影响，因为位码内部用整数 ID 而非名字来引用值。

`parseTopLevelEntities` 是顶层分派的核心循环：

[lib/AsmParser/LLParser.cpp:L584-L617](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/AsmParser/LLParser.cpp#L584-L617)

```cpp
while (true) {
  switch (Lex.getKind()) {
  default:
    return tokError("expected top-level entity");
  case lltok::Eof: return false;
  case lltok::kw_declare:
    if (parseDeclare()) return true;       // 函数声明
    break;
  case lltok::kw_define:
    if (parseDefine()) return true;        // 函数定义
    break;
  case lltok::kw_module:
    if (parseModuleAsm()) return true;     // module asm
    break;
  // ...
  case lltok::GlobalVar:
    if (parseNamedGlobal()) return true;   // 全局变量 @g = ...
    break;
  // ...
  }
}
```

这就是「递归下降」的样子：看当前 token 是什么关键字，调对应的解析子程序。`parseDefine` 会进一步解析函数签名、基本块、块内指令……一层层把整棵 IR 树建出来。

#### 4.2.4 代码实践

**实践目标**：跟踪 `llvm-as` 工具如何用 AsmParser 把 `.ll` 变成 Module，理解「薄壳」调用。

**操作步骤**（源码阅读型）：

1. 打开 [tools/llvm-as/llvm-as.cpp:L123-L135](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/llvm-as/llvm-as.cpp#L123-L135)。这里 `main` 调 `parseAssemblyFileWithIndex(InputFilename, Err, Context, ...)`——这就是 4.2.1 说的 AsmParser 入口。

2. 紧接着第 139-148 行：除非 `--disable-verify`，否则调 `verifyModule(*M, &OS)` 校验。`verifyModule`（u1-l3、u2-l3 提过）确保 SSA、类型、终结指令等结构性约束都满足。

3. 第 157-158 行：调 `WriteOutputFile`，里面（第 98 行）调 `WriteBitcodeToFile`——这就把 AsmParser 读出的 Module 交给 BitcodeWriter（见 4.3）写盘。

**需要观察的现象 / 预期结果**：

- 你会确认 `llvm-as` 的源码里**完全没有**手写的「逐字符解析逻辑」——它只是「调 AsmParser 读 → 校验 → 调 BitcodeWriter 写」。这印证了 u1-l3 的结论：工具是薄壳，逻辑在库里。
- 数据流方向：`.ll` 文本 →(AsmParser)→ 内存 Module →(BitcodeWriter)→ `.bc`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `LLParser::Run` 一开始就要检查 `shouldDiscardValueNames()`？把位码读取也加上这个限制合理吗？

> **参考答案**：因为文本 IR 用**名字**（`@main`、`%r`）引用和定义值，解析时必须保留这些名字才能建立 def-use 关系；若 Context 丢弃名字，解析根本无法进行。位码内部用**整数 ID**引用值，名字是可选的附加信息，所以位码读取**不需要**这个限制——这也解释了为什么「丢弃名字」的 Context（常用于只关心形态、省内存的场景）能读位码却不能读文本。

**练习 2**：`parseTopLevelEntities` 遇到一个它不认识的关键字（`default` 分支）会怎样？

> **参考答案**：调用 `tokError("expected top-level entity")` 报错并返回 `true`（表示解析失败），错误经 `SMDiagnostic` 上报，最终 `parseAssembly` 返回 `nullptr`。这说明文本语法是严格的——顶层只允许 `define`/`declare`/全局变量/类型等若干种实体。

### 4.3 Bitcode：紧凑二进制格式（.bc）的读写

#### 4.3.1 概念说明

位码（Bitcode）是把同一棵 IR 树**按位（bit）打包**后的二进制形态。相比文本，它有三个本质优势：

1. **紧凑**：用位级编码和「缩写」压缩，体积远小于文本；小整数只占几个比特。
2. **快**：解析时无需做字符串→token→语法树的繁重工作，直接按固定格式读比特。
3. **可惰性加载**：内部是结构化的 block，记录了每个函数体在文件中的偏移，可以「先读目录、用到再读正文」。

位码的读和写是**对称的两条独立链路**：写（`BitcodeWriter`）遍历 Module，把每个值、类型、指令序列化进 Bitstream 的 block/record；读（`BitcodeReader`）按 Bitstream 规则逆操作，重建 Module。`llvm-as` 用的是「AsmParser 读 + BitcodeWriter 写」，`llvm-dis` 用的是「BitcodeReader 读 + Module::print 写」。

#### 4.3.2 核心流程

**写入流程**（`WriteBitcodeToFile → writeModule → ModuleBitcodeWriter::write`）：

1. `WriteBitcodeToFile` 判断目标平台：Darwin/Mach-O 需要在外面包一层 wrapper 头并先缓冲；否则直接写到输出流。
2. 实际写入委托给 `writeModule`：它把 Module 登记到内部列表，创建 `ModuleBitcodeWriter` 并调其 `write()`。
3. `ModuleBitcodeWriter::write()` 按固定顺序发射 Bitstream block：
   - 先写 `IDENTIFICATION_BLOCK`（生产者字符串如 `"LLVM19.x.x"` + epoch 版本号）。
   - 进入 `MODULE_BLOCK`，依次写：模块版本号、blockinfo（缩写定义）、类型表、属性组表、属性表、comdat、模块信息（target triple、全局变量、函数原型）、常量、元数据、各函数体（`FUNCTION_BLOCK`）、全局值符号表。
4. 最后 `writeSymtab` / `writeStrtab` 写符号表与字符串表，便于链接器快速查符号。

**读取流程**（`parseBitcodeFile → BitcodeModule::parseModule` / 惰性版 `getLazyBitcodeModule`）：

1. `parseBitcodeFile`（急切）调 `getSingleModule` 定位到文件里的单个 `BitcodeModule`，再 `parseModule` 一次性解码全部。
2. 惰性版 `getLazyBitcodeModule` 返回一个只读完了「目录」的 Module：函数体尚未解码。
3. 解码主体在 `BitcodeReader::parseModule`：进入 `MODULE_BLOCK`，循环 `Stream.advance()` 读每一条 record/subblock，按类型分发——遇到函数定义时不立即读函数体，而是记下偏移；当后续有人访问该函数时，才调 `parseFunctionBody` 回到对应偏移解码指令。
4. `parseFunctionBody` 进入 `FUNCTION_BLOCK`，逐条把指令 record 还原成 `MachineInstr` 链……不对——还原成 IR `Instruction`（这是 IR 层不是后端 MIR，u5 才讲 MachineInstr）。

**魔数（magic bytes）**：位码文件开头两套魔数都定义在头文件里，是 IRReader 分发的依据：

- 原始位码：`'B','C',0xc0,0xde`。
- 包装位码（wrapper，给系统加 padding 用）：`0xDE,0xC0,0x17,0x0B`。

#### 4.3.3 一个数学细节：VBR 变长整数编码

Bitstream 大量使用 **VBR（Variable Bit Rate）** 编码来压缩整数。设块宽为 \(w\) 位（如 `VBR,6` 即 \(w=6\)），每个块的**最高位是「续位」**：1 表示后面还有块，0 表示结束；其余 \(w-1\) 位承载数据。若一个数被编成 \(k\) 个块，第 \(i\) 块的数据部分为 \(c_i\)（低 \(w-1\) 位），则还原值为：

\[
v = \sum_{i=0}^{k-1} c_i \cdot 2^{i(w-1)}
\]

例如 \(w=6\) 时每块带 5 个数据位：0~31 的数只需 1 个块；32 及以上才需多个块。这让「小整数占极少比特、大整数按需扩展」，是位码紧凑的根本原因之一。你在 `writeIdentificationBlock` 里能直接看到 `BitCodeAbbrevOp(BitCodeAbbrevOp::VBR, 6)` 这样的用法。

#### 4.3.4 源码精读

**先看魔数判定**（IRReader 分发的根基），全部定义在头文件里：

[include/llvm/Bitcode/BitcodeReader.h:L259-L289](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/include/llvm/Bitcode/BitcodeReader.h#L259-L289)

```cpp
inline bool isBitcodeWrapper(const unsigned char *BufPtr, const unsigned char *BufEnd) {
  return BufPtr != BufEnd &&
         BufPtr[0] == 0xDE && BufPtr[1] == 0xC0 &&
         BufPtr[2] == 0x17 && BufPtr[3] == 0x0B;
}
inline bool isRawBitcode(const unsigned char *BufPtr, const unsigned char *BufEnd) {
  return BufPtr != BufEnd &&
         BufPtr[0] == 'B' && BufPtr[1] == 'C' &&
         BufPtr[2] == 0xc0 && BufPtr[3] == 0xde;
}
inline bool isBitcode(const unsigned char *BufPtr, const unsigned char *BufEnd) {
  return isBitcodeWrapper(BufPtr, BufEnd) || isRawBitcode(BufPtr, BufEnd);
}
```

`isBitcode` 就是 4.1 里分发判断调的函数。注意源码注释里的「彩蛋」：作者在注释里暗示魔数里藏了小信息——`0x0B17C0DE` 读起来像 "obfuscated code"。

**看写入顶层** `WriteBitcodeToFile`：

[lib/Bitcode/Writer/BitcodeWriter.cpp:L5725-L5752](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Bitcode/Writer/BitcodeWriter.cpp#L5725-L5752)

```cpp
auto Write = [&](BitcodeWriter &Writer) {
  Writer.writeModule(M, ShouldPreserveUseListOrder, Index, GenerateHash, ModHash);
  Writer.writeSymtab();
  Writer.writeStrtab();
};
Triple TT(M.getTargetTriple());
if (TT.isOSDarwin() || TT.isOSBinFormatMachO()) {
  // Darwin/Mach-O: 先写缓冲，再补 wrapper 头，最后整体输出
  // ...
} else {
  BitcodeWriter Writer(Out);
  Write(Writer);
}
```

这里揭示一个平台细节：Darwin 把位码当作一种特殊的「mach-o section」嵌入，需要前后加 wrapper 头（注意它与上面「wrapper 魔数」呼应——`llvm-dis` 读时若发现 wrapper 魔数，会先 `SkipBitcodeWrapperHeader` 跳到真正的 `BC` 起点）。

**看模块写入顺序** `ModuleBitcodeWriter::write`，这是理解位码内部结构的关键：

[lib/Bitcode/Writer/BitcodeWriter.cpp:L5482-L5530](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Bitcode/Writer/BitcodeWriter.cpp#L5482-L5530)

```cpp
void ModuleBitcodeWriter::write() {
  writeIdentificationBlock(Stream);          // ① 标识：生产者 + epoch
  Stream.EnterSubblock(bitc::MODULE_BLOCK_ID, 3);   // ② 进入模块块
  // ...
  writeModuleVersion();                      // ③ 版本号 = 2
  writeBlockInfo();                          // ④ 缩写定义
  writeTypeTable();                          // ⑤ 类型表
  writeAttributeGroupTable();                // ⑥ 属性组
  writeAttributeTable();                     // ⑦ 属性
  writeComdats();                            // ⑧ comdat
  writeModuleInfo();                         // ⑨ triple/inline asm/全局/函数原型
  writeModuleConstants();                    // ⑩ 常量
  writeModuleMetadataKinds();                // ⑪ 元数据类型名
  writeModuleMetadata();                     // ⑫ 元数据
  // ...
  for (const Function &F : M)
    if (!F.isDeclaration())
      writeFunction(F, FunctionToBitcodeIndex);  // ⑬ 每个函数体独立成 FUNCTION_BLOCK
  // ...
}
```

注意第 ⑬ 步：**每个非声明函数都被单独写进一个 `FUNCTION_BLOCK`**，并记录它在文件中的偏移（`FunctionToBitcodeIndex`）。正是这个偏移让读侧能够「按需跳过去只解码某一个函数」——这就是惰性加载的物理基础。

模块版本号固定为 2：

[lib/Bitcode/Writer/BitcodeWriter.cpp:L183-L186](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Bitcode/Writer/BitcodeWriter.cpp#L183-L186) —— `EmitRecord(bitc::MODULE_CODE_VERSION, {2})`。版本号让读侧能兼容旧格式。

标识块里写了生产者字符串和 epoch，用到了前面讲的 VBR 缩写：

[lib/Bitcode/Writer/BitcodeWriter.cpp:L5439-L5459](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Bitcode/Writer/BitcodeWriter.cpp#L5439-L5459) —— 用 `Char6` 缩写（6 位字符编码）写 `"LLVM" LLVM_VERSION_STRING`，用 `VBR,6` 写 epoch 版本号。

**看急切读取** `parseBitcodeFile` 与惰性读取 `getLazyBitcodeModule`（二者对称、都很短）：

[lib/Bitcode/Reader/BitcodeReader.cpp:L8974-L9010](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Bitcode/Reader/BitcodeReader.cpp#L8974-L9010)

```cpp
llvm::getLazyBitcodeModule(MemoryBufferRef Buffer, LLVMContext &Context,
                           bool ShouldLazyLoadMetadata, bool IsImporting,
                           ParserCallbacks Callbacks) {
  Expected<BitcodeModule> BM = getSingleModule(Buffer);
  if (!BM) return BM.takeError();
  return BM->getLazyModule(Context, ShouldLazyLoadMetadata, IsImporting, Callbacks);
}
// ...
llvm::parseBitcodeFile(MemoryBufferRef Buffer, LLVMContext &Context,
                       ParserCallbacks Callbacks) {
  Expected<BitcodeModule> BM = getSingleModule(Buffer);
  if (!BM) return BM.takeError();
  return BM->parseModule(Context, Callbacks);   // 急切：一次解完
}
```

`getOwningLazyBitcodeModule` 紧随其后（第 8985-8993 行），区别仅在于它让 Module **接管 MemoryBuffer 的所有权**——这样惰性加载时原始字节一直存活，随时能回去解码函数体。若不接管，buffer 可能在函数体解码前就被释放，导致崩溃。

**看惰性读取的主体** `BitcodeReader::parseModule`（关注它如何对待函数体）：

[lib/Bitcode/Reader/BitcodeReader.cpp:L4613-L4683](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Bitcode/Reader/BitcodeReader.cpp#L4613-L4683)

```cpp
Error BitcodeReader::parseModule(uint64_t ResumeBit, ...) {
  // ...
  } else if (Error Err = Stream.EnterSubBlock(bitc::MODULE_BLOCK_ID))
    return Err;
  // ...
  while (true) {
    Expected<llvm::BitstreamEntry> MaybeEntry = Stream.advance();
    // ...
    switch (Entry.Kind) {
    case BitstreamEntry::SubBlock:
      switch (Entry.ID) {
      default:  // 跳过未知内容
        if (Error Err = Stream.SkipBlock()) ...
```

关键是：当 `parseModule` 遇到函数体的 `FUNCTION_BLOCK` 子块时，在惰性模式下它**记录偏移并 `SkipBlock` 跳过**，而不是立即解码。等运行时有人访问该函数（触发 `materialize`），才调 `parseFunctionBody`：

[lib/Bitcode/Reader/BitcodeReader.cpp:L5059-L5081](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/lib/Bitcode/Reader/BitcodeReader.cpp#L5059-L5081)

```cpp
Error BitcodeReader::parseFunctionBody(Function *F) {
  if (Error Err = Stream.EnterSubBlock(bitc::FUNCTION_BLOCK_ID))
    return Err;
  // ... 把参数加入值表 ...
  // Read all the records.
  while (true) {
    // 逐条 record 还原为 Instruction，挂进 BasicBlock
  }
}
```

至此「写时每函数独立成块、读时按偏移按需解码」的闭环就清晰了。

#### 4.3.5 代码实践

**实践目标**：跟踪 `llvm-dis` 如何用 BitcodeReader 把 `.bc` 还原成 `.ll`，并验证「读位码不依赖文件后缀」。

**操作步骤**（源码阅读 + 工具验证）：

1. 阅读 [tools/llvm-dis/llvm-dis.cpp:L196-L223](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/tools/llvm-dis/llvm-dis.cpp#L196-L223)。`main` 的流程是：
   - `MemoryBuffer::getFileOrSTDIN` 读文件（**完全不看后缀**）。
   - `getBitcodeFileContents(*MB)` 解析位码容器，得到其中的模块列表 `IF.Mods`。
   - 对每个 `BitcodeModule MB`：`MB.getLazyModule(...)` 得到惰性 Module，再 `M->materializeAll()` 把全部函数体解码出来。
2. 继续看第 264-271 行：`M->print(Out->os(), Annotator.get(), ...)`——**写文本用的不是独立的「Writer」，而是 `Module::print`**，它直接把内存 IR 树打印成 `.ll` 文本（u2-l1 讲过 IR 树的遍历，这里就是把树反向打印）。
3. 工具验证（待本地验证输出）：在本地执行

   ```bash
   cp add.bc add.bin        # 故意改成奇怪后缀
   llvm-dis add.bin -o add2.ll
   diff add.ll add2.ll      # 应当无差异
   ```

**需要观察的现象 / 预期结果**：即使后缀是 `.bin`，`llvm-dis` 仍能正确读出并生成与原 `add.ll` 内容等价的 `add2.ll`——因为它靠的是文件头魔数 `BCẞÞ`，而非后缀。

#### 4.3.6 小练习与答案

**练习 1**：为什么 `getOwningLazyBitcodeModule` 在成功时要把 `MemoryBuffer` 的所有权转交给 Module？若不转交会发生什么？

> **参考答案**：惰性 Module 在创建时只读了「目录」，函数体尚未解码；之后访问函数时会回到原始 buffer 的对应偏移去 `parseFunctionBody`。因此**原始字节必须一直存活到所有函数都被物化为止**。把 buffer 所有权交给 Module，正好保证它的生命周期与 Module 一致；若不转交，调用方可能提前释放 buffer，导致惰性解码时读到已释放内存。

**练习 2**：`parseBitcodeFile` 和 `getLazyBitcodeModule` 最终都调到 `BitcodeModule` 的方法，二者差别在哪？什么场景该用哪个？

> **参考答案**：`parseBitcodeFile` 走 `BM->parseModule`，**一次性把所有函数体都解码**（急切），适合需要立刻处理整个程序的场景（如 `opt` 默认行为）。`getLazyBitcodeModule` 走 `BM->getLazyModule`，**只读目录、函数体按需解码**，适合只关心部分函数、或想缩短启动时间的场景（如 JIT、ThinLTO 导入）。

## 5. 综合实践

把本讲三条链路串起来，完成一个「最小 IR 阅读器」小任务。

**任务**：写一段 C++，用 IRReader 的统一入口 `parseIRFile` 读入一个 IR 文件（无论 `.ll` 还是 `.bc`），打印出它的目标三元组（target triple）和每个函数的名字、基本块数、指令数。

**操作步骤**：

1. 创建 `irinfo.cpp`（这是**示例代码**，不在 LLVM 仓库中）：

   ```cpp
   // 示例代码：用 IRReader 统一入口读取任意 IR 文件
   #include "llvm/IRReader/IRReader.h"
   #include "llvm/IR/Module.h"
   #include "llvm/IR/Function.h"
   #include "llvm/IR/BasicBlock.h"
   #include "llvm/IR/Instructions.h"
   #include "llvm/Support/raw_ostream.h"
   #include "llvm/Support/SourceMgr.h"
   using namespace llvm;

   int main(int argc, char **argv) {
     if (argc < 2) { errs() << "usage: irinfo <file.ll|file.bc>\n"; return 1; }
     LLVMContext Context;
     SMDiagnostic Err;
     // 关键：parseIRFile 自动按魔数分发到 AsmParser 或 BitcodeReader
     std::unique_ptr<Module> M = parseIRFile(argv[1], Err, Context);
     if (!M) { Err.print(argv[0], errs()); return 1; }

     outs() << "target triple: "
            << (M->getTargetTriple().empty() ? "(none)" : M->getTargetTriple())
            << "\n";
     for (Function &F : *M) {
       unsigned BB = 0, INS = 0;
       for (BasicBlock &B : F) { ++BB; INS += B.size(); }
       outs() << "function " << F.getName() << " : " << BB
              << " basic blocks, " << INS << " instructions\n";
     }
     return 0;
   }
   ```

2. 参考 `examples/` 下的 CMakeLists（u1-l4 讲过 `add_llvm_example` 与 `LLVM_LINK_COMPONENTS`）写一个最小构建脚本，链接组件至少包含 `core`、`irreader`、`support`、`asmparser`、`bitreader`、`bitwriter`。

3. 准备两个输入：`add.ll`（4.1.4 的那段）和 `add.bc`（`llvm-as add.ll -o add.bc` 生成）。

4. 分别运行 `irinfo add.ll` 与 `irinfo add.bc`。

**需要观察的现象 / 预期结果**（待本地验证）：

- 两次运行输出**完全相同**——因为 `parseIRFile` 把格式差异藏在了门面背后，调用方拿到的是同一棵 IR 树。
- 对 `add` 函数应输出类似 `1 basic blocks, 2 instructions`（一个 `add` + 一个 `ret`）。
- 若把 `add.ll` 故意写错语法（如把 `define` 写成 `defin`），`parseIRFile` 返回 `nullptr`，`Err.print` 会打印带行列号的错误——这正是 4.1.5 练习 2 说的「统一错误格式」。

**进阶**（可选）：把程序里的 `parseIRFile` 换成 `getLazyIRFileModule`，并在访问函数前后观察哪些函数被物化（可在调试器里看 `Function` 是否已有函数体）。这能直观感受「惰性加载」。

## 6. 本讲小结

- LLVM IR 在磁盘上有两种等价格式：人类可读的文本 `.ll`（AsmParser 负责读写）与紧凑二进制的位码 `.bc`（BitcodeReader/Writer 负责）。
- `IRReader`（`lib/IRReader/IRReader.cpp`）是统一读取门面，靠文件头魔数（`isBitcode`）自动分发到 AsmParser 或 BitcodeReader，并对调用方屏蔽格式与错误类型差异。
- AsmParser 走「词法 + 递归下降」：`Parser.cpp` 建环境、`LLParser::Run` 驱动，`parseTopLevelEntities` 按 `define`/`declare`/全局变量等关键字分派；文本格式无法惰性加载，且依赖值名字。
- Bitcode 用 Bitstream 容器，按 `IDENTIFICATION_BLOCK` + `MODULE_BLOCK`（内含类型表/常量/各函数的 `FUNCTION_BLOCK` 等）组织；VBR 变长编码让它紧凑。
- 惰性加载的物理基础是「每个函数体独立成块并记录偏移」：写侧 `writeFunction` 记偏移，读侧 `parseModule` 跳过、`parseFunctionBody` 按需解码。
- 工具印证：`llvm-as` = AsmParser 读 + BitcodeWriter 写；`llvm-dis` = BitcodeReader 读 + `Module::print` 写；二者都是薄壳。

## 7. 下一步学习建议

- **进入 Pass 流水线（u3）**：本讲让你理解了「读 IR → Module → 写 IR」的完整闭环，这正是 `opt` 的工作方式。u3-l1 讲新 Pass 管理器时，你会看到 `opt` 读入 Module、把 Module 喂给 `PassManager`、跑完再写出去的完整骨架，本讲是其前置。
- **深读建议**：如果想更懂位码格式，可读 `include/llvm/Bitstream/` 下的头文件（`BitstreamReader.h`、`BitCodes.h`）理解 block/record 的底层编码；并对照 `lib/Bitcode/Writer/BitcodeWriter.cpp` 中 `writeTypeTable`、`writeFunction` 等函数看具体记录长什么样。
- **动手延伸**：试着给第 5 节的 `irinfo` 增加「检测输入是文本还是位码」的功能（直接调 `isBitcode`），并打印所用链路，强化对分发的理解。
