# IR 的文本与二进制表示：AsmParser 与 Bitcode

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 LLVM IR 为什么有 **两种持久化形式**——人类可读的 `.ll` 文本与紧凑的 `.bc` 位码（bitcode），以及它们各自适用的场景。
- 理解 **文本 IR 的「读」与「写」是两个对称的库**：`AsmParser`（`LLLexer` + `LLParser`）把 `.ll` 解析成内存 `Module`，`AsmWriter`（`AssemblyWriter`）把 `Module` 打印回 `.ll`。
- 看懂 **位码的物理格式**：以「块（block）+ 记录（record）」组织的二进制 bitstream、文件头的魔数（magic bytes）、以及让位码紧凑的「缩写（abbreviation）」机制。
- 说出 `BitcodeWriter` 写出一个 `.bc` 时依次写出哪些块，`BitcodeReader` 读回时如何支持 **惰性物化（lazy materialization）**，以及为什么这正是链接时优化（LTO/ThinLTO）和 IR 分发的基础。
- 用 `llvm-as` / `llvm-dis` 完成一次「`.ll` ↔ `.bc`」无损往返，并用 `hexdump` / `llvm-bcanalyzer` 直接观察位码的字节与块结构。

## 2. 前置知识

本讲建立在前几讲的认知之上，复用以下概念（不再重复展开）：

- **`.ll` 文本 IR 的语法**（u2-l2）：模块头、全局声明、函数（签名 + 基本块 + 指令）、`@`/`%` 符号约定、终结指令、SSA 与 `phi`。本讲要回答「这些文本是谁写出来、又是谁读回去的」。
- **内存 `Module` 对象模型**（u3-l1）：`Module ⊃ Function ⊃ BasicBlock ⊃ Instruction`。本讲讲的两种形式，本质上都是这棵「内存对象树」的**序列化**与**反序列化**。
- **Type 唯一化与 Constant**（u3-l3）：类型在 `LLVMContext` 内唯一化、`Constant` 本身就是 `Value`。这些决定了位码里「类型表」「常量表」如何组织。
- **IR 的三种形态**（u1-l4）：内存 `Module`、`.ll` 文本、`.bc` 位码，三者以 `Module` 为中介无损互转。本讲就是把这「中介两侧的转换器」拆开讲。

一个核心直觉先放在这里：**文本是为「人」服务的，位码是为「机器与流水线」服务的。** 两者序列化的是同一棵 `Module`，所以同一个程序用哪种形式保存，语义完全等价，差别只在可读性、体积和读写速度。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [llvm/lib/AsmParser/Parser.cpp](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/AsmParser/Parser.cpp) | 文本 IR 解析的**公共入口层**：`parseAssembly` / `parseAssemblyFile` / `parseAssemblyString` 等函数把文本包进 `MemoryBuffer`，再委托给真正的 `LLParser`。 |
| [llvm/include/llvm/AsmParser/Parser.h](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/AsmParser/Parser.h) | 上述入口函数的公共声明；注释明确「解析后不会自动校验，需自己跑 verifier」。 |
| [llvm/lib/AsmParser/LLLexer.cpp](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/AsmParser/LLLexer.cpp) | `.ll` 的**词法分析器**：`LexToken()` 是一个大 `switch`，把字符流切成 Token；`LexIdentifier()` 负责识别关键字、整数类型（如 `i32`）和标签。 |
| [llvm/lib/IR/AsmWriter.cpp](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/AsmWriter.cpp) | 文本 IR 的**打印机**：`Module::print` 创建一个 `AssemblyWriter` 并调用 `printModule`，把内存 `Module` 反向输出成 `.ll`（与 `LLLexer`/`LLParser` 互为镜像）。 |
| [llvm/lib/Bitcode/Writer/BitcodeWriter.cpp](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Bitcode/Writer/BitcodeWriter.cpp) | 位码**写入器**：`writeBitcodeHeader` 写魔数，`ModuleBitcodeWriter::write()` 按固定顺序写出各块（类型表、常量表、模块信息、每个函数体……）。 |
| [llvm/lib/Bitcode/Reader/BitcodeReader.cpp](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Bitcode/Reader/BitcodeReader.cpp) | 位码**读取器**：`BitcodeReader` 类把 `.bc` 还原成 `Module`，并支持「先读骨架、用到再读函数体」的惰性物化。 |
| [llvm/include/llvm/Bitcode/LLVMBitCodes.h](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Bitcode/LLVMBitCodes.h) | 位码块 ID 枚举（`MODULE_BLOCK_ID`、`FUNCTION_BLOCK_ID`、`TYPE_BLOCK_ID_NEW` 等）与「纪元（epoch）」定义。 |
| [llvm/tools/llvm-as/llvm-as.cpp](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/tools/llvm-as/llvm-as.cpp) | `llvm-as` 工具：`.ll → .bc`，串联 `parseAssemblyFileWithIndex`（AsmParser）→ `verifyModule` → `WriteBitcodeToFile`（BitcodeWriter）。 |
| [llvm/tools/llvm-dis/llvm-dis.cpp](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/tools/llvm-dis/llvm-dis.cpp) | `llvm-dis` 工具：`.bc → .ll`，串联 `getBitcodeFileContents`（BitcodeReader）→ `materializeAll` → `Module::print`（AsmWriter）。 |

> 一个提前点破的全局结论：把 `llvm-as` 和 `llvm-dis` 的源码读一遍，你就能看到「`.ll → Module → .bc → Module → .ll`」这条完整的无损往返链。本讲四个最小模块，正是这条链上四个箭头背后的库。

---

## 4. 核心概念与源码讲解

### 4.1 IR 的两种持久化形式与无损往返

#### 4.1.1 概念说明

到目前为止，你接触的 IR 大多是 `.ll` 文本——它好读、好写、好放进 Git。但文本有两个先天不足：

- **体积大、解析慢**：文本是给人看的，字符冗余多（每个类型、每个操作数都要显式写出），`opt`/`llc` 每次启动都要重新词法分析 + 语法分析。
- **不利于流水线传递**：编译器流水线（前端 → 优化 → 后端 → 链接）在工具之间传递 IR 时，更想要一种紧凑、能随机访问、能「只读需要的部分」的格式。

于是 LLVM 给同一棵内存 `Module` 设计了两种序列化形式：

