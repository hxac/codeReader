# link() 入口与 Ctx 上下文对象

## 1. 本讲目标

学完本讲，你应当能够：

1. 说出 ELF 后端入口函数 `elf::link()` 的五个参数各自含义，以及它的返回值代表什么。
2. 理解 `Ctx` 这个「ELF 全局上下文」聚合了哪些核心成员（`arg` / `driver` / `script` / `target` / `symtab` / `objectFiles` 等），并解释它为什么必须继承自 `CommonLinkerContext`。
3. 说明诊断流 `ELFSyncStream`（即 `Err` / `Warn` / `Fatal` / `Msg` / `Log`）是如何借助 RAII 析构、线程安全地转发到底层 `ErrorHandler` 的。

本讲是第二单元「ELF 链接主线」的起点。在上一讲（u1-l4）里我们看到 `lldMain` 通过 flavor 分发表选中了某个后端的 `link()`；从这一讲开始，我们真正进入 ELF 后端的内部。

## 2. 前置知识

阅读本讲前，建议你已经理解以下概念（在 u1-l1 到 u1-l4 中建立）：

- **LLD 是一个二进制内含多个后端**：`tools/lld/lld.cpp` 的 `lldMain` 根据程序名或 `-flavor` 把控制权交给对应后端的 `link()`，ELF 后端对应的那个函数就是本讲的 `elf::link()`。
- **链接器的三大角色**：`InputFile`（输入文件）、`Driver`（驱动整个流程）、`Writer`（写出结果）。本讲关注 Driver 的最外层。
- **「LLD 可作为库」的设计目标**：LLD 希望能在一个进程里被反复调用。为此它把所有「看似全局」的状态聚合到一个堆对象上，而不是用程序级全局变量或函数局部静态变量。这个堆对象就是 `CommonLinkerContext`，而 ELF 的 `Ctx` 是它的派生类。

补充两个 C++ 小概念：

- **placement new（就地构造）**：在已分配好的内存上直接构造对象，而不另开内存。LLD 的符号解析会用到它（u4-l1 详述），本讲只需知道有这回事。
- **RAII（资源获取即初始化）**：对象的析构函数负责释放资源。LLD 的诊断流正是利用「临时对象析构时把缓冲区一次性输出」来实现线程安全的日志打印。

## 3. 本讲源码地图

本讲涉及三个关键源码文件：

| 文件 | 作用 |
| --- | --- |
| [ELF/Driver.cpp](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp) | ELF 后端的驱动实现。`elf::link()` 入口、`ELFSyncStream` 的工厂函数、`LinkerDriver::linkerMain` 都在这里。 |
| [ELF/Config.h](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Config.h) | 声明 `Config`（命令行配置）、`Ctx`（全局上下文）、`ELFSyncStream` 及一组诊断流工厂函数的声明。 |
| [include/lld/Common/CommonLinkerContext.h](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/include/lld/Common/CommonLinkerContext.h) | 公共基类 `CommonLinkerContext`，聚合了所有后端共享的全局状态（分配器、`ErrorHandler`），并提供 `context<T>()` 访问接口。 |

辅助文件（出现少量引用）：

- [include/lld/Common/ErrorHandler.h](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/include/lld/Common/ErrorHandler.h) 与 [Common/ErrorHandler.cpp](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/Common/ErrorHandler.cpp)：`ErrorHandler` 与 `SyncStream` 的实现。
- [Common/CommonLinkerContext.cpp](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/Common/CommonLinkerContext.cpp)：`CommonLinkerContext` 的构造/析构与全局指针 `lctx`。
- [ELF/SymbolTable.h](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/SymbolTable.h)：`SymbolTable` 的构造。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **`elf::link()` 入口**：后端被分发到之后，做的第一件事——建上下文、装好诊断、再启动 `linkerMain`。
2. **`Ctx` 上下文聚合**：ELF 链接过程中所有「全局状态」的归宿，以及它与公共基类的关系。
3. **诊断流 `ELFSyncStream`**：`Err` / `Warn` / `Fatal` 这些函数背后的线程安全输出机制。

### 4.1 `elf::link()` 入口

#### 4.1.1 概念说明

在 u1-l4 中，`lldMain` 通过分发表（`whichDriver`）查到了 ELF 后端的入口函数指针，这个指针指向的就是 `lld::elf::link()`。也就是说：**`elf::link()` 是「外部世界进入 ELF 链接器内部」的门槛**。

它的职责非常聚焦，只做三件事：

1. **建上下文**：在堆上 `new` 一个 `Ctx`，并把诊断用的 `ErrorHandler` 初始化好。
2. **装好辅助对象**：创建 `LinkerScript`、`SymbolTable`，预置 `symAux`。
3. **把控制权交给 `linkerMain`**：真正解析命令行、加载文件、跑流水线的工作都在 `linkerMain` 里（u2-l2 详述）。

