# 单一可执行文件与 flavor 分发机制

## 1. 本讲目标

学完本讲后，你应该能够：

- 说明 LLD 为什么是「一个二进制里塞了四个链接器」，以及这样设计的好处。
- 看懂 `tools/lld/lld.cpp` 里 `lld_main` 的两条执行路径（普通路径 / lit 测试路径），并解释 `inTestVerbosity` 的作用。
- 描述 `argv[0]`（程序名）或 `-flavor` 选项是如何被解析成一个 `Flavor` 枚举值，再被映射到具体后端 `link()` 函数的。
- 理解 `Driver.h` 里的 `LLD_HAS_DRIVER` / `LLD_ALL_DRIVERS` 宏如何把四个后端「注册」成一张分发表，以及 `Result{retCode, canRunAgain}` 的含义。

本讲是单元一的收尾，承接 u1-l3 的「目录结构与多后端源码组织」：你已经知道 LLD 有四个后端目录，本讲回答「四个后端是怎么塞进同一个可执行文件、又怎么知道该调用哪一个的」。

## 2. 前置知识

在阅读本讲前，建议你先了解以下概念：

- **`argv[0]` 与程序名**：在 C/C++ 的 `main(int argc, char **argv)` 里，`argv[0]` 通常是程序被调用时使用的名字。同一个可执行文件如果通过不同的名字（通常是符号链接）被调用，`argv[0]` 就会不同。LLD 正是利用这一点来区分要调用哪个后端。
- **函数指针**：C/C++ 里可以把一个函数的地址存进变量，之后通过这个变量「间接调用」该函数。LLD 用函数指针表实现「分发表」。
- **`enum`（枚举）**：一组带名字的整数常量。本讲里 `Flavor` 枚举用来标记「当前要调用 GNU/WinLink/Darwin/Wasm 哪一种风格的链接器」。
- **崩溃恢复（Crash Recovery）**：LLVM 提供的 `CrashRecoveryContext` 机制，可以把一段代码包起来，即使这段代码里调用了 `fatal()`（内部用异常或 `setjmp/longjmp` 跳转）甚至发生了段错误，外层也能「接住」并继续运行，而不是整个进程崩溃。
- **lit 测试**：LLVM 的回归测试框架。LLD 的 `test/` 目录下大量使用 lit + FileCheck 做端到端测试（详见 u1-l2、u9-l1）。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `tools/lld/lld.cpp` | 整个 `lld` 可执行文件的真正入口 `lld_main`。它判断当前是否在 lit 测试中，决定走「快速但不可重入」的路径还是「可重入 + 崩溃恢复」的路径，最终都把工作交给 `unsafeLldMain`。 |
| `include/lld/Common/Driver.h` | 定义 `Flavor` 枚举、`Driver` 函数指针类型、`DriverDef`/`Result` 结构体、公共库入口 `lldMain()` 声明，以及两个注册宏 `LLD_HAS_DRIVER` / `LLD_ALL_DRIVERS`。 |
| `Common/DriverDispatcher.cpp` | 分发的核心逻辑：从 `argv[0]` 或 `-flavor` 解析出 `Flavor`（`getFlavor` / `parseProgname` / `parseFlavor`），在驱动表里查到对应函数指针（`whichDriver`），并提供 `unsafeLldMain` 与 `lldMain` 两个入口。 |
| `include/lld/Common/ErrorHandler.h` | 声明 `[[noreturn]] void exitLld(int val);`，本讲涉及的「快速退出」函数。 |
| `Common/ErrorHandler.cpp` | `exitLld` 的实现：清理临时文件、必要时把崩溃重新抛出、调用 `_exit`。 |

> 提示：本讲引用的源码行号基于 HEAD `8bdbeac21eccd679489614e0326ab398425d47f1`。每段代码都附有永久链接，可直接点击核对。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **模块一：`lld.cpp` 主函数与崩溃恢复** —— 谁是程序入口，为什么有两条路径，`inTestVerbosity` 干什么用。
2. **模块二：`DriverDispatcher` 的 flavor 解析** —— `argv[0]` 或 `-flavor` 是怎么变成一个 `Flavor`、又是怎么从 PE 目标推导出 MinGW 的。
3. **模块三：`Driver.h` 的注册宏与 `Result`** —— 四个后端的 `link()` 如何被登记进一张表、`lldMain` 如何用崩溃恢复把这张表安全地驱动起来。

---

### 4.1 模块一：`lld.cpp` 主函数与崩溃恢复

#### 4.1.1 概念说明