| 形式 | 扩展名 | 谁负责「读」（→ Module） | 谁负责「写」（Module →） | 主要用途 |
| --- | --- | --- | --- | --- |
| 文本汇编 | `.ll` | `AsmParser`（`LLLexer`+`LLParser`） | `AsmWriter`（`AssemblyWriter`） | 人读、测试、教学、放进版本库 |
| 位码 | `.bc` | `BitcodeReader` | `BitcodeWriter` | 工具间传递、LTO/ThinLTO、IR 分发、`lli` 执行 |

关键性质是**无损往返（lossless round-trip）**：理论上，任意一个 `Module` 都可以

\[
\text{Module} \xrightarrow{\text{AsmWriter}} .ll \xrightarrow{\text{AsmParser}} \text{Module}' \quad\text{且}\quad \text{Module}' \equiv \text{Module}
\]

位码侧同理。也就是说，文本和位码只是同一份信息的两种「编码」，`Module` 是它们共同的中介。这也是为什么 u2-l2 强调过：要理解 `.ll` 语法，最好同时对照写它的 `AsmWriter` 与读它的 `LLParser`——它们互为镜像，共同构成语法的权威定义。

#### 4.1.2 核心流程

四种转换器围绕 `Module` 形成一个环：

```
                    AsmWriter (打印)
            ┌──────────────────────────────┐
            ▼                               │
         .ll 文本 ──AsmParser(解析)──▶  Module  ──BitcodeWriter(写)──▶ .bc 位码
            ▲                               │                          │
            └──────── llvm-dis ─────────────┘◀── llvm-as ──────────────┘
                    AsmWriter                  BitcodeReader(读)
```

- **左半环（文本）**：`AsmParser` 把 `.ll` 变 `Module`，`AsmWriter` 把 `Module` 变 `.ll`。
- **右半环（位码）**：`BitcodeWriter` 把 `Module` 变 `.bc`，`BitcodeReader` 把 `.bc` 变 `Module`。
- **横跨两环的工具**：`llvm-as` 走「`AsmParser` → `BitcodeWriter`」（左到右），`llvm-dis` 走「`BitcodeReader` → `AsmWriter`」（右到左）。

> 这张环图是本讲的总纲。下面 4.2–4.5 依次拆开这四个箭头。

---

### 4.2 文本 IR 的解析：AsmParser（.ll → Module）

#### 4.2.1 概念说明

「解析一段 `.ll`」本质是经典的编译前端两步：先**词法分析（Lex）**把字符流切成 Token，再**语法分析（Parse）**按 `.ll` 文法把 Token 流组装成内存 `Module`。在 LLVM 里这分别由 `LLLexer` 和 `LLParser` 承担，二者都住在 `llvm/lib/AsmParser/`。

对外暴露的却不是这两个类，而是一组自由函数 `parseAssembly*`（声明在 `llvm/AsmParser/Parser.h`）。这层薄壳的意义是：调用方只需「给我一段文本/文件和一个 `LLVMContext`，我还你一个 `Module`」，不必关心 Lexer/Parser 的内部状态。

`Parser.h` 的注释特别强调一点：**解析器不会自动校验产物是否合法**——它只保证「按文法组装」，至于 SSA 是否完整、类型是否自洽，要靠调用方再跑一次 verifier（u3-l4 综合实践里用过的 `verifyFunction`/`verifyModule`）。

#### 4.2.2 核心流程

从「一个 `.ll` 文件」到「一个 `Module`」的完整路径：

```
parseAssemblyFile(filename)
  │  MemoryBuffer::getFileOrSTDIN  → 把文件读成内存缓冲
  ▼
parseAssembly(MemoryBufferRef)
  │  new Module(name, Context)     → 先建一个空 Module
  ▼
parseAssemblyInto(F, M, ...)
  │  把缓冲包进 SourceMgr（带行号/列号，便于报错定位）
  │  LLParser(F, SM, Err, M, Context).Run(...)
  ▼
LLParser::Run                       → 真正的语法分析
   ├─ 内部持有一个 LLLexer，按需 LexToken() 取下一个 Token
   ├─ 按 .ll 文法：模块头 → 全局 → 函数(签名/基本块/指令)
   └─ 边读边往 M 里 new 出 Function/BasicBlock/Instruction
```