这种「入口很薄、只负责装配、真正的活交给下一层」的设计，正是为了让 `link()` 能被当作库函数安全调用——调用方只要拿到返回的 `bool`，就能知道这次链接是否成功。

#### 4.1.2 核心流程

`elf::link()` 的执行过程可以用下面这段伪代码概括：

```
elf::link(args, stdoutOS, stderrOS, exitEarly, disableOutput):
    1. ctx = new Ctx                      # 堆上建上下文（构造时设置全局 lctx）
    2. ctx.e.initialize(stdoutOS, stderrOS, exitEarly, disableOutput)
    3. ctx.e.logName = args[0] 去掉路径后的程序名
    4. ctx.e.errorLimitExceededMsg = "...too many errors..."
    5. script = LinkerScript(ctx)         # 栈上建链接脚本对象
       ctx.script = &script
    6. ctx.symAux.emplace_back()          # 预置一个默认 SymbolAux（索引 0）
    7. ctx.symtab = make_unique<SymbolTable>(ctx)
    8. ctx.arg.progName = args[0]
    9. ctx.driver.linkerMain(args)        # 真正的链接驱动
   10. return errCount(ctx) == 0          # 成功当且仅当错误数为 0
```

注意第 5 步的 `LinkerScript` 是**栈对象**，而 `ctx.script` 只是一个裸指针——它的生命周期仅覆盖这一次 `link()` 调用，调用返回后自动析构。而第 1 步的 `Ctx` 是堆对象，它的释放由更上层的 `unsafeLldMain()` 负责（见 u1-l4 与 u3-l4），源码注释也明确写了这一点。

#### 4.1.3 源码精读

完整入口函数如下：

```cpp
// ELF/Driver.cpp
bool link(ArrayRef<const char *> args, llvm::raw_ostream &stdoutOS,
          llvm::raw_ostream &stderrOS, bool exitEarly, bool disableOutput) {
  // This driver-specific context will be freed later by unsafeLldMain().
  auto *context = new Ctx;
  Ctx &ctx = *context;

  context->e.initialize(stdoutOS, stderrOS, exitEarly, disableOutput);
  context->e.logName = args::getFilenameWithoutExe(args[0]);
  context->e.errorLimitExceededMsg =
      "too many errors emitted, stopping now (use "
      "--error-limit=0 to see all errors)";

  LinkerScript script(ctx);
  ctx.script = &script;
  ctx.symAux.emplace_back();
  ctx.symtab = std::make_unique<SymbolTable>(ctx);

  ctx.arg.progName = args[0];

  ctx.driver.linkerMain(args);

  return errCount(ctx) == 0;
}
```

这段代码做了上面伪代码列出的全部事情：[ELF/Driver.cpp:118-140](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L118-L140)（ELF 后端入口，建上下文并启动 `linkerMain`）。

**逐个参数说明：**

| 参数 | 类型 | 含义 |
| --- | --- | --- |
| `args` | `ArrayRef<const char *>` | 命令行参数数组，**含 `argv[0]`**（程序名）。注意 `linkerMain` 内部会 `args.slice(1)` 跳过它。 |
| `stdoutOS` | `raw_ostream &` | 标准输出流。`-M`/`--Map`、`--version`、`--print-*` 等输出都写到这个流，而不是直接 `printf`，从而允许调用方重定向。 |
| `stderrOS` | `raw_ostream &` | 标准错误流，错误与警告写到这里。 |
| `exitEarly` | `bool` | 是否在退出时走「快路径」直接 `_exit`，不做内存清理（详见 u1-l4 与 u3-l3）。 |
| `disableOutput` | `bool` | 为 `true` 时，LLD **不真正写出输出文件**（用于「只校验命令行/做语法检查」或库场景）。它还会把写到 `-`（即 stdout）的辅助文件重定向到 `/dev/null`（见 `openAuxiliaryFile`）。 |

**返回值：**`bool`——`true` 表示这次链接没有任何错误（`errCount(ctx) == 0`），`false` 表示有错误。注意这个返回值和顶层 `lldMain` 返回的 `Result{retCode, canRunAgain}` 不是一回事：`link()` 是「单次链接是否成功」，而 `Result` 还携带「进程是否还能再次进入 LLD」的信息（u3-l4 详述）。

#### 4.1.4 代码实践

**实践目标**：亲手跟踪 `elf::link()` 内部对象的创建顺序，理解「先装配后驱动」的设计。

**操作步骤**：