在 u1-l3 里我们看到，LLD 有 ELF、COFF、Mach-O、WebAssembly 四个后端，外加一个 MinGW 薄包装。但用户拿到的只是一个可执行文件，名字通常叫 `lld` 或它的某个符号链接（`ld.lld`、`lld-link`、`ld64.lld`、`wasm-ld`）。

那么「谁来决定调用哪个后端」？答案在 `tools/lld/lld.cpp`。这个文件是整个 `lld` 可执行文件的「大门」，但它本身几乎不做链接工作——它只负责：

1. 判断当前是不是在 lit 测试环境里。
2. 据此选择「快速路径」或「可重入路径」。
3. 把命令行参数交给分发器（`unsafeLldMain`），由分发器再路由到具体后端。

这里有一个关键概念：**可重入性（re-entrancy）**。普通命令行使用时，LLD 链接完就直接进程退出，不需要清理内存——操作系统会回收。但如果把 LLD 当作库（详见 u3-l4）反复调用，就必须在每次调用后把全局状态彻底清理干净，否则下一次调用会读到上一次的残留数据。lit 测试也要求这种「反复调用」能力，所以 lit 路径和库路径走的是同一套「可重入 + 内存清理」逻辑。

#### 4.1.2 核心流程

`lld_main` 的执行流程可以用下面这段伪代码概括：

```
lld_main(argc, argv):
    打开 ANSI 颜色输出
    如果环境变量 FORCE_LLD_DIAGNOSTICS_CRASH 被设置 -> 故意触发崩溃（测试诊断用）

    如果 不是 lit 测试（inTestVerbosity() == 0）:
        直接调用 unsafeLldMain(..., exitEarly=true)
        return                    # 走最快路径，不清理内存

    # 是 lit 测试：开启崩溃恢复，连跑 inTestVerbosity() 遍
    开启 CrashRecoveryContext
    对 i 从 inTestVerbosity() 到 1:
        除最后一遍外，关闭输出（inTestOutputDisabled = true）
        r = lldMain(args, ...)     # 可重入入口
        如果 不能再跑(r.canRunAgain == false) -> 立即 exitLld
        如果 多次运行结果不一致 -> 立即返回（让测试失败）
    return 第一次的结果
```

两个要点：

- **普通路径**用 `unsafeLldMain(..., exitEarly=true)`。`exitEarly=true` 意味着链接完成后会调用 `exitLld()` 直接走 `_exit`，跳过析构函数——这比正常退出更快，也避免了析构期间的竞态。
- **测试路径**连跑多遍（由环境变量 `LLD_IN_TEST` 控制遍数），目的有两个：一是验证 LLD 真的可以被反复调用而状态不泄漏；二是把输出关到只剩最后一遍，避免重复打印。多次结果不一致就直接返回失败码，保证测试的可靠性。

#### 4.1.3 源码精读

先看入口函数本体。注意它的签名是 `lld_main(int argc, char **argv, const llvm::ToolContext &)`——这是 LLVM 的统一驱动入口约定，由 LLVM 的命令行基础设施调用：