注意 `parseAssemblyInto`（[Parser.cpp:24-38](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/AsmParser/Parser.cpp#L24-L38)）里这一句——它是整个 AsmParser 的「心脏」：

```cpp
return LLParser(F.getBuffer(), SM, Err, M, Index,
                M ? M->getContext() : OptContext.emplace(), Slots,
                ParserContext)
    .Run(UpgradeDebugInfo, DataLayoutCallback);
```

一个 `LLParser` 对象被构造出来、立即调用 `.Run()`，就把文本灌进了传入的 `Module *M`。`UpgradeDebugInfo` 参数还顺便做了一件事：把旧版本 IR 的调试信息「自动升级」到当前版本——这一点和位码侧的「纪元」机制（见 4.5）思路一致，都是为了向前兼容。

#### 4.2.3 源码精读

**入口层**。`parseAssemblyFile`（[Parser.cpp:64-77](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/AsmParser/Parser.cpp#L64-L77)）只做两件事：把文件读成 `MemoryBuffer`，再转调 `parseAssembly`：

```cpp
ErrorOr<std::unique_ptr<MemoryBuffer>> FileOrErr =
    MemoryBuffer::getFileOrSTDIN(Filename, /*IsText=*/true);
// ... 错误处理 ...
return parseAssembly(FileOrErr.get()->getMemBufferRef(), Err, Context, Slots);
```

`parseAssembly`（[Parser.cpp:50-62](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/AsmParser/Parser.cpp#L50-L62)）则先建一个空 `Module`，再调用 `parseAssemblyInto` 把内容填进去，失败返回 `nullptr`。这一层公共 API 的契约写在头文件里（[Parser.h:49-67](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/AsmParser/Parser.h#L49-L67)），注释点明「does not verify that the generated Module is valid, so you should run the verifier」。

**词法分析**。`LLLexer` 把字符切成 Token 的全部逻辑集中在 `LexToken()`（[LLLexer.cpp:193-263](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/AsmParser/LLLexer.cpp#L193-L263)），它是一个基于「当前首字符」的大 `switch`：

```cpp
switch (CurChar) {
  // 空白字符（空格/制表/换行/回车）直接跳过
  case ' ': case '\t': case '\n': case '\r': continue;
  case ';': SkipLineComment(); continue;     // 行注释 ;
  case '@': return LexAt();                   // 全局符号 @name / @0
  case '%': return LexPercent();              // 局部符号 %name / %0
  case '0'...'9': case '-': return LexDigitOrNegative(); // 数值
  default:
    if (isalpha(CurChar) || CurChar == '_') return LexIdentifier(); // 标识符/关键字
    return lltok::Error;
  // 单字符标点：= [ ] { } ( ) , * | : 等
}
```

读到不同首字符就派发给不同的 `LexXxx`。其中 `LexIdentifier()`（[LLLexer.cpp:498-557](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/AsmParser/LLLexer.cpp#L498-L557)）最值得看，它一身三任：

1. **识别整数类型**：看到 `i` 后面跟数字（如 `i32`），就在 `TyVal` 里构造好 `IntegerType` 并返回 `lltok::Type`——这就是为什么 `.ll` 里 `i32` 这种「类型字面量」能直接当 Token 用。
2. **识别标签**：遇到 `name:`，整体作为一个标签 Token（`lltok::LabelStr`）。
3. **识别关键字**：剩下的字母串拿去和关键字表比对。

关键字比对用的是一段宏（[LLLexer.cpp:549-557](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/AsmParser/LLLexer.cpp#L549-L557)），把字符串 `STR` 映射成 Token 种类 `kw_##STR`：

```cpp
#define KEYWORD(STR) do { if (Keyword == #STR) return lltok::kw_##STR; } while (false)
KEYWORD(true);    KEYWORD(false);
KEYWORD(declare); KEYWORD(define);
KEYWORD(global);  KEYWORD(constant);
```

所以 `define`、`declare`、`global` 这些 `.ll` 关键字，正是在这里被归类成各自的 `kw_*` Token，再交给 `LLParser` 决定语义。

每个 Token 都带一个「类型化取值」，存在 `LLLexer` 的成员里（[LLLexer.h:51-58](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/AsmParser/LLLexer.h#L51-L58)）：`StrVal`（字符串/名字）、`TyVal`（类型）、`UIntVal`/`APSIntVal`/`APFloatVal`（各种数值）。`LLParser` 取 Token 后，就通过这些 getter 拿到 Token 携带的值。

> 一个易被忽略的细节：词法器和语法器都能报错，且词法错误往往更精准（比如「非法字符」）。`LLLexer` 用 `ErrorPriority`（`Lexer > Parser > None`，见 [LLLexer.cpp:28-43](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/AsmParser/LLLexer.cpp#L28-L43)）保证：一旦记下了一条词法错误，后续更泛化的语法错误不会覆盖它。

#### 4.2.4 代码实践

**实践目标**：在源码里确认「`.ll` 的关键字与整数类型是在词法阶段被识别的」。

**操作步骤**：

1. 打开 [LLLexer.cpp:549-557](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/AsmParser/LLLexer.cpp#L549-L557) 的 `KEYWORD` 列表，找到 `define`、`declare`、`ret`、`add` 等关键字是如何被登记的。
2. 对照 [LLLexer.cpp:525-541](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/AsmParser/LLLexer.cpp#L525-L541)，看 `i32` 是如何被识别为 `lltok::Type` 并在 `TyVal` 里构造出 `IntegerType` 的。
3. 浏览真实测试样本 [llvm/test/Assembler/](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/test/Assembler)（目录下有大量 `.ll` 文件），任选一个，在心里用 `LexToken` 的 `switch` 走一遍它的第一两个 Token。

**需要观察的现象**：`define`、`i32`、`@main`、`%1` 这四类 Token 分别走的是 `switch` 的哪条分支（字母→`LexIdentifier`、数字/`i`→整数类型、`@`→`LexAt`、`%`→`LexPercent`）。

**预期结果**：你能用一句话说出——「`.ll` 文本在进入语法分析之前，已经被 `LLLexer` 切成了一串带类型化取值的 Token」。本项为源码阅读型实践，运行验证留给综合实践（第 5 节）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `parseAssembly*` 这一组函数要返回 `unique_ptr<Module>` 并注明「不会自动 verify」？

**参考答案**：解析器只负责「按文法把 Token 组装成对象」，但 IR 的合法性（如 SSA 每值只定义一次、基本块必以终结指令结尾、类型自洽）属于更深的语义约束，校验成本不低且并非所有调用方都需要。把「解析」和「校验」解耦，让需要校验的场景（如 `llvm-as`，见 [llvm-as.cpp:142](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/tools/llvm-as/llvm-as.cpp#L142) 的 `verifyModule`）显式调用，更灵活。

**练习 2**：`i32` 这个 Token 为什么不需要在 `LLParser` 里再去拼装类型？

**参考答案**：因为 `LexIdentifier()` 在词法阶段就已经用 `IntegerType::get(Context, NumBits)` 把 `i32` 构造好并放进 `TyVal`（见 [LLLexer.cpp:535-540](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/AsmParser/LLLexer.cpp#L535-L540)）。配合 u3-l3 讲过的「类型在 `LLVMContext` 内唯一化」，这里 `get` 到的就是那个唯一单例，语法器直接 `getTyVal()` 取用即可。

---

### 4.3 文本 IR 的打印：AsmWriter（Module → .ll）

#### 4.3.1 概念说明

`AsmWriter` 是 `AsmParser` 的镜像：它不读 Token，而是遍历内存 `Module` 树，按 `.ll` 文法把每个节点打印出来。你之前在 u3-l4 综合实践里调用过的 `M.print(outs(), nullptr)`，以及 `llvm-dis`、`opt` 的默认输出，最终都落到这里。

打印看似简单，其实有一个关键设计：**无名值的编号**。u2-l2 讲过，`.ll` 里 `%0`、`%1` 这种「数字编号」是打印时按定义顺序分配的——内存对象本身并没有这个编号。负责分配编号的是 `SlotTracker`，它对整个模块做一次编号扫描，保证「同一个 `Module` 多次打印，编号一致」。

#### 4.3.2 核心流程

`Module::print` 的套路是「建表 → 建打印器 → 打印」：

```
Module::print(OS)
  ├─ SlotTracker SlotTable(this)          // 扫描模块，给无名值分配 %0/%1/… 编号
  ├─ AssemblyWriter W(OS, SlotTable, M)   // 把「输出流 + 编号表 + 模块」打包成打印器
  └─ W.printModule(this)                  // 递归打印：模块头 → 全局 → 函数(基本块/指令)
```

`AssemblyWriter` 内部对每种 IR 节点都有一个 `printXxx` 方法，它们和 u3-l1 的包含层次一一对应：`printModule` → `printFunction` → `printInstruction`。换句话说，**AsmWriter 是 Module 层次树的一次「深度优先文本化」**。

#### 4.3.3 源码精读

`Module::print`（[AsmWriter.cpp:5118-5125](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/AsmWriter.cpp#L5118-L5125)）非常短，正好印证上面的三步：

```cpp
void Module::print(raw_ostream &ROS, AssemblyAnnotationWriter *AAW,
                   bool ShouldPreserveUseListOrder, bool IsForDebug) const {
  SlotTracker SlotTable(this);
  formatted_raw_ostream OS(ROS);
  AssemblyWriter W(OS, SlotTable, this, AAW, IsForDebug,
                   ShouldPreserveUseListOrder);
  W.printModule(this);
}
```

递归层次从 `printModule`（[AsmWriter.cpp:3105](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/AsmWriter.cpp#L3105)）开始，一路下钻到 `printFunction`（[AsmWriter.cpp:4139](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/AsmWriter.cpp#L4139)）打印 `define ... {`，再到 `printInstruction`（[AsmWriter.cpp:4437](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/AsmWriter.cpp#L4437)）打印每一条指令（带类型、操作数、名字）。

> 为什么说 `AsmWriter` 和 `LLLexer`/`LLParser` 是镜像？因为 AsmWriter `printInstruction` 打印出的 `add`、`load`、`ret` 等文本，恰好就是 `LLLexer` 的 `kw_add`/`kw_load`/`kw_ret` 关键字（4.2.3）、再由 `LLParser` 组装回 `Instruction`。一打印一解析，文法两端对齐，互为权威定义。这也是为什么改 IR 文法时，往往要同时改 `AsmWriter` 和 `AsmParser`。

#### 4.3.4 代码实践

**实践目标**：亲眼确认「`.ll` 文本完全由 `AssemblyWriter` 逐节点打印出来」。

**操作步骤**：

1. 阅读 [AsmWriter.cpp:5118-5125](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/AsmWriter.cpp#L5118-L5125)（`Module::print`），确认它就是「建 `SlotTracker` + 建 `AssemblyWriter` + `printModule`」。
2. 跳到 [AsmWriter.cpp:4437](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/IR/AsmWriter.cpp#L4437) 的 `printInstruction`，浏览它如何输出操作码、类型、操作数与可选名字。

**需要观察的现象**：打印器遍历的顺序就是 u3-l1 讲过的包含层次（Module → Function → BasicBlock → Instruction），与「读」侧 `Module` 的组织方式完全对称。

**预期结果**：你能解释「`errs() << *M`（即对 Module 调 `print`）为什么能稳定地输出可读 `.ll`」——因为 `SlotTracker` 给无名值编了号、`AssemblyWriter` 按层次递归打印。本项为源码阅读型实践。

#### 4.3.5 小练习与答案

**练习 1**：如果同一个 `Module` 调两次 `M.print(...)`，两次输出的 `%0`、`%1` 编号会不会一致？为什么？

**参考答案**：会一致。`SlotTracker` 的编号是按「无名值在模块中的出现顺序」确定性分配的，只要模块内容不变，每次扫描得到的编号表相同，打印结果也就相同。这正是测试里能用 `FileCheck` 精确匹配 `%0`、`%1` 的基础。

**练习 2**：为什么「打印」需要 `SlotTracker`，而「解析」不需要？

**参考答案**：`.ll` 文本里无名值的编号（`%0`/`%1`）只是「打印约定」，内存 `Instruction` 对象并不存这个编号——它只有可选的「名字」（u3-l2 的 `Value::getName`）。打印时必须现编一份，所以需要 `SlotTracker`；解析时文本里直接写着 `%0`/`%name`，`LLParser` 边读边建立「编号/名字 → Value」映射即可，无需额外编号表。

---

### 4.4 位码的写入：BitcodeWriter（Module → .bc）

#### 4.4.1 概念说明

位码（bitcode，`.bc`）是 IR 的**二进制序列化形式**。和文本相比，它有三个截然不同的设计目标：

- **紧凑**：用「位（bit）」而非「字节/字符」为单位编码，且对高频记录定义「缩写（abbreviation）」来压缩。
- **可流式、可随机访问**：以「块（block）」嵌套组织，块里装「记录（record）」；可以跳着读、只读需要的块。
- **向前兼容**：带版本「纪元（epoch）」与「自动升级」机制，新版本读取旧位码时能自动转换。

位码的物理格式叫 **bitstream**。它只有三类基本元素：

| 元素 | 含义 |
| --- | --- |
| 块（block） | 可嵌套的容器，每个块有一个 ID（如 `MODULE_BLOCK_ID`）和长度 |
| 记录（record） | 块内的一条数据，形如 `[code, op1, op2, ...]`，描述一个 IR 元素 |
| 缩写（abbrev） | 为高频记录定义的紧凑编码，避免每次都写完整的「未缩写记录」 |

文件以 4 字节**魔数**开头：ASCII 的 `'B'`、`'C'` 后跟 `0xC0`、`0xDE`，即字节序列 `42 43 C0 DE`。读取侧正是靠这 4 个字节判断「这是不是一个 `.bc`」（见 4.5.3）。

> 一个常被混淆的点：位码里**不存 `.ll` 文本**。它直接序列化 IR 对象（类型、常量、指令……），所以比「把 `.ll` 压个 zip」更省、更快。`.ll` 和 `.bc` 是同一个 `Module` 的两种独立编码，不存在「一个包含另一个」的关系。

#### 4.4.2 核心流程

`ModuleBitcodeWriter::write()`（[BitcodeWriter.cpp:5482-5542](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Bitcode/Writer/BitcodeWriter.cpp#L5482-L5542)）规定了写出一个模块的**固定顺序**——这也是位码读取侧必须遵循的布局：

```
writeBitcodeHeader()                       → 魔数 42 43 C0 DE
writeIdentificationBlock()                 → IDENTIFICATION 块：生产者串 "LLVM<版本>" + epoch
Stream.EnterSubblock(MODULE_BLOCK_ID)      → 进入主模块块
   ├─ writeModuleVersion()
   ├─ writeBlockInfo()                     → 块元信息（标准缩写定义）
   ├─ writeTypeTable()                     → TYPE 块：所有类型
   ├─ writeAttributeGroupTable() / writeAttributeTable()
   ├─ writeComdats()
   ├─ writeModuleInfo()                    → triple、inline asm、全局变量、函数原型
   ├─ writeModuleConstants()               → CONSTANTS 块
   ├─ writeModuleMetadataKinds() / writeModuleMetadata()   → 元数据
   ├─ writeUseListBlock()                  → use-list 顺序（可选，保证往返一致）
   ├─ writeOperandBundleTags() / writeSyncScopeNames()
   ├─ for 每个非声明函数: writeFunction(F)  → FUNCTION 块（每个函数体一块）
   ├─ writePerModuleGlobalValueSummary()   → 摘要（仅 ThinLTO，见 Index 参数）
   ├─ writeGlobalValueSymbolTable()        → 顶层符号表
   └─ writeModuleHash()                    → 模块哈希（仅 GenerateHash）
Stream.ExitBlock()
```

注意几个呼应前面讲义的设计：

- **类型表在前**（`writeTypeTable`）：因为后续所有值都引用类型，而类型是唯一化的（u3-l3），位码里给每个类型一个编号，值只存「类型 ID」。
- **常量表**（`writeModuleConstants`）：呼应 u3-l3 的 `Constant`，常量被集中存放、按 ID 引用。
- **每个函数体单独一块**（`writeFunction`）：这是「可随机访问」的关键——读取侧可以跳过不需要的函数块（见 4.5 的惰性物化）。

#### 4.4.3 源码精读

**魔数**。`writeBitcodeHeader`（[BitcodeWriter.cpp:5610-5618](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Bitcode/Writer/BitcodeWriter.cpp#L5610-L5618)）逐位写出这 4 个字节：

```cpp
Stream.Emit((unsigned)'B', 8);   // 0x42
Stream.Emit((unsigned)'C', 8);   // 0x43
Stream.Emit(0x0, 4); Stream.Emit(0xC, 4);   // → 字节 0xC0
Stream.Emit(0xE, 4); Stream.Emit(0xD, 4);   // → 字节 0xDE
```

它在 `BitcodeWriter` 构造时就被调用一次（[BitcodeWriter.cpp:5620-5628](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Bitcode/Writer/BitcodeWriter.cpp#L5620-L5628)），所以任何 `.bc` 文件都以 `42 43 C0 DE` 开头。

**块 ID**。所有块种类定义在 `bitc::BlockIDs`（[LLVMBitCodes.h:28-66](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Bitcode/LLVMBitCodes.h#L28-L66)），与上面 `write()` 写出的子表一一对应：

```cpp
enum BlockIDs {
  MODULE_BLOCK_ID = FIRST_APPLICATION_BLOCKID,   // 主模块块
  PARAMATTR_BLOCK_ID, PARAMATTR_GROUP_BLOCK_ID,
  CONSTANTS_BLOCK_ID,
  FUNCTION_BLOCK_ID,                             // 每个函数体
  IDENTIFICATION_BLOCK_ID,                       // 生产者 + epoch
  VALUE_SYMTAB_BLOCK_ID, METADATA_BLOCK_ID,
  TYPE_BLOCK_ID_NEW,                             // 类型表
  STRTAB_BLOCK_ID, SYMTAB_BLOCK_ID,              // 字符串表 / 符号表
  GLOBALVAL_SUMMARY_BLOCK_ID,                    // ThinLTO 摘要
  // ...
};
```

bitstream 的底层宽度规则与缩写机制定义在 `BitCodeEnums.h`（[BitCodeEnums.h:36-61](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Bitstream/BitCodeEnums.h#L36-L61)）：块 ID 用 VBR-8 编码、记录用 `UNABBREV_RECORD` 或自定义 `DEFINE_ABBREV`。缩写就是「为某条高频记录约定一种更短的位编码」，这是位码比文本紧凑的核心原因之一。

**识别块**。`writeIdentificationBlock`（[BitcodeWriter.cpp:5439-5459](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Bitcode/Writer/BitcodeWriter.cpp#L5439-L5459)）写两样东西：一个「`LLVM<版本号>`」的生产者串，和一个「纪元」值：

```cpp
writeStringRecord(Stream, bitc::IDENTIFICATION_CODE_STRING,
                  "LLVM" LLVM_VERSION_STRING, StringAbbrev);
// ...
constexpr std::array<unsigned, 1> Vals = {{bitc::BITCODE_CURRENT_EPOCH}};
Stream.EmitRecord(bitc::IDENTIFICATION_CODE_EPOCH, Vals, EpochAbbrev);
```

纪元定义在 [LLVMBitCodes.h:75-81](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Bitcode/LLVMBitCodes.h#L75-L81)，当前 `BITCODE_CURRENT_EPOCH = 0`。注释点明它的兼容契约：同一大版本内，小版本可读旧小版本生成的位码；`X.0` 版本还能读 `N-1` 大版本。读取侧会据此决定要不要「自动升级」旧 IR。

**公共入口**。多数调用方不直接用 `BitcodeWriter` 类，而用更方便的 `WriteBitcodeToFile`（[BitcodeWriter.cpp:5725-5752](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Bitcode/Writer/BitcodeWriter.cpp#L5725-L5752)），它一次把「模块 + 符号表 + 字符串表」写齐：

```cpp
auto Write = [&](BitcodeWriter &Writer) {
  Writer.writeModule(M, ShouldPreserveUseListOrder, Index, GenerateHash, ModHash);
  Writer.writeSymtab();   // 符号表（提升链接性能，读取侧非必需）
  Writer.writeStrtab();   // 字符串表（必须恰好写一次）
};
```

注意 `writeModule` 的 `Index` 参数（[BitcodeWriter.h:88-92](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Bitcode/BitcodeWriter.h#L88-L92)）：传入 `ModuleSummaryIndex` 时会额外写出「每模块摘要」，这正是 ThinLTO 的入口（见第 7 节，详细在 u8-l2）。另外，Darwin/Mach-O 目标会额外套一个「包装头」（wrapper header）以兼容系统归档器 `ar`（[BitcodeWriter.cpp:5736-5747](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Bitcode/Writer/BitcodeWriter.cpp#L5736-L5747)），读取侧有对应的 `SkipBitcodeWrapperHeader` 跳过它。

#### 4.4.4 代码实践

**实践目标**：在源码里走一遍「写一个 `.bc` 的完整步骤」，并对照 `llvm-as` 看它如何调用写入器。

**操作步骤**：

1. 读 `ModuleBitcodeWriter::write()`（[BitcodeWriter.cpp:5482-5542](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Bitcode/Writer/BitcodeWriter.cpp#L5482-L5542)），记下子块的写出顺序。
2. 读 [llvm-as.cpp:96-99](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/tools/llvm-as/llvm-as.cpp#L96-L99)，看 `llvm-as` 在 `verifyModule` 通过后如何调用 `WriteBitcodeToFile`（注意它把 `ShouldPreserveUseListOrder` 传了 `true`，以保往返一致）。

**需要观察的现象**：`llvm-as` 的 `main`（[llvm-as.cpp:110-161](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/tools/llvm-as/llvm-as.cpp#L110-L161)）严格按「`parseAssemblyFileWithIndex`（AsmParser）→ `verifyModule` → `WriteBitcodeToFile`（BitcodeWriter）」三步走，正好串起 4.2 与 4.4。

**预期结果**：你能说清 `llvm-as` 干的其实就是「左半环到右半环」：`.ll → Module → .bc`。本项为源码阅读型实践，实跑验证见综合实践。

#### 4.4.5 小练习与答案

**练习 1**：为什么位码要把「类型表」「常量表」集中放在前面，而不是随用随写？

**参考答案**：因为类型与常量都是唯一化、可被多处引用的对象（u3-l3）。集中成表、给每个分配一个 ID，后续值只需引用 ID，既省空间又便于读取侧「先建好类型/常量池，再按 ID 取用」。若随用随写，则同一类型/常量可能重复出现，且读取侧难以处理「先引用后定义」。

**练习 2**：`writeSymtab` 写出的符号表是「必需」的吗？删掉它，位码还能被正常读回 `Module` 吗？

**参考答案**：能正常读回。`writeSymtab` 的注释（[BitcodeWriter.h:53-60](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Bitcode/BitcodeWriter.h#L53-L60)）明说：读取侧并不依赖符号表来解释位码，符号表「只用于提升链接时性能」（让链接器不必完整解析 IR 就能拿到符号信息）。所以它是一种性能优化，而非正确性必需。

---

### 4.5 位码的读取与惰性物化：BitcodeReader（.bc → Module）

#### 4.5.1 概念说明

读取是写入的逆过程，但比写入多了一个对「规模」的考量。考虑 LTO 的场景：链接器面对成百上千个 `.bc`，若每个都「一次性把所有函数体全部解析进内存」，开销极大。可链接器在符号决议阶段，其实只关心「有哪些全局符号、各自的摘要」，多数函数体根本用不上。

于是位码读取器支持 **惰性物化（lazy materialization）**：

- 先只读「骨架」——模块信息、类型表、常量表、全局变量、函数**声明**（不含函数体）。
- 函数**定义体**先不读，只在「真正用到某个函数」时才按需解析它对应的 `FUNCTION_BLOCK`。

实现这一点的关键是 `BitcodeReader` 同时是一个 **`GVMaterializer`**（GlobalValue Materializer）：它把「读位码」和「按需物化」绑在一起，挂在 `Module` 上，谁要用某个尚未物化的 `GlobalValue`，就触发读取器去把对应块读出来。

> 这正是 `.bc` 相对 `.ll` 在「工具间传递」上的核心优势：文本只能整读，位码可以「先看目录、按需翻页」。

#### 4.5.2 核心流程

从「一个 `.bc` 文件」到「一个（可能部分物化的）`Module`」：

```
parseBitcodeFile(Buffer)                  ← 公共入口（全量读）
  ├─ getSingleModule(Buffer) → BitcodeModule BM
  └─ BM.parseModule(Context)
        └─ getModuleImpl(Context, MaterializeAll=true, ...)
              ├─ 跳到 IDENTIFICATION 块：读生产者串（用于诊断）
              ├─ 跳到 MODULE 块起点，new BitcodeReader(...)
              ├─ M->setMaterializer(R)        ← 把读取器挂为模块的物化器
              ├─ R->parseBitcodeInto(M, ...)   ← 读骨架 + 类型/常量/全局/函数声明
              └─ M->materializeAll()           ← 全量：把所有函数体也读出来
```

惰性版本（`getLazyModule` / `llvm-dis` 的 `getLazyModule`）的区别只在最后一步：不调 `materializeAll`，而是只 `materializeForwardReferencedFunctions`（处理 `blockaddress` 这类前向引用），其余函数体留到用时再读。

#### 4.5.3 源码精读

**魔数识别**。读取侧第一步是判断「这到底是不是位码」。三个内联函数（[BitcodeReader.h:259-289](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Bitcode/BitcodeReader.h#L259-L289)）检查开头的 4 字节：

```cpp
// 裸位码：'B' 'C' 0xC0 0xDE
inline bool isRawBitcode(...) {
  return BufPtr[0]=='B' && BufPtr[1]=='C' && BufPtr[2]==0xc0 && BufPtr[3]==0xde;
}
// 包装位码（Darwin 等）：0xDE 0xC0 0x17 0x0B
inline bool isBitcodeWrapper(...) {
  return BufPtr[0]==0xDE && BufPtr[1]==0xC0 && BufPtr[2]==0x17 && BufPtr[3]==0x0B;
}
inline bool isBitcode(...) { return isBitcodeWrapper(...) || isRawBitcode(...); }
```

这正好对应 4.4.3 写入侧的魔数。若是包装格式，`SkipBitcodeWrapperHeader`（[BitcodeReader.h:307-324](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Bitcode/BitcodeReader.h#L307-L324)）按头部里的 `Offset`/`Size` 字段跳到真正的 BC 段——头部结构（`Magic`/`Version`/`BitcodeOffset`/`BitcodeSize`）见 [BitCodeEnums.h:26-33](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Bitstream/BitCodeEnums.h#L26-L33) 的 `BWH_*` 与 [BitcodeReader.h:291-300](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Bitcode/BitcodeReader.h#L291-L300) 的注释。

**公共入口**。`parseBitcodeFile`（[BitcodeReader.cpp:9003-9010](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Bitcode/Reader/BitcodeReader.cpp#L9003-L9010)）极其简短，它把工作交给 `BitcodeModule`：

```cpp
Expected<BitcodeModule> BM = getSingleModule(Buffer);
return BM->parseModule(Context, Callbacks);
```

`BitcodeModule`（[BitcodeReader.h:112-179](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Bitcode/BitcodeReader.h#L112-L179)）代表「位码文件里的一个模块」（一个 `.bc` 里可含多个模块）。它提供 `parseModule`（全量）、`getLazyModule`（惰性）、`getSummary`（读 ThinLTO 摘要）、`getLTOInfo`（判断是否 ThinLTO）等多种粒度的读取。

**核心实现**。真正的活儿在 `getModuleImpl`（[BitcodeReader.cpp:8786-8825](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Bitcode/Reader/BitcodeReader.cpp#L8786-L8825)）：

```cpp
BitstreamCursor Stream(Buffer);
// 1) 先读识别块，拿到生产者串（仅用于诊断信息）
if (IdentificationBit != -1ull) {
  Stream.JumpToBit(IdentificationBit);
  readIdentificationBlock(Stream).moveInto(ProducerIdentification);
}
// 2) 跳到模块块，建读取器并挂为物化器
Stream.JumpToBit(ModuleBit);
auto *R = new BitcodeReader(std::move(Stream), Strtab, ProducerIdentification, Context);
std::unique_ptr<Module> M = std::make_unique<Module>(ModuleIdentifier, Context);
M->setMaterializer(R);                       // ← 关键：把读取器挂到模块上
// 3) 读骨架
R->parseBitcodeInto(M.get(), ShouldLazyLoadMetadata, IsImporting, Callbacks);
// 4) 按模式决定是否全量物化
if (MaterializeAll) M->materializeAll();     // 全量
else R->materializeForwardReferencedFunctions(); // 惰性
```

`parseBitcodeInto`（[BitcodeReader.cpp:4942-4955](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Bitcode/Reader/BitcodeReader.cpp#L4942-L4955)）设置好元数据加载器后，转调 `parseModule`，按 4.4.2 的逆序把各块还原成 IR 对象。

**`BitcodeReader` 类**。它继承自 `BitcodeReaderBase` 并实现 `GVMaterializer`（[BitcodeReader.cpp:581](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Bitcode/Reader/BitcodeReader.cpp#L581)），内部维护 `TypeList`（类型池）、`ValueList`（值池）、`MetadataLoader`（元数据加载器）、`InstructionList`（指令缓冲）等（[BitcodeReader.cpp:595-616](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/lib/Bitcode/Reader/BitcodeReader.cpp#L595-L616)）。正是这组容器把「位码里的 ID」逐步解析成「内存里的 `Type*`/`Value*`」，呼应 u3-l2/u3-l3 的对象模型。

> 兼容性体现在 `ParserCallbacks`（[BitcodeReader.h:75-99](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Bitcode/BitcodeReader.h#L75-L99)）里：它能在读取时覆盖 `DataLayout`、回传类型信息、以及控制「旧调试 intrinsic 是否自动升级」。这套机制和 4.2 的 `UpgradeDebugInfo`、4.4 的「纪元」一起，构成 LLVM IR 的版本兼容策略。

#### 4.5.4 代码实践

**实践目标**：在源码里确认「`llvm-dis` 是位码读取 + AsmWriter 打印的组合」，并看清惰性物化的开关。

**操作步骤**：

1. 读 [llvm-dis.cpp:204-223](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/tools/llvm-dis/llvm-dis.cpp#L204-L223)：`getBitcodeFileContents` 拿到 `BitcodeModule`，`MB.getLazyModule(...)` 惰性加载，随后 `M->materializeAll()` 把函数体也读全。
2. 读 [llvm-dis.cpp:264-271](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/tools/llvm-dis/llvm-dis.cpp#L264-L271)：最终用 `M->print(...)`（即 4.3 的 AsmWriter）输出 `.ll`。

**需要观察的现象**：`llvm-dis` 走的正是「右半环到左半环」——`.bc → Module → .ll`；它虽然用 `getLazyModule`（惰性）开始，但随即 `materializeAll()` 全量物化，因为反汇编要把所有内容都打印出来。

**预期结果**：你能解释「为什么 `llvm-dis` 适合用惰性加载开头」——`BitcodeModule` 的统一接口让「全量」与「按需」共用一条骨架读取路径，区别只在最后是否 `materializeAll`。本项为源码阅读型实践。

#### 4.5.5 小练习与答案

**练习 1**：链接器在做 LTO 符号决议时，为什么偏好 `getLazyModule` 而非 `parseModule`？

**参考答案**：符号决议阶段只需要「有哪些全局符号、各自的摘要（summary）」，绝大多数函数体用不到。`getLazyModule` 只读骨架、把函数体留到按需物化，从而避免把成百上千个 `.bc` 的全部函数体一次性解析进内存。等到真正要内联/优化某个函数时，再通过 `GVMaterializer` 触发读取。这正是 LTO/ThinLTO 能扩展到大型项目的关键（详见 u8-l2）。

**练习 2**：一个裸位码文件的前 4 字节是 `42 4E C0 DE`，读取器会把它当 `.bc` 吗？

**参考答案**：不会。`isRawBitcode`（[BitcodeReader.h:272-281](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Bitcode/BitcodeReader.h#L272-L281)）要求第二字节是 `'C'`（`0x43`），而这里是 `0x4E`（`'N'`），既不匹配裸位码、也不匹配包装位码，`isBitcode` 返回假，读取器会拒绝把它当位码处理。

---

## 5. 综合实践

把本讲四个最小模块串成一次完整的「无损往返」：用 `llvm-as` 把一段 `.ll` 转成 `.bc`，直接观察位码的字节与块结构，再用 `llvm-dis` 还原，最后对比是否一致。

**第 1 步：准备一段最小的 `.ll`**。新建 `add.ll`（示例代码，非项目原有文件）：

```llvm
; add.ll —— 求两数之和
define i32 @add(i32 %a, i32 %b) {
entry:
  %sum = add i32 %a, %b
  ret i32 %sum
}
```

**第 2 步：`.ll → .bc`（左半环到右半环，对应 4.2 + 4.4）**。用 `llvm-as`：

```bash
llvm-as add.ll -o add.bc        # 默认就是 add.ll → add.bc
```

在源码层面，这条命令即 [llvm-as.cpp:110-161](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/tools/llvm-as/llvm-as.cpp#L110-L161) 的 `main`：`parseAssemblyFileWithIndex`（`LLLexer`+`LLParser` 把 `.ll` 读成 `Module`）→ `verifyModule`（校验）→ `WriteBitcodeToFile`（`BitcodeWriter` 写成 `.bc`）。

**第 3 步：观察位码字节（魔数验证，对应 4.4）**。用 `hexdump` 看前 16 字节：

```bash
hexdump -C add.bc | head -2
```

**预期现象**：前 4 字节应为 `42 43 c0 de`——即 4.4.3 中 `writeBitcodeHeader` 写出的 `'B' 'C' 0xC0 0xDE`。这就是「裸位码」的魔数。

**第 4 步：观察位码块结构（对应 4.4.1 / 4.4.2）**。用 `llvm-bcanalyzer` 解析块布局：

```bash
llvm-bcanalyzer -dump add.bc | head -40
```

**预期现象**：能看到 `IDENTIFICATION_BLOCK`（含生产者串 `LLVM<版本>` 与 epoch）、`MODULE_BLOCK`、其内的 `TYPE_BLOCK_ID_NEW`、`CONSTANTS_BLOCK`、`VALUE_SYMTAB_BLOCK` 以及每个函数对应的 `FUNCTION_BLOCK` 等块——正是 4.4.2 列出的写出顺序。`-dump` 还会显示每条记录和它用的「缩写（abbrev）」，直观体现位码的紧凑编码。

**第 5 步：`.bc → .ll`（右半环到左半环，对应 4.5 + 4.3）**。用 `llvm-dis` 还原：

```bash
llvm-dis add.bc -o add.dis.ll
diff add.ll add.dis.ll
```

**预期现象**：`llvm-dis`（[llvm-dis.cpp:204-271](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/tools/llvm-dis/llvm-dis.cpp#L204-L271)）用 `getBitcodeFileContents`+`getLazyModule`+`materializeAll`（`BitcodeReader`）把 `.bc` 读回 `Module`，再 `M->print`（`AsmWriter`）打印。`diff` 通常只有细微差异（如 `add.dis.ll` 顶部多一行 `; ModuleID = 'add.bc'`），核心 IR（`define`/`add`/`ret`）应完全一致——这就是「无损往返」。

**第 6 步（选做，源码阅读型）**：浏览 [llvm/test/Bitcode/](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/test/Bitcode) 目录，里面有大量 `.ll` 与同名 `.ll.bc` 配对文件（如 `DICompileUnit-no-DWOId.ll` 与 `.ll.bc`）。任选一对，它们就是「同一 `Module` 的两种编码」的真实样本，可用来验证你的往返理解。

> 上述命令的具体输出（字节数、块 ID 顺序、`diff` 行数）依赖本地构建的 LLVM 版本与目标，**待本地验证**。若尚未构建 LLVM，可仅做源码阅读：把第 2、5 步分别对应到 `llvm-as.cpp` 与 `llvm-dis.cpp` 的 `main`，确认两个工具正是本讲四个库的「胶水」。

## 6. 本讲小结

- LLVM IR 有两种持久化形式：人类可读的 `.ll`（`AsmParser` 读 / `AsmWriter` 写）与紧凑的二进制 `.bc`（`BitcodeReader` 读 / `BitcodeWriter` 写）；两者序列化的是同一棵内存 `Module`，因而**无损往返**，差别只在可读性、体积与读写速度。
- **文本侧**：`AsmParser` 经「`LLLexer` 切 Token → `LLParser` 组装 `Module`」两步，公共入口是 `parseAssembly*`；`AsmWriter` 则用 `AssemblyWriter` 按 `Module → Function → Instruction` 层次递归打印，`SlotTracker` 给无名值分配 `%0/%1` 编号。二者互为镜像，共同定义 `.ll` 文法。
- **位码侧**：bitstream 以「块（block）+ 记录（record）+ 缩写（abbrev）」组织，文件头是魔数 `42 43 C0 DE`；`ModuleBitcodeWriter::write()` 按固定顺序写出 `IDENTIFICATION`、`MODULE`（含 `TYPE`/`CONSTANTS`/模块信息/每个 `FUNCTION` 块等）。
- **位码读取**支持**惰性物化**：`BitcodeReader` 同时是 `GVMaterializer`，先读骨架、把函数体留到用时再读（`getLazyModule` vs `parseModule` 的区别即在此），这正是 LTO/ThinLTO 与 IR 分发能扩展到大规模的关键。
- **兼容性**贯穿两端：文本侧有 `UpgradeDebugInfo`，位码侧有「纪元（epoch）」+ `ParserCallbacks` 的自动升级，共同保证新版本能读旧 IR。
- 两个工具是整讲的「胶水」：`llvm-as` = `AsmParser` → `BitcodeWriter`（`.ll → .bc`），`llvm-dis` = `BitcodeReader` → `AsmWriter`（`.bc → .ll`），直接印证了那条无损往返环。

## 7. 下一步学习建议

- **进入 Pass 与优化（u4）**：现在你已经能把 IR 在三种形态间自如转换。下一单元 u4-l1 讲新 Pass 管理器与 `PassBuilder`——`opt` 读进 `Module`、跑 pass 流水线、再写出 IR，正建立在「`.ll`/`.bc` → `Module` → `.ll`/`.bc`」这套往返之上。
- **LTO/ThinLTO（u8-l2）**：本讲多次提到的 `ModuleSummaryIndex`、`writePerModuleGlobalValueSummary`、`getSummary`、`getLTOInfo` 与惰性物化，是链接时优化的入口。学完 u8-l2 你会把「位码为什么这样设计」彻底接上 LTO 全流程。
- **JIT 执行（u8-l1）**：`lli` 默认通过 ORC v2 直接「执行」`.ll`/`.bc`——它读位码的方式正是本讲的 `BitcodeReader` 路径。
- **深入 bitstream 底层**：若对二进制格式本身感兴趣，可读 [llvm/include/llvm/Bitstream/BitCodeEnums.h](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Bitstream/BitCodeEnums.h)（宽度/缩写规则）与 [llvm/include/llvm/Bitcode/LLVMBitCodes.h](https://github.com/llvm/llvm-project/blob/bd9aa3ca5789c94b3a0d0c42b0aa8a94be4c0695/llvm/include/llvm/Bitcode/LLVMBitCodes.h)（块/记录码定义），并用 `llvm-bcanalyzer -dump` 对照真实 `.bc` 逐块解读。