1. 打开 [ELF/Driver.cpp:118-140](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L118-L140)，对照下面的表格，把每一行对应到一个对象：

   | 顺序 | 代码 | 创建/初始化的对象 | 所在位置 |
   | --- | --- | --- | --- |
   | 1 | `new Ctx` | `Ctx`（含继承来的 `ErrorHandler e`、分配器等） | 堆 |
   | 2 | `context->e.initialize(...)` | `ErrorHandler`（设置流、`exitEarly`、`disableOutput`） | `Ctx` 成员 |
   | 3 | `LinkerScript script(ctx)` | `LinkerScript` | 栈 |
   | 4 | `ctx.symAux.emplace_back()` | 一个默认 `SymbolAux`（`gotIdx`/`pltIdx` 等均为 `uint32_t(-1)`） | `Ctx.symAux` |
   | 5 | `make_unique<SymbolTable>(ctx)` | `SymbolTable` | `Ctx.symtab`（unique_ptr） |
   | 6 | `ctx.driver.linkerMain(args)` | （驱动启动，不再创建顶层对象） | — |

2. **回答一个关键问题：为什么这些对象必须先于 `linkerMain` 存在？**
   - `ErrorHandler`：`linkerMain` 第一步 `parser.parse(ctx, ...)` 就可能解析失败并调用 `Err(ctx)`，所以诊断必须在解析之前就绪；此外 `e.logName` 决定错误信息里显示的程序名。
   - `LinkerScript`：`readConfigs`/`setConfigs` 会处理 `-T <script>` 等选项，`linkerMain` 后续几乎每一步都会查询 `ctx.script`，所以脚本对象不能为空。
   - `SymbolTable`：`linkerMain` → `createFiles` → `addFile` 会立即往符号表里插入符号（`symtab->addSymbol`），符号表若不存在就无法装载任何输入文件。
   - `symAux`：符号的索引类属性（GOT/PLT 槽位编号）单独存放在这个副表里（见 `SymbolAux`），预置索引 0 的默认项保证后续按索引访问时有一致的行为。

**需要观察的现象 / 预期结果**：你应当能用自己的话复述「`link()` 做的是装配，`linkerMain` 做的是驱动」这一分工。

> 说明：本实践为源码阅读型实践，无需运行命令；如果你想跑一个最小例子，可执行 `ld.lld --version`，它会进入 `linkerMain` 后通过 `Msg(ctx)` 输出版本字符串（详见 4.3）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `LinkerScript` 用栈对象而 `Ctx` 用堆对象？

**参考答案**：`LinkerScript` 的生命周期天然只覆盖一次链接，栈对象在 `link()` 返回时自动析构，简单高效；而 `Ctx` 是整个 LLD 实例的全局状态载体，需要由 `unsafeLldMain()` 在确认不再重入后统一释放（注释 `freed later by unsafeLldMain()`），所以放在堆上。

**练习 2**：`link()` 的返回值是 `bool`，那调用方（`lldMain`）怎么知道进程「能否再次进入 LLD」？

**参考答案**：这个信息不由 `link()` 返回，而由更上层的 `Result{retCode, canRunAgain}` 承载（u1-l4、u3-l4）。`link()` 只回答「这一次链接是否无错」。

---

### 4.2 `Ctx` 上下文聚合

#### 4.2.1 概念说明

在早期的 LLD 里，符号表、配置、脚本指针等都是程序级全局变量。这种写法有两个问题：

1. **不可重入**：一个进程里只能跑一次链接，无法把 LLD 当库反复调用。
2. **初始化顺序不确定**：分散在各个翻译单元的全局变量，构造顺序难以保证。

`CommonLinkerContext` 的引入正是为了解决这两个问题——它把所有「看似全局」的状态聚合到**一个堆对象**里，构造即初始化、析构即清理，顺序完全确定。ELF 后端在此基础上派生出 `Ctx`，把 ELF 专属的状态（配置、驱动、脚本、符号表、输入文件列表等）也挂到同一个对象上。

一句话总结：**`Ctx` 是 ELF 链接过程的「总账本」，链接过程中任何代码都能通过一个 `Ctx &ctx` 引用访问到全部状态。**

#### 4.2.2 核心流程

`Ctx` 的继承与聚合关系如下：