这段代码先设置颜色输出，并提供了一个「故意崩溃」的开关，用于测试错误处理路径本身：[tools/lld/lld.cpp:75-82](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/tools/lld/lld.cpp#L75-L82)（`lld_main` 入口与 `FORCE_LLD_DIAGNOSTICS_CRASH` 测试钩子）。

`inTestVerbosity()` 读取环境变量 `LLD_IN_TEST`，返回 0 表示「不在测试中」，返回正整数 N 表示「连跑 N 遍」：[tools/lld/lld.cpp:63-67](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/tools/lld/lld.cpp#L63-L67)（`inTestVerbosity`：非测试时返回 0，从而让 LLD 在退出时不释放内存以加速进程销毁）。

非测试时走最快路径，调用 `unsafeLldMain` 并传入 `exitEarly=true`：[tools/lld/lld.cpp:88-93](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/tools/lld/lld.cpp#L88-L93)（注释明确：不在 lit 中时，采用全局异常处理且退出时不做内存清理）。

测试时则开启崩溃恢复、连跑多遍，并比较每遍结果是否一致：[tools/lld/lld.cpp:95-114](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/tools/lld/lld.cpp#L95-L114)（关闭非最后一遍的输出、调用可重入的 `lldMain`、结果不一致即提前返回）。

注意 `lld.cpp` 顶部这一组宏——它们声明了五个后端的 `link()` 函数，是把「表」和「函数」连接起来的关键（模块三会展开）：[tools/lld/lld.cpp:69-73](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/tools/lld/lld.cpp#L69-L73)（用 `LLD_HAS_DRIVER` 声明 coff/elf/mingw/macho/wasm 五个后端）。

#### 4.1.4 代码实践

**实践目标**：直观感受「普通路径 vs 测试路径」的差异，并理解 `LLD_IN_TEST` 的作用。

**操作步骤**：

1. 假设你已经按 u1-l2 构建出了 `lld` 可执行文件（或系统里已安装 `lld`）。
2. 用 `lld --version` 正常运行一次，观察它会打印出哪个后端的版本信息（注意：`--version` 的解释本身也依赖分发，见模块二的实践）。
3. 设置环境变量强制多跑一遍，再运行：
   ```bash
   LLD_IN_TEST=2 lld -flavor gnu --version
   ```
4. 故意触发崩溃诊断钩子（**仅供观察，会让进程立即崩溃，不要在生产环境用**）：
   ```bash
   FORCE_LLD_DIAGNOSTICS_CRASH=1 lld --version
   ```

**需要观察的现象**：

- 第 3 步应当和普通运行产生**完全一致**的输出（因为非最后一遍的输出被关掉了，且结果一致才会正常返回）。
- 第 4 步会打印一行 `crashing due to environment variable FORCE_LLD_DIAGNOSTICS_CRASH` 后进程崩溃。

**预期结果**：第 3 步正常输出 ELF 链接器的版本；第 4 步以非零状态退出（段错误/陷阱）。

> 待本地验证：如果你没有现成的 `lld`，需要先按 u1-l2 完成 `LLVM_ENABLE_PROJECTS=lld` 的构建。不同发行版/构建下 `LLD_IN_TEST` 的实际遍数解析行为以本机为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么普通命令行使用 LLD 时，不在退出前清理内存？

**参考答案**：因为进程退出时操作系统会自动回收所有内存，主动清理只会拖慢退出速度、还可能在析构期间引入竞态。所以非测试路径用 `exitEarly=true`，直接 `_exit` 跳过析构。只有在「把 LLD 当库反复调用」或 lit 测试场景里，才需要彻底清理以保证可重入。

**练习 2**：`LLD_IN_TEST=2` 和 `LLD_IN_TEST=3` 在输出上会有什么区别？

**参考答案**：从用户可见输出来看没有区别——无论连跑几遍，只有最后一遍会真正输出，其余各遍的输出被 `inTestOutputDisabled` 关掉。连跑的目的是验证可重入性和结果一致性，而不是产生多份输出。

---

### 4.2 模块二：`DriverDispatcher` 的 flavor 解析

#### 4.2.1 概念说明

`lld_main` 把工作交给了 `unsafeLldMain`，而 `unsafeLldMain`（定义在 `Common/DriverDispatcher.cpp`）要做的第一件事就是：**搞清楚该调用哪个后端**。

判断依据有两个，按优先级：

1. **`-flavor` 选项**：如果命令行里显式写了 `-flavor gnu` / `-flavor link` / `-flavor darwin` / `-flavor wasm`，就用它。这是「显式指定」，主要是为了向后兼容，源码注释明确说「不推荐」（not recommended）。
2. **`argv[0]`（程序名）**：如果没有 `-flavor`，就根据被调用时的名字推断。比如名字是 `ld.lld` 就推断为 ELF，`lld-link` 就推断为 COFF，`wasm-ld` 就推断为 WebAssembly，`ld64.lld` 就推断为 Mach-O。

此外还有一个**二级推断**：如果推断出来是 GNU 风格，但命令行里的目标（`-m` 参数）是 PE 目标（如 `i386pe`、`i386pep` 等 Windows 格式），就会把 flavor 从 `Gnu` 改成 `MinGW`，从而走 MinGW 包装层（详见 u8-l3）。

#### 4.2.2 核心流程

flavor 解析的核心链路如下（函数都在 `Common/DriverDispatcher.cpp`）：

```
whichDriver(args, drivers):          # 在驱动表里查函数指针
    f = parseFlavor(args)
    在 drivers 表里找 driverdef.f == f 的那一项
    找到 -> 返回它的函数指针 d
    没找到 -> 返回一个「永远返回 false」的桩函数（表示该后端未编入）

parseFlavor(args):
    f = parseFlavorWithoutMinGW(args)    # 先按 -flavor 或 argv[0] 解析
    如果 f == Gnu:
        如果 isPETarget(args) 为真 -> 返回 MinGW   # PE 目标改走 MinGW
    返回 f

parseFlavorWithoutMinGW(args):
    如果 args[1] == "-flavor":
        f = getFlavor(args[2])
        从 args 中删掉 "-flavor" 和它的值
        返回 f
    否则:
        progname = 取 args[0] 的文件名部分（去掉路径和 .exe 后缀）
        返回 parseProgname(progname)

getFlavor(s):   # 一个 StringSwitch，把名字映射成 Flavor
    "ld"/"ld.lld"/"gnu"            -> Gnu
    "wasm"/"ld-wasm"               -> Wasm
    "link"                         -> WinLink
    "ld64"/"ld64.lld"/"darwin"     -> Darwin
    其它                           -> Invalid
```

`whichDriver` 是「解析 + 查表」的合体。注意它如果没找到匹配项，不会崩溃，而是返回一个永远返回 `false` 的 lambda——这表示「该后端没有被编入这个二进制」（例如用 `LLD_ALL_DRIVERS` 之外的子集构建时）。

`isPETarget` 的逻辑值得单独说一下：它会先在原始参数里找 `-m`，找不到就**展开 `@responsefile` 响应文件**再找一次（因为 `-m i386pe` 可能藏在响应文件里），最后再回退到编译期宏 `LLD_DEFAULT_LD_LLD_IS_MINGW`。

#### 4.2.3 源码精读

`getFlavor` 用 `StringSwitch` 把一组名字映射成 `Flavor`，大小写不敏感（`CasesLower`）：[Common/DriverDispatcher.cpp:31-38](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/Common/DriverDispatcher.cpp#L31-L38)（`ld`、`ld.lld`、`gnu` 都映射到 `Gnu` 等）。

`parseProgname` 把程序名按 `-` 切开逐段匹配，并对裸 `ld` 默认成 `Gnu`：[Common/DriverDispatcher.cpp:83-95](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/Common/DriverDispatcher.cpp#L83-L95)（这样 `lld-link`、`wasm-ld`、`ld64.lld` 都能被正确切出 `link`/`wasm`/`ld64` 并命中）。

`parseFlavorWithoutMinGW` 先看 `-flavor`，否则用 `argv[0]` 推断；推断失败时会打印那句著名的提示信息：[Common/DriverDispatcher.cpp:97-125](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/Common/DriverDispatcher.cpp#L97-L125)（提示用户改用 `ld.lld`/`ld64.lld`/`lld-link`/`wasm-ld` 调用）。

`parseFlavor` 在 `Gnu` 的基础上叠加 PE/MinGW 判定：[Common/DriverDispatcher.cpp:127-137](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/Common/DriverDispatcher.cpp#L127-L137)（`Gnu` 且是 PE 目标则升级为 `MinGW`）。

`isPETarget` 查找 `-m` 参数，必要时展开响应文件，最后用编译期宏兜底：[Common/DriverDispatcher.cpp:51-81](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/Common/DriverDispatcher.cpp#L51-L81)（`isPETargetName` 列出全部支持的 PE 目标名：`i386pe`、`i386pep`、`thumb2pe`、`arm64pe`、`arm64ecpe`、`arm64xpe`、`mipspe`，见 [Common/DriverDispatcher.cpp:46-49](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/Common/DriverDispatcher.cpp#L46-L49)）。

`whichDriver` 解析出 flavor 后在驱动表里查函数指针，查不到就返回「失败桩」：[Common/DriverDispatcher.cpp:139-150](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/Common/DriverDispatcher.cpp#L139-L150)（没匹配项时返回的 lambda 恒返回 `false`，对应「后端未编入」）。

最后看分发落地的 `unsafeLldMain`：它调用 `whichDriver` 拿到函数指针，执行它，再按 `exitEarly` 决定是否立即 `exitLld`，最后销毁全局上下文：[Common/DriverDispatcher.cpp:157-175](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/Common/DriverDispatcher.cpp#L157-L175)（注意 `r = !d(...)`：后端 `link()` 返回 `true` 表示成功，这里取反转成「0 表示成功」的进程退出码惯例）。

#### 4.2.4 代码实践

**实践目标**：用同一个 `lld` 可执行文件，通过不同名字（符号链接）触发不同后端；再手动跟踪 `lld -flavor gnu` 的完整解析过程。

**操作步骤**：

1. 准备一个工作目录，假设你已有 `lld` 的路径记作 `$LLD`：
   ```bash
   mkdir -p /tmp/flavor-demo && cd /tmp/flavor-demo
   ln -sf "$LLD" ld.lld
   ln -sf "$LLD" lld-link
   ln -sf "$LLD" wasm-ld
   ln -sf "$LLD" ld64.lld
   ```
2. 依次运行，观察 `--version` 输出里报告的是哪个链接器：
   ```bash
   ./ld.lld    --version    # 预期：LLD 的 ELF 后端（"LLD X.Y.Z" / "compatible with GNU linkers"）
   ./lld-link  --version    # 预期：COFF 后端（"LLD X.Y.Z" / "compatible with Microsoft link.exe"）
   ./wasm-ld   --version    # 预期：WebAssembly 后端
   ./ld64.lld  --version    # 预期：Mach-O 后端（"compatible with Darwin ld"）
   ```
3. 直接用裸名 + `-flavor` 显式指定后端：
   ```bash
   lld -flavor gnu    --version
   lld -flavor link   --version
   lld -flavor darwin --version
   lld -flavor wasm   --version
   ```
4. **手动跟踪 `lld -flavor gnu --version` 的解析过程**（这是本实践的核心，参照模块二的伪代码和上面的源码行号）：
   - `lld_main` 收到 `argv = {"lld", "-flavor", "gnu", "--version"}`，不在测试中 → 调 `unsafeLldMain(..., LLD_ALL_DRIVERS, exitEarly=true)`。
   - `unsafeLldMain` 调 `whichDriver` → `parseFlavor` → `parseFlavorWithoutMinGW`。
   - `parseFlavorWithoutMinGW` 发现 `args[1] == "-flavor"`，于是 `getFlavor("gnu")` 返回 `Gnu`，并从参数里**删掉** `-flavor` 和 `gnu` 两项，剩下的 `args = {"lld", "--version"}`。
   - 回到 `parseFlavor`：`f == Gnu`，于是调 `isPETarget(args)`。参数里没有 `-m`，响应文件也没有，回退到 `LLD_DEFAULT_LD_LLD_IS_MINGW`（默认未定义 → `false`），所以保持 `Gnu`。
   - `whichDriver` 在 `LLD_ALL_DRIVERS` 表里找 `f == Gnu` 的项 → 命中 `{lld::Gnu, &lld::elf::link}`。
   - `unsafeLldMain` 执行 `lld::elf::link({"lld", "--version"}, ...)`，由 ELF 后端打印版本信息。
   - 返回值取反成退出码，`exitEarly=true` → 调 `exitLld(0)`。

**需要观察的现象**：第 2、3 步的四个命令应分别打印出 ELF / COFF / Mach-O / WebAssembly 四种后端的版本描述。

**预期结果**：四个名字（或四种 `-flavor`）能稳定地分发到四个不同后端，证明分发只取决于「名字 / `-flavor`」，与二进制内容无关——因为它们指向的是**同一个** `lld`。

> 待本地验证：具体版本号和「compatible with …」字样以你本机构建的 LLD 为准。跨平台运行时，某些后端（如 `ld64.lld` 在非 macOS 上的 Mach-O 链接产物行为）以本机工具链为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `parseProgname` 要把程序名按 `-` 切成多段再逐段匹配，而不是整体匹配？

**参考答案**：因为实际程序名形如 `lld-link`、`wasm-ld`、`ld64.lld`、`ld.lld`，它们的「风格关键词」（`link`、`wasm`、`ld64`、`ld`）被各种前缀/后缀包围。按 `-` 切开后逐段用 `getFlavor` 匹配，可以一次兼容所有命名变体，比枚举每一种完整名字更简洁健壮。

**练习 2**：如果用户运行 `ld -m i386pep ...`（程序名是 `ld`，但目标是 64 位 PE），LLD 最终会走哪个后端？

**参考答案**：`parseProgname("ld")` 先返回 `Gnu`；随后 `parseFlavor` 检测到 `isPETarget` 为真（`i386pep` 命中 `isPETargetName`），于是把 flavor 从 `Gnu` 改成 `MinGW`，最终走的是 MinGW 后端（它内部再委托给 COFF 后端，详见 u8-l3）。这正是 LLD 能在 Windows/MinGW 环境下用 `ld` 名字工作的原因。

---

### 4.3 模块三：`Driver.h` 的注册宏与 `Result`

#### 4.3.1 概念说明

模块二里反复提到的「驱动表 `LLD_ALL_DRIVERS`」和「后端 `link()` 函数」都来自头文件 `include/lld/Common/Driver.h`。这个头文件是 LLD「作为库」的公共契约，它定义了：

- **`Flavor` 枚举**：五种风格（`Invalid`/`Gnu`/`MinGW`/`WinLink`/`Darwin`/`Wasm`）。
- **`Driver` 类型**：一个统一的函数指针签名，所有后端的 `link()` 都长成这个样子。
- **`DriverDef` 结构**：把一个 `Flavor` 和一个 `Driver` 绑在一起，构成表里的一项。
- **`Result` 结构**：`lldMain()` 的返回值，含 `retCode` 和 `canRunAgain`。
- **`lldMain()` 声明**：把 LLD 当库时调用的安全入口。
- **两个宏**：`LLD_HAS_DRIVER(name)` 声明某后端的 `link()`；`LLD_ALL_DRIVERS` 构造一张包含全部后端的表。

理解这两个宏是理解「四个后端如何被装进一个二进制」的关键。它们让「后端实现」和「分发逻辑」彻底解耦：后端各自在自己的目录里实现 `bool link(...)`，分发器只通过函数指针和一张 `{Flavor, 函数指针}` 的表来调用，根本不需要 `#include` 任何后端的头文件。

#### 4.3.2 核心流程

注册与分发的协作关系：

```
            ┌──────────── include/lld/Common/Driver.h ────────────┐
            │  enum Flavor { Gnu, MinGW, WinLink, Darwin, Wasm }   │
            │  using Driver = bool(*)(args, out, err, exitEarly,   │
            │                          disableOutput);             │
            │  struct DriverDef { Flavor f; Driver d; };           │
            │  struct Result { int retCode; bool canRunAgain; };   │
            │                                                       │
            │  #define LLD_HAS_DRIVER(name)  声明 name::link        │
            │  #define LLD_ALL_DRIVERS       { {Flavor, &link}... } │
            └───────────────────────────────────────────────────────┘
                                   │
   tools/lld/lld.cpp 用 LLD_HAS_DRIVER 声明全部 5 个后端的 link()
   并把 LLD_ALL_DRIVERS 当作 drivers 参数传下去
                                   │
                                   ▼
   unsafeLldMain / lldMain (DriverDispatcher.cpp)
          │  whichDriver 在表里查到 &lld::elf::link 等
          ▼
   具体后端 link() (ELF/Driver.cpp、COFF/Driver.cpp、...)
```

`LLD_ALL_DRIVERS` 展开后就是这样一个数组字面量（这正是模块二里 `whichDriver` 用来查找的表）：

```cpp
{
  {lld::WinLink, &lld::coff::link},
  {lld::Gnu,     &lld::elf::link},
  {lld::MinGW,   &lld::mingw::link},
  {lld::Darwin,  &lld::macho::link},
  {lld::Wasm,    &lld::wasm::link}
}
```

而 `LLD_HAS_DRIVER(name)` 展开后，会声明 `namespace lld::name { bool link(...); }`——注意只是**声明**，真正的定义在各后端的 `Driver.cpp` 里。`lld.cpp` 顶部连写五行 `LLD_HAS_DRIVER(...)`，就是在为这张表里用到的五个 `link()` 函数做前置声明。

#### 4.3.3 源码精读

`Flavor` 枚举和三种核心类型（`Driver` / `DriverDef` / `Result`）的定义：[include/lld/Common/Driver.h:16-36](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/include/lld/Common/Driver.h#L16-L36)（注意 `Result` 有两个字段：`retCode` 和 `canRunAgain`，后者告诉调用方「内存是否已被污染、还能不能再调一次」）。

`lldMain` 的声明和它详细的「可重入 + 崩溃恢复」契约注释：[include/lld/Common/Driver.h:38-45](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/include/lld/Common/Driver.h#L38-L45)（注释说明：崩溃可能损坏内存导致不能再入，此时应调 `exitLld()` 正常退出）。

`LLD_HAS_DRIVER` 宏的定义——库用户必须用它声明要链接的后端：[include/lld/Common/Driver.h:48-57](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/include/lld/Common/Driver.h#L48-L57)（展开后是 `namespace lld::name { bool link(...); }` 的前置声明）。

`LLD_ALL_DRIVERS` 宏——一张把全部 flavor 绑到对应 `link()` 的表：[include/lld/Common/Driver.h:60-67](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/include/lld/Common/Driver.h#L60-L67)（这张表正是 `whichDriver` 查找的对象）。

`lldMain` 的实现：用 `CrashRecoveryContext::RunSafely` 把 `unsafeLldMain` 包起来，崩溃则返回 `{RetCode, canRunAgain=false}`；正常结束再安全地销毁上下文，返回 `{r, canRunAgain=true}`：[Common/DriverDispatcher.cpp:178-202](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/Common/DriverDispatcher.cpp#L178-L202)（这就是「可重入」语义的实现——崩溃恢复负责接住 `fatal()` 引发的跳转和段错误）。

`exitLld` 的声明（`[[noreturn]]`，表示它永不返回）：[include/lld/Common/ErrorHandler.h:173](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/include/lld/Common/ErrorHandler.h#L173)。其实现负责丢弃临时文件、必要时把崩溃重新抛出、最终 `_exit`：[Common/ErrorHandler.cpp:83-93](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/Common/ErrorHandler.cpp#L83-L93)（开头部分：删临时文件、`CrashRecoveryContext::throwIfCrash(val)`）。

#### 4.3.4 代码实践

**实践目标**：把 LLD 当作一个 C++ 库来调用，亲手体会 `lldMain` 的 `Result{retCode, canRunAgain}` 和 `LLD_ALL_DRIVERS` / `LLD_HAS_DRIVER` 的用法。

**操作步骤**：

1. 阅读官方的「作为库」单元测试，它就是最好的最小示例：[unittests/AsLibELF/SomeDrivers.cpp](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/unittests/AsLibELF/SomeDrivers.cpp)（这个测试演示了「只链接部分后端」的用法）。
2. 理解它的套路：
   - 用 `LLD_HAS_DRIVER(elf)` 只声明 ELF 后端（不声明 coff/macho/wasm，于是这些后端不会被链接进测试程序）。
   - 自己构造一个**只含 ELF** 的 `DriverDef` 数组（而不是用包含全部后端的 `LLD_ALL_DRIVERS`）。
   - 调用 `lldMain(args, outs(), errs(), drivers)`，检查返回的 `Result.retCode`。
3.（可选，需自行搭建编译环境）写一个最小 `main.cpp`，仿照上面的测试，只链接 ELF 驱动，传入 `{"lld", "--version"}`，打印 `r.retCode` 与 `r.canRunAgain`：
   ```cpp
   // 示例代码（非项目原有文件，需自行与 lldELF 库一起编译链接）
   #include "lld/Common/Driver.h"
   #include "llvm/Support/raw_ostream.h"
   LLD_HAS_DRIVER(elf)
   int main() {
     const char *args[] = {"lld", "--version"};
     lld::DriverDef drivers[] = {{lld::Gnu, &lld::elf::link}};
     lld::Result r = lld::lldMain(args, llvm::outs(), llvm::errs(), drivers);
     llvm::outs() << "retCode=" << r.retCode
                  << " canRunAgain=" << r.canRunAgain << "\n";
     return r.retCode;
   }
   ```

**需要观察的现象**：

- `lldMain` 正常返回时 `canRunAgain == true`、`retCode == 0`。
- 如果尝试在 `drivers` 数组里塞一个**未声明**（没写对应 `LLD_HAS_DRIVER`）的后端，链接期就会报「未定义符号」——这印证了宏的「声明 + 链接」双重作用。
- 如果把 `drivers` 数组里某个 flavor 改成「表里没有的」（例如故意只给 ELF，却传入 `{"lld", "-flavor", "link", "--version"}`），`whichDriver` 会返回失败桩，`link()` 返回 `false`，于是 `retCode == 1`。

**预期结果**：`--version` 成功打印版本，`retCode=0 canRunAgain=1`。

> 待本地验证：作为库使用涉及 CMake 链接 `lldELF` 等库目标的具体配置，请以 `unittests/AsLibELF/CMakeLists.txt` 和 `unittests/AsLibAll/` 下的构建脚本为准。这部分会在 u3-l4「把 LLD 当作库使用」中系统讲解。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `lld.cpp` 里要同时写 `LLD_HAS_DRIVER(elf)` 等**五个**声明，又用 `LLD_ALL_DRIVERS`？两者各起什么作用？

**参考答案**：`LLD_HAS_DRIVER(name)` 是**声明**——它告诉编译器「存在一个 `lld::name::link` 函数」，这样 `LLD_ALL_DRIVERS` 表里取地址 `&lld::elf::link` 时才能通过编译；而这些函数的**定义**在各自后端的 `Driver.cpp` 里，最终在链接期被链进来。`LLD_ALL_DRIVERS` 则把这些声明好的函数地址连同 flavor 组成一张表，交给分发器去查。简言之：宏负责「声明」，表负责「登记」，后端 `.cpp` 负责「实现」。

**练习 2**：`Result.canRunAgain` 在什么情况下会是 `false`？调用方这时该做什么？

**参考答案**：当本次调用发生了崩溃（被 `CrashRecoveryContext` 接住），内存可能已被污染，`lldMain` 就返回 `canRunAgain=false`。调用方此时不应再调用 LLD，而应按 `Driver.h` 注释的建议调用 `exitLld()` 正常退出整个进程，以避免退出阶段因为操作损坏的内存而发生间歇性崩溃。`lld.cpp` 的测试路径正是这么做的：`if (!r.canRunAgain) exitLld(r.retCode);`。

## 5. 综合实践

把本讲的三个模块串起来，完成下面这个「全链路跟踪」任务：

1. **准备**：按模块二的方法，为一个 `lld` 可执行文件建立四个符号链接（`ld.lld`、`lld-link`、`ld64.lld`、`wasm-ld`）。
2. **运行**：分别运行四个链接的 `--version`，确认它们分发到了四个不同后端。
3. **画图**：画出从「用户在 shell 输入 `./wasm-ld --version`」到「`lld::wasm::link` 被执行」的完整调用链，标出每一步发生在哪个文件、哪个函数。预期涉及的节点至少包括：`lld_main`（lld.cpp）→ `unsafeLldMain`（DriverDispatcher.cpp）→ `whichDriver` → `parseFlavor` → `parseFlavorWithoutMinGW` → `parseProgname` → `getFlavor("wasm")` → 在 `LLD_ALL_DRIVERS` 表里查到 `{lld::Wasm, &lld::wasm::link}` → 调用 `lld::wasm::link`。
4. **对比**：再对 `lld -flavor darwin --version` 做同样的跟踪，指出它和第 3 步在 `parseFlavorWithoutMinGW` 这一步的差异（一个走 `argv[0]` 分支，一个走 `-flavor` 分支）。
5. **思考题**（不必运行，口头回答）：如果有人用 `-DLLVM_ENABLE_PROJECTS=clang` 之外的方式构建了一个**只包含 ELF 后端**的 `lld`（即不链 `lldCOFF`/`lldMachO`/`lldWasm` 库），那么 `./lld-link --version` 会发生什么？请用 `whichDriver` 返回「失败桩」的机制来解释。

> 提示：第 5 步的答案藏在 [Common/DriverDispatcher.cpp:144-148](https://github.com/llvm/llvm-project/blob/8bdbeac21eccd679489614e0326ab398425d47f1/lld/Common/DriverDispatcher.cpp#L144-L148)——如果构建时仍用 `LLD_ALL_DRIVERS`（表里有 coff），但 `&lld::coff::link` 未被链入，会出现链接错误；而若驱动表本身只含 ELF，则 `whichDriver` 查不到 `WinLink`，返回失败桩，`link()` 返回 `false`，最终 `retCode=1`。这正是宏机制给「裁剪后端」提供的灵活性。

## 6. 本讲小结

- LLD 是**一个**可执行文件，内含 ELF、COFF、Mach-O、WebAssembly（加 MinGW 包装）多个后端；入口 `lld_main` 在 `tools/lld/lld.cpp`。
- `lld_main` 有两条路径：非测试时走 `unsafeLldMain(..., exitEarly=true)` 的快路径、不清理内存；lit 测试时开启崩溃恢复、连跑 `LLD_IN_TEST` 遍并校验结果一致性。
- 分发依据是 `-flavor` 选项（优先）或 `argv[0]`（回退），由 `parseFlavorWithoutMinGW` / `parseProgname` / `getFlavor` 解析成 `Flavor` 枚举。
- 当解析为 `Gnu` 且目标是 PE（`-m i386pe*` 等）时，flavor 会被升级为 `MinGW`，由 `isPETarget` 判定（必要时展开响应文件）。
- `Driver.h` 的 `LLD_HAS_DRIVER` 宏声明后端 `link()`、`LLD_ALL_DRIVERS` 宏把它们登记成一张 `{Flavor, 函数指针}` 表，`whichDriver` 查表分发——这让后端实现与分发逻辑彻底解耦。
- `lldMain` 用 `CrashRecoveryContext` 包住 `unsafeLldMain`，返回 `Result{retCode, canRunAgain}`；`canRunAgain=false` 时应调 `exitLld()` 退出。

## 7. 下一步学习建议

到这里，你已经看清了 LLD 的「外壳」：一个二进制、一条入口、一张分发表。接下来的学习建议：

- **进入 ELF 主线**：从 u2-l1「`link()` 入口与 `Ctx` 上下文对象」开始，沿着 `lld::elf::link` → `LinkerDriver::linkerMain` → `link<ELFT>` → `Writer` 这条主线，把一个 ELF 链接的全过程走通。这是手册第二单元的核心。
- **横向对比其他后端**：如果你更关心 Windows/macOS，可以跳到 u8 单元看 COFF、Mach-O、wasm、MinGW 后端是如何复用同一套「Driver + Writer」设计图的。
- **公共基础设施**：若你想先搞懂「`exitLld`、`CommonLinkerContext`、内存分配」这些被所有后端共享的底层机制，可先读 u3 单元，尤其是 u3-l1（`CommonLinkerContext`）和 u3-l4（把 LLD 当库，与本讲模块三紧密相关）。
- **延伸阅读**：本讲多次提到的「作为库」用法，可对照源码目录 `unittests/AsLibELF/` 与 `unittests/AsLibAll/` 里的真实用例反复体会。