```
CommonLinkerContext            （公共层，四个后端共享）
├─ bAlloc        BumpPtrAllocator   内存分配器
├─ saver         StringSaver        去重字符串存储
├─ uniqueSaver   UniqueStringSaver  唯一字符串存储
├─ instances     DenseMap<void*, SpecificAllocBase*>  按类型分桶的分配器表
└─ e             ErrorHandler       诊断处理器（错误计数、流、互斥锁）
        ▲
        │ 继承（struct Ctx : CommonLinkerContext）
        │
Ctx                            （ELF 专属）
├─ arg           Config             命令行配置（几百个字段的「大袋子」）
├─ driver        LinkerDriver       驱动对象（含 linkerMain/createFiles/link<ELFT>）
├─ script        LinkerScript*      指向栈上的脚本对象
├─ target        unique_ptr<TargetInfo>  架构后端（X86_64/AArch64/...）
├─ symtab        unique_ptr<SymbolTable> 符号表
├─ symAux        SmallVector<SymbolAux>  符号索引属性副表
├─ objectFiles   SmallVector<ELFFileBase*>   输入目标文件
├─ sharedFiles / binaryFiles / bitcodeFiles ...
├─ inputSections SmallVector<InputSectionBase*>  聚合后的输入段
├─ ehInputSections SmallVector<EhInputSection*>  .eh_frame 段（单独存放）
├─ out / in / outputSections      输出段、合成段的容器
└─ sym           ElfSym            __bss_start / _etext / _end 等链接器生成符号
```

这条继承链意味着：**所有 ELF 代码都能拿到一个 `Ctx &ctx`，就同时拥有了公共状态（`ctx.e`、`ctx.bAlloc`）和 ELF 状态（`ctx.arg`、`ctx.symtab`）。** 这是 LLD「把全局状态传引用」改革的核心载体。

#### 4.2.3 源码精读

公共基类 `CommonLinkerContext` 聚合了分配器与 `ErrorHandler`，并说明「优先用堆聚合而非全局/局部静态」的动机：

```cpp
// include/lld/Common/CommonLinkerContext.h
class CommonLinkerContext {
public:
  CommonLinkerContext();
  virtual ~CommonLinkerContext();
  static void destroy();

  llvm::BumpPtrAllocator bAlloc;
  llvm::StringSaver saver{bAlloc};
  llvm::UniqueStringSaver uniqueSaver{bAlloc};
  llvm::DenseMap<void *, SpecificAllocBase *> instances;

  ErrorHandler e;
};
```

见 [include/lld/Common/CommonLinkerContext.h:32-45](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/include/lld/Common/CommonLinkerContext.h#L32-L45)（公共基类，聚合分配器与 `ErrorHandler`）。文件顶部的注释直接点明了设计意图：「Instead of program-wide globals or function-local statics, we prefer aggregating all "global" states into a heap-based structure」。

构造函数会把「当前实例」登记到一个文件内静态指针 `lctx` 上，这是 `context<T>()` 能找到实例的依据：

```cpp
// Common/CommonLinkerContext.cpp
static CommonLinkerContext *lctx;

CommonLinkerContext::CommonLinkerContext() {
  lctx = this;
  codegen::RegisterCodeGenFlags CGF;
}
```

见 [Common/CommonLinkerContext.cpp:23-29](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/Common/CommonLinkerContext.cpp#L23-L29)（构造函数设置全局 `lctx`）。注意注释承认这是一种「临时方案」——理想情况下应把 `Ctx &` 处处按引用传递（这正是当前代码正在做的事），最终让 LLD 完全摆脱全局状态、支持单进程多实例。

访问接口是一组模板/函数，它们都从 `lctx` 取实例：

```cpp
// include/lld/Common/CommonLinkerContext.h
CommonLinkerContext &commonContext();
template <typename T = CommonLinkerContext> T &context() {
  return static_cast<T &>(commonContext());
}
bool hasContext();

inline llvm::BumpPtrAllocator &bAlloc() { return context().bAlloc; }
inline llvm::StringSaver &saver() { return context().saver; }
```

见 [include/lld/Common/CommonLinkerContext.h:50-60](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/include/lld/Common/CommonLinkerContext.h#L50-L60)（`context<T>()` / `hasContext()` 及便捷访问器）。当某段公共代码（不知道 `Ctx` 的存在）需要分配内存时，就调用全局 `lld::bAlloc()`，它内部会 `static_cast` 到 `CommonLinkerContext` 取出分配器。

ELF 的 `Ctx` 继承自这个基类，并挂上 ELF 专属成员。这里只摘录开头几行足以体现继承关系与核心字段：

```cpp
// ELF/Config.h
struct Ctx : CommonLinkerContext {
  Config arg;
  LinkerDriver driver;
  LinkerScript *script;
  std::unique_ptr<TargetInfo> target;

  // These variables are initialized by Writer and should not be used before
  // Writer is initialized.
  uint8_t *bufferStart = nullptr;
  PhdrEntry *tlsPhdr = nullptr;
  SmallVector<std::unique_ptr<PhdrEntry>, 0> phdrs;
  // ...（OutSections out; outputSections; InStruct in; ...）

  std::unique_ptr<SymbolTable> symtab;
  SmallVector<Symbol *, 0> synthesizedSymbols;
  // ...
  SmallVector<ELFFileBase *, 0> objectFiles;
  SmallVector<SharedFile *, 0> sharedFiles;
  SmallVector<BinaryFile *, 0> binaryFiles;
  SmallVector<BitcodeFile *, 0> bitcodeFiles;
  SmallVector<InputSectionBase *, 0> inputSections;
  SmallVector<EhInputSection *, 0> ehInputSections;

  SmallVector<SymbolAux, 0> symAux;
  // ...
  Ctx();
  // ...
};
```

见 [ELF/Config.h:651-787](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Config.h#L651-L787)（`Ctx` 结构体，聚合了 ELF 链接所需的全部全局状态）。可以看到 `Ctx` 同时是「公共状态容器」（通过继承）和「ELF 状态容器」（通过成员）。

`Ctx` 的构造函数非常简单，只初始化 `driver`：

```cpp
// ELF/Driver.cpp
Ctx::Ctx() : driver(*this) {}
```

见 [ELF/Driver.cpp:99](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L99)（`Ctx` 构造函数，把 `driver` 绑定到自己）。注意构造 `driver` 之前，`Ctx` 自身（含继承的 `CommonLinkerContext`）已构造完成，所以 `LinkerDriver` 拿到的 `ctx` 引用是完全可用的。

> 关于 `arg`（`Config`）：它是命令行选项的「大袋子」，字段数量极多（几百个），大部分与选项同名。它的填充发生在 `linkerMain` 的 `readConfigs`/`setConfigs` 阶段（u2-l2），本讲只需知道 `ctx.arg` 是配置入口。

#### 4.2.4 代码实践

**实践目标**：在源码中确认 `Ctx` 通过继承同时拥有公共状态和 ELF 状态。

**操作步骤**：

1. 打开 [ELF/Config.h:651](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Config.h#L651)，确认 `struct Ctx : CommonLinkerContext` 这一行。
2. 回到 [include/lld/Common/CommonLinkerContext.h:32-45](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/include/lld/Common/CommonLinkerContext.h#L32-L45)，找到 `ErrorHandler e;` 成员。
3. 回到本讲的 `link()`：`context->e.initialize(...)` 里的 `e` 其实**不是 `Ctx` 直接声明的成员，而是从 `CommonLinkerContext` 继承来的**。这一行能编译通过，正是继承关系的直接证据。

**预期结果**：你应当能解释「为什么 `Ctx` 没有显式写 `ErrorHandler e;`，却能用 `ctx.e`」——因为它是继承自公共基类的成员。

#### 4.2.5 小练习与答案

**练习 1**：`context().bAlloc()` 这样的全局函数还在用，而 ELF 代码里更多是直接 `ctx.bAlloc`，两者矛盾吗？

**参考答案**：不矛盾。`context<T>()` 内部就是从 `lctx` 取出当前实例并 `static_cast`，最终访问的还是同一个 `bAlloc`。公共层代码（不知道 `Ctx` 类型）用全局函数；ELF 层代码已经有 `Ctx &ctx` 引用，就直接用成员访问，少一次 `static_cast`、也更清晰。这正反映了 LLD 正在从「全局函数」向「处处传引用」逐步迁移的过渡状态。

**练习 2**：`Ctx` 里 `objectFiles` / `sharedFiles` / `bitcodeFiles` 等是分开存放的多个 `SmallVector`，而不是一个统一的 `inputFiles` 列表，为什么？

**参考答案**：不同类型的输入文件后续处理路径完全不同（目标文件要做段聚合、共享库要做符号版本解析、bitcode 要走 LTO 编译）。按类型分桶存放，让流水线各阶段能直接拿到自己关心的子集，避免每次都要 `dynamic_cast` 判断类型。

---

### 4.3 诊断流 `ELFSyncStream`

#### 4.3.1 概念说明

链接器在解析输入、处理符号时会大量报告诊断信息（错误、警告、日志）。LLD 的诊断设计有三个目标（见 `ErrorHandler.h` 顶部注释）：

1. **简单**：发现错误就调 `error()` 报告并继续，而不是用 `ErrorOr<T>` 把可能失败的函数层层包裹。
2. **尽量多报**：一次运行要尽可能多地暴露错误，而不是遇到第一个错就停。办法是「报告 + 继续用合理默认值」，到某个检查点（`if (errCount) return;`）再统一退出。
3. **线程安全**：链接过程大量并行（u9-l2），所以输出必须加锁，**禁止直接用 `llvm::outs()` / `llvm::errs()`**。

为了同时满足「写起来像流（`<<`）」和「线程安全地一次性输出」，LLD 设计了 `SyncStream`（公共层）和 `ELFSyncStream`（ELF 层派生）。它的思路类似 C++20 的 `std::osyncstream`：**先把内容写进一个临时缓冲区，在析构时再加锁、一次性刷到底层流。**

#### 4.3.2 核心流程

一次 `Err(ctx) << "msg"` 的完整生命周期：

```
1. Err(ctx)              → 返回一个临时 ELFSyncStream（level = Err 或 Warn，取决于 --noinhibit-exec）
2. << "msg"              → 把 "msg" 写进 ELFSyncStream 内部的 SmallString 缓冲区
3. （语句结束）临时对象析构 → ~SyncStream() 据 level 调 e.error(buf) / e.warn(buf) / ...
4. e.error(buf)          → 加锁(mu)，写 stderrOS，errorCount++
5. errCount(ctx)         → 返回 e.errorCount，供检查点判断
```

关键在于「析构即提交」。同一行里多次 `<<` 都只是往缓冲区追加，**只有析构那一刻才加锁输出**，所以一条诊断在输出流里永远是一个完整、连续的整体，不会被其他线程的输出打断。

`Err` 还有一个「降级」技巧：当用户指定了 `--noinhibit-exec` 时，`Err(ctx)` 实际把级别设成 `Warn`——即「错误降级为警告，仍然生成输出」。这与 `ErrAlways`（无论何时都当错误）形成对比。

#### 4.3.3 源码精读

公共层 `SyncStream` 持有一个 `ErrorHandler &` 引用、一个级别和一个缓冲区：

```cpp
// include/lld/Common/ErrorHandler.h
enum class DiagLevel { None, Log, Msg, Warn, Err, Fatal };

class SyncStream {
  ErrorHandler &e;
  DiagLevel level;
  llvm::SmallString<0> buf;

public:
  mutable llvm::raw_svector_ostream os{buf};
  SyncStream(ErrorHandler &e, DiagLevel level) : e(e), level(level) {}
  SyncStream(SyncStream &&o) : e(o.e), level(o.level), buf(std::move(o.buf)) {}
  ~SyncStream();
  // ...
};
```

见 [include/lld/Common/ErrorHandler.h:155-171](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/include/lld/Common/ErrorHandler.h#L155-L171)（`DiagLevel` 枚举与 `SyncStream` 类）。`os` 是绑定在 `buf` 上的流，所以 `s.os << x` 实际上就是往 `buf` 追加。

析构函数按级别分发到底层 `ErrorHandler` 的各个方法：

```cpp
// Common/ErrorHandler.cpp
SyncStream::~SyncStream() {
  switch (level) {
  case DiagLevel::None:  break;
  case DiagLevel::Log:   e.log(buf);       break;
  case DiagLevel::Msg:   e.message(buf, e.outs()); break;
  case DiagLevel::Warn:  e.warn(buf);      break;
  case DiagLevel::Err:   e.error(buf);     break;
  case DiagLevel::Fatal: ...               break;
  }
}
```

见 [Common/ErrorHandler.cpp:338-353](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/Common/ErrorHandler.cpp#L338-L353)（`SyncStream` 析构，按级别转发到 `ErrorHandler`）。`ErrorHandler::error()` 等方法内部会取 `mu` 互斥锁再写流、并维护 `errorCount`、`errorLimit`。

ELF 层的 `ELFSyncStream` 只是「带上 `Ctx &`」的薄包装，把 `ctx.e` 传给基类：

```cpp
// ELF/Config.h
struct ELFSyncStream : SyncStream {
  Ctx &ctx;
  ELFSyncStream(Ctx &ctx, DiagLevel level)
      : SyncStream(ctx.e, level), ctx(ctx) {}
};
```

见 [ELF/Config.h:795-799](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Config.h#L795-L799)（`ELFSyncStream`，把 `ctx.e` 传给 `SyncStream`）。配套的 `operator<<` 重载让它支持各种类型（普通值、`const char*`、`llvm::Error` 等），见 [ELF/Config.h:801-817](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Config.h#L801-L817)。

真正在代码里被调用的「工厂函数」定义在 Driver.cpp 里。它们返回临时对象，让调用点写成 `Err(ctx) << "..."`：

```cpp
// ELF/Driver.cpp
ELFSyncStream elf::Log(Ctx &ctx) { return {ctx, DiagLevel::Log}; }
ELFSyncStream elf::Msg(Ctx &ctx) { return {ctx, DiagLevel::Msg}; }
ELFSyncStream elf::Warn(Ctx &ctx) { return {ctx, DiagLevel::Warn}; }
ELFSyncStream elf::Err(Ctx &ctx) {
  return {ctx, ctx.arg.noinhibitExec ? DiagLevel::Warn : DiagLevel::Err};
}
ELFSyncStream elf::ErrAlways(Ctx &ctx) { return {ctx, DiagLevel::Err}; }
ELFSyncStream elf::Fatal(Ctx &ctx) { return {ctx, DiagLevel::Fatal}; }
uint64_t elf::errCount(Ctx &ctx) { return ctx.e.errorCount; }
```

见 [ELF/Driver.cpp:83-91](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L83-L91)（`Err`/`Warn`/`Fatal`/`Msg`/`Log` 工厂函数与 `errCount`）。注意 `Err` 的降级逻辑：`--noinhibit-exec` 时退化为 `Warn`。

它们的声明位于 [ELF/Config.h:819-841](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Config.h#L819-L841)，注释明确说明了各自的语义，例如 `Err` 的注释：「Report an error that will suppress the output file generation. Downgraded to a warning if `--noinhibit-exec` is specified.」

一个真实调用例子（来自 `parseEmulation`）：

```cpp
// ELF/Driver.cpp，parseEmulation 内
if (ret.first == ELFNoneKind)
  ErrAlways(ctx) << "unknown emulation: " << emul;
```

这一行创建临时 `ELFSyncStream`，把 `"unknown emulation: "` 和 `emul` 写进缓冲区，分号结束析构时一次性提交为一条错误。

#### 4.3.4 代码实践

**实践目标**：通过一个可运行命令，亲眼看到 `Msg(ctx)` 流的输出。

**操作步骤**：

1. 在已构建好 LLD 的环境里执行：`ld.lld --version`。
2. 阅读本讲的 `linkerMain` 源码片段（见下），确认 `--version` 是通过 `Msg(ctx) << getLLDVersion() << ...` 输出的。

**需要观察的现象 / 预期结果**：终端应打印类似 `LLD 18.x.x (compatible with GNU linkers)` 的一行，这就是 `Msg(ctx)` 流最终经 `ErrorHandler::message(buf, e.outs())` 写到 `stdoutOS` 的结果。`Msg` 与 `Err` 不同——它写到 `stdout` 而非 `stderr`，且不计入错误数。

对应的源码（`linkerMain` 处理 `-v`/`-version`）：

```cpp
// ELF/Driver.cpp
if (args.hasArg(OPT_v) || args.hasArg(OPT_version))
  Msg(ctx) << getLLDVersion() << " (compatible with GNU linkers)";
```

见 [ELF/Driver.cpp:664-665](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L664-L665)（`linkerMain` 用 `Msg(ctx)` 输出版本字符串）。

> 待本地验证：`ld.lld --version` 的确切输出字符串取决于你构建的 LLVM 版本号，但 `(compatible with GNU linkers)` 后缀是固定的（为了让旧版 Libtool 脚本识别 LLD）。

#### 4.3.5 小练习与答案

**练习 1**：为什么不直接 `llvm::errs() << "error"`，而要绕一圈用 `Err(ctx) << "error"`？

**参考答案**：两个原因。一是线程安全——`llvm::errs()` 不加锁，并行阶段多条诊断会交错撕裂；`SyncStream` 在析构时加 `ErrorHandler::mu` 锁一次性输出，保证每条诊断完整。二是错误计数——`Err` 会自增 `errorCount`，使检查点 `if (errCount(ctx)) return;` 能在合适的阶段终止链接。

**练习 2**：`Err(ctx)` 和 `ErrAlways(ctx)` 的区别是什么？

**参考答案**：`Err(ctx)` 在指定 `--noinhibit-exec` 时会**降级为警告**（级别变 `Warn`，不增加错误计数、不抑制输出）；`ErrAlways(ctx)` 无论何时都是真正的错误级别。需要「即便用户允许带错输出也必须算作错误」的场景（如 `--reproduce` 解析失败）用 `ErrAlways`。

**练习 3**：`Fatal(ctx)` 与 `Err(ctx)` 的使用边界是什么？

**参考答案**：`Fatal` 会**立即终止**链接（`fatal()` 标注了 `[[noreturn]]`），而 `Err` 只报告并继续。`ErrorHandler.h` 注释明确建议：除非是「输入文件损坏、无法继续」这类情形，否则应优先用 `Err`，因为 `Fatal` 只能一次发现一个问题，且不利于把 LLD 当库使用。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「**入口追踪 + 上下文映射**」任务：

**任务**：假设你想回答「一次 ELF 链接里，诊断、配置、符号、输入文件分别存在哪里、由谁创建」。请按下面的步骤完成一张映射表。

1. **入口顺序**：对照 [ELF/Driver.cpp:118-140](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L118-L140)，列出 `link()` 创建对象的顺序（参考 4.1.4 的表格）。

2. **状态归宿**：填空——下面这些链接过程需要的状态，分别挂在 `Ctx` 的哪个成员上？（答案见 [ELF/Config.h:651-787](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Config.h#L651-L787)）

   | 状态 | 归宿（`ctx.xxx`） |
   | --- | --- |
   | 命令行配置（如 `-shared`、`-o`） | `ctx.arg`（`Config`） |
   | 诊断处理器与错误计数 | `ctx.e`（继承自 `CommonLinkerContext`） |
   | 符号表 | `ctx.symtab` |
   | 链接脚本对象指针 | `ctx.script` |
   | 输入目标文件列表 | `ctx.objectFiles` |
   | 聚合后的输入段 | `ctx.inputSections` |
   | 架构后端（X86_64 等） | `ctx.target` |
   | 内存分配器 | `ctx.bAlloc`（继承自 `CommonLinkerContext`） |

3. **诊断路径**：选一个真实调用点，例如 [ELF/Driver.cpp:184](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/ELF/Driver.cpp#L184) 的 `ErrAlways(ctx) << "unknown emulation: "`，画出它从「`<<`」到「`errorCount++`」的完整链路：`ErrAlways(ctx)` → 临时 `ELFSyncStream` → `<<` 写缓冲 → 析构 `~SyncStream` → `e.error(buf)` → 加锁写 `stderrOS` + `errorCount++`。

**预期产出**：一张能解释「`link()` 装配了什么、`Ctx` 装了什么、诊断怎么流出」的完整心智地图。完成它之后，你就为下一讲（u2-l2，`linkerMain` 内部的选项解析与文件加载）打好了地基——因为 `linkerMain` 的每一步，本质上都是在读写 `ctx` 的这些成员，并通过 `Err`/`Warn` 报告问题。

## 6. 本讲小结

- `elf::link()` 是 ELF 后端的薄入口，只负责「装配 + 启动」：建 `Ctx`、初始化 `ErrorHandler`、创建 `LinkerScript`/`SymbolTable`，再调用 `ctx.driver.linkerMain(args)`，最后以 `errCount(ctx) == 0` 作为成功标志。
- `link()` 的五个参数（`args`/`stdoutOS`/`stderrOS`/`exitEarly`/`disableOutput`）让调用方能重定向输出、控制是否快路径退出、是否真正生成输出文件——这正是「LLD 当库」的基础。
- `Ctx` 继承自公共层 `CommonLinkerContext`，同时拥有公共状态（`bAlloc`/`saver`/`e` 等分配器与诊断）和 ELF 状态（`arg`/`driver`/`script`/`symtab`/`objectFiles`/`inputSections` 等），是整个链接过程的「总账本」。
- 把全局状态聚合到堆对象（而非程序级全局变量）解决了「初始化顺序不确定」和「不可重入」两大问题，使 LLD 能在一个进程里被反复调用。
- 诊断流 `ELFSyncStream`（`Err`/`Warn`/`Fatal`/`Msg`/`Log`）借助 RAII：临时对象先写内部缓冲，析构时再加锁一次性提交，既支持 `<<` 流式写法，又保证线程安全并维护错误计数。
- `Err` 在 `--noinhibit-exec` 下会降级为 `Warn`；`ErrAlways` 始终是错误；`Fatal` 会立即终止、应仅用于输入损坏等不可恢复场景。

## 7. 下一步学习建议

本讲只走到 `linkerMain(args)` 这一行的门口。下一讲 **u2-l2「linkerMain：选项解析、配置与文件加载」** 会进入 `linkerMain` 内部，看它如何用 `ELFOptTable` 解析命令行、`readConfigs`/`setConfigs` 填充 `ctx.arg`、`createFiles`/`loadFiles`/`addFile` 把输入文件分类装入 `ctx.objectFiles` 等容器。

进阶阅读建议：

- 想深入理解「LLD 当库」与崩溃恢复，可跳到 **u3-l1（CommonLinkerContext）** 与 **u3-l4（把 LLD 当作库使用）**，并结合 [unittests/AsLibELF/SomeDrivers.cpp](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/unittests/AsLibELF/SomeDrivers.cpp) 看真实的库调用示例。
- 想理解错误处理的检查点机制（`if (errCount(ctx)) return;`），可看 **u3-l2（错误与诊断处理机制）**，它会系统讲解何时该退出。
- 对符号表与符号结构感兴趣，可在学完 u2-l2 后直接进入第四单元（u4-l1 符号结构、u4-l2 符号表）。
