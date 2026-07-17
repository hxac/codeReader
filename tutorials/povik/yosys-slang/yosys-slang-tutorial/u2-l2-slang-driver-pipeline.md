# slang 驱动流水线：解析、编译与精化

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 [`slang::driver::Driver`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3683) 这个对象里都装了什么（`cmdLine` / `diagEngine` / `sourceManager` / `sourceLoader` / `options`），以及 sv-elab 为什么「复用」它而不是自己写一套解析器。
- 按真实顺序讲出 slang driver 的处理链：`addStandardArgs` → `parseCommandLine` → `fixup_options` → `processOptions` → `catch_forbidden_options` → `parseAllSources` → `createCompilation` → `reportCompilation`，并说明 sv-elab 在哪两步之间「插手」注入综合专用的默认值。
- 解释 `parseAllSources` 与 `createCompilation` 的分工：前者做词法/语法分析产出语法树，后者做语义分析/精化产出 [`ast::Compilation`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3720)。
- 画出「slang 侧产出 → sv-elab 侧消费」的数据流：从 [`compilation->getRoot().topInstances`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3757) 取出顶层实例，经 `get_instance_body` 拿到实例体，交给 `PopulateNetlist` 翻译。

本讲承接 u2-l1。u2-l1 把 [`SlangFrontend::execute`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3674) 切成「参数装配 → 解析源码 → 创建 Compilation → 遍历 PopulateNetlist → 调用 proc」四段骨架；本讲把放大镜对准中间那两段半——**slang driver 到底替 sv-elab 做了什么、产出了什么、sv-elab 又从哪里接手**。选项细节（`SynthesisSettings`）留给 u2-l3，诊断细节留给 u2-l4。

## 2. 前置知识

阅读本讲前，建议你已具备（来自 u1-l1、u2-l1）：

- sv-elab 的职责是把 SystemVerilog 精化成 Yosys RTLIL 字级网表；**词法/语法/语义分析交给 slang**，本仓库只做「AST → RTLIL」的翻译。
- `read_slang` 是一个 Yosys `Frontend`，其 `execute(f, filename, args, design)` 是端到端主函数；它故意丢弃 Yosys 传入的输入流 `(void) f; (void) filename;`，改由 slang driver 自己读源码（这样才能支持 heredoc、多文件、`-D` 等）。

再加一点关于「编译器前端」的通用直觉：

- **词法分析（lex）** 把字符流切成 token；**语法分析（parse）** 把 token 组织成**语法树（SyntaxTree）**，这一步只关心「形式上合不合法」。
- **语义分析（semantic analysis）** 在语法树之上做类型检查、查找符号、求值参数、展开 `generate`、实例化模块层次，得到一棵带完整语义的 **AST**。slang 里这棵 AST 的「总容器」就叫 `Compilation`。
- **精化（elaboration）** 通常指从参数化的模块定义「长出」确定的实例层次的过程；slang 在 `createCompilation` 阶段就把这件事做完了，所以 sv-elab 拿到的 `Compilation` 已经是一棵展开好的实例树。

> 名词速查：**Driver** 在编译器语境里指「把命令行参数 → 读文件 → 调用各编译阶段」串起来的顶层协调器，类似 `gcc`/`clang` 的 driver。slang 把它实现成 `slang::driver::Driver` 类，sv-elab 直接拿来用。

## 3. 本讲源码地图

本讲全部内容集中在一个文件里，重点区间在 `execute()` 的中段：

| 文件 | 作用 |
|------|------|
| [src/slang_frontend.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc) | 定义 `SlangFrontend::execute`（驱动流水线就在这里）、`fixup_options`、`catch_forbidden_options`、`get_instance_body`、`HierarchyQueue`。 |
| [src/slang_frontend.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h) | 声明 `SynthesisSettings`（含 `disable_instance_caching` 等字段）与 `NetlistContext` 的构造签名，用于理解选项如何流进 Compilation。 |

> 说明：slang 以 git 子模块形式内嵌在 `third_party/slang/`（需 `--recursive` clone）。本讲只引用 sv-elab 侧的源码与行号；对 slang driver 各方法的描述基于其在 sv-elab 中的**调用方式与行为**，不引用 slang 内部行号。所有 sv-elab 行号对应当前 HEAD `3dddccd`。

## 4. 核心概念与源码讲解

本讲按四个最小模块推进，恰好对应数据流的四个环节：

1. **4.1 `slang::driver::Driver`**：sv-elab 复用的编译前端框架，及其五大成员。
2. **4.2 命令行解析与编译环境装配**：`parseCommandLine` → `fixup_options` → `processOptions` → `catch_forbidden_options`。
3. **4.3 `parseAllSources` → `createCompilation`**：slang 产出 `Compilation`（AST 根 + 诊断）。
4. **4.4 从 `topInstances` 进入精化**：sv-elab 消费 `Compilation` 的入口与 `get_instance_body`。

### 4.1 slang::driver::Driver：sv-elab 复用的编译前端框架

#### 4.1.1 概念说明

「解析 SystemVerilog」是一件庞大的工程：要支持 `-D` 宏、`+incdir` 头文件搜索、命令行直接列多个 `.v`/`.sv`、`--top` 指定顶层、各种 pragma……如果 sv-elab 自己写一套，既重复劳动又容易和 slang 的语义分析对不齐。所以 sv-elab 的策略是：**直接实例化 slang 的 `slang::driver::Driver`，把它当成一个现成的「读源码 + 建编译」黑盒来用**。

`Driver` 是一个聚合对象，它把编译前端需要的几大件攥在一起。从 sv-elab 对它的使用方式，我们可以反推出它对外暴露的关键成员：

| 成员 | 类型（slang 侧） | sv-elab 用它做什么 |
|------|------------------|--------------------|
| `cmdLine` | `slang::CommandLine` | 注册并解析命令行选项（既挂 slang 标准参数，也挂 sv-elab 自己的参数） |
| `options` | `DriverOptions` | 持有 `-D` 宏定义（`defines`）、translate-off 格式（`translateOffOptions`）、编译开关（`compilationFlags`）、`timeScale` 等 |
| `sourceManager` | `slang::SourceManager` | 管理源文件缓冲区、行号/列号；heredoc 文本就通过它注入 |
| `sourceLoader` | `slang::SourceLoader` | 负责真正读取文件/缓冲区，喂给解析器 |
| `diagEngine` | `slang::DiagnosticEngine` | 诊断信息的格式化与上报引擎 |

理解这个表是本讲的基础：后面所有的 `driver.xxx(...)` 调用，本质都是在调度这几大件。

#### 4.1.2 核心流程

sv-elab 在 `execute()` 开头先「组装」driver，把三套参数来源都挂上去：

```
slang::driver::Driver driver;          ← 一个空 driver
        │
        ├── driver.addStandardArgs()    ← 来源①：slang 自带的标准参数（-D/-I/--top/+incdir…）
        │
        ├── SynthesisSettings settings;
        │   settings.addOptions(driver.cmdLine)  ← 来源②：sv-elab 专属参数（--dump-ast/--no-proc…）
        │
        └── diag::setup_messages(driver.diagEngine) ← 来源③：注册 sv-elab 自定义诊断文案
```

注意「来源①」和「来源②」都是往**同一个** `driver.cmdLine` 上注册选项，只是分别由 slang 和 sv-elab 负责。这样后续一次 `parseCommandLine` 就能把两类参数一起解析掉。

#### 4.1.3 源码精读

组装 driver 的三行：

[ src/slang_frontend.cc:3683-3687](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3683-L3687)

```cpp
slang::driver::Driver driver;
driver.addStandardArgs();
SynthesisSettings settings;
settings.addOptions(driver.cmdLine);
diag::setup_messages(driver.diagEngine);
```

- 第 3684 行 `addStandardArgs()` 是 slang driver 自带的方法，把 slang 的标准命令行参数（如 `-D`、`-I`、`--top`、`+incdir+…`、待编译的文件实参等）登记到 `driver.cmdLine`。
- 第 3686 行 `settings.addOptions(driver.cmdLine)` 把 sv-elab 的专属选项（`--dump-ast`、`--no-proc`、`--keep-hierarchy`、`--unroll-limit` 等）也登记到**同一个** `cmdLine`。`addOptions` 的实现在 [ src/slang_frontend.cc:89-140](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L89-L140)，逐项细节由 u2-l3 展开。
- 第 3687 行 `diag::setup_messages(driver.diagEngine)` 把 sv-elab 自定义的诊断码（`diag::*`）绑定到 slang 的诊断引擎上，以便后续用统一的通道上报（u2-l4 详讲）。

顺便看 `help()` 里也有**一模一样**的三行（[ src/slang_frontend.cc:3591-3594](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3591-L3594)）——`help read_slang` 输出的选项列表，就是把 driver 的标准参数和 sv-elab 的参数合并打印出来。这从侧面印证了两套参数是「合流」到同一个 `cmdLine` 的。

#### 4.1.4 代码实践

**实践目标**：确认「两套参数合流到同一个 cmdLine」，并认清 driver 的五大成员。

**操作步骤**（源码阅读型）：

1. 打开 [src/slang_frontend.cc:3683-3687](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3683-L3687)，确认 `driver.cmdLine` 被 `addStandardArgs()` 与 `settings.addOptions(...)` 共同写入。
2. 在 [src/slang_frontend.cc:89-140](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L89-L140) 里任选两个 sv-elab 专属选项（如 `--dump-ast`、`--no-proc`），看清它们都是通过 `cmdLine.add(...)` 注册的。
3. 全文搜索 `driver.`，把命中的成员名（`cmdLine` / `options` / `sourceManager` / `sourceLoader` / `diagEngine`）填进 4.1.1 的表格，验证「五大成员」都有真实调用点。

**需要观察的现象**：所有 `driver.` 调用都落在表格列出的那几个成员上；没有任何一处 sv-elab 去「手动调 slang 的 lexer/parser」——它始终通过 driver 这个门面来驱动 slang。

**预期结果**：你能向别人解释「sv-elab 不直接碰 slang 的底层解析器，而是把参数和源码都交给 `Driver`，由 `Driver` 统一调度」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 sv-elab 要调用 `driver.addStandardArgs()`，而不是只注册自己的 `SynthesisSettings`？  
**答案**：因为用户需要 slang 的标准能力——`-D` 定义宏、`-I`/`+incdir` 找头文件、`--top` 指定顶层、在命令行直接列源文件等。这些参数的解析逻辑 slang 已经写好，`addStandardArgs()` 把它们登记进 `cmdLine`，sv-elab 直接复用即可。

**练习 2**：`driver.cmdLine` 是 slang 的对象还是 sv-elab 的对象？  
**答案**：它是 slang 的对象（`Driver` 的成员），但 sv-elab 通过 `settings.addOptions(driver.cmdLine)` 把自己的选项也注册了上去——两套参数共用同一个命令行解析器实例。

---

### 4.2 命令行解析与编译环境装配：parseCommandLine → fixup_options → processOptions

#### 4.2.1 概念说明

把参数「登记」好（4.1）之后，还要真正去**解析**用户这次敲的命令行，并把解析结果**应用**到 driver 的配置上。slang driver 把这件事拆成两步：

- `parseCommandLine(argc, argv)`：把 argv 的字符串切分、匹配到已登记的选项上，把值写进 `driver.options` 与 `SynthesisSettings` 的各字段。
- `processOptions()`：把解析得到的原始选项「加工」成可执行的配置——例如整理 include 目录、配置 pragma 处理、把宏定义装填好等。

sv-elab 的精妙之处在于：**它在这两步之间插了一个 `fixup_options()`**。也就是说，先让用户参数解析进来，然后由 sv-elab 修补/补齐一批「综合场景必备」的默认值，**再**让 slang 去 process。这样既尊重了用户的显式覆盖，又保证了综合所需的编译环境。

> 直觉：`parseCommandLine` 是「点菜」，`fixup_options` 是「服务员补上必备的餐具」，`processOptions` 是「后厨开始备料」。餐具必须在备料前摆好。

#### 4.2.2 核心流程

```
args（含 read_slang、用户选项、文件名、注入的 default_options）
        │  转 char*[] (c_args)
        ▼
driver.parseCommandLine(argc, argv)   ──失败→ log_cmd_error("Bad command")
        │  此时 driver.options / settings 已填入用户显式值
        ▼
fixup_options(settings, driver)       ← sv-elab 插手：补 SYNTHESIS 宏、translate-off、
        │                                  compilationFlags、timeScale（仅当用户未指定时）
        ▼
driver.processOptions()               ──失败→ log_cmd_error("Bad command")
        │
        ▼
catch_forbidden_options(driver)       ← sv-elab 再补一刀：禁止某些危险的选项降级
```

注意 `fixup_options` 的位置：它在 `parseCommandLine` **之后**（能看到用户给了什么）、在 `processOptions` **之前**（改的值会被 slang 真正使用）。

#### 4.2.3 源码精读

**(a) 解析命令行** [ src/slang_frontend.cc:3700-3708](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3700-L3708)

```cpp
for (auto arg : args) {
    char *c = new char[arg.size() + 1];
    strcpy(c, arg.c_str());
    c_args_guard.emplace_back(c);
    c_args.push_back(c);
}

if (!driver.parseCommandLine(c_args.size(), &c_args[0]))
    log_cmd_error("Bad command\n");
```

slang 的 `parseCommandLine` 接收经典的 `(argc, argv)` 形式，所以这里把 `std::vector<std::string>` 拷成 `char*` 数组（用 `c_args_guard` 管理生命周期）。`argv[0]` 约定是程序名，对应 `args[0]` 即 `"read_slang"`。解析失败用 `log_cmd_error`（命令级错误，比 `log_error` 更贴近「这条命令用错了」的语义）。

在这之前还有两件小事值得注意：

- heredoc 注入（[ src/slang_frontend.cc:3690-3693](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3690-L3693)）：如果用户用了 `read_slang <<EOF ... EOF`，把整段源码当作一个名为 `<inlined>` 的缓冲区，通过 `driver.sourceManager.assignText` + `driver.sourceLoader.addBuffer` 喂进 driver。于是 heredoc 里的源码和磁盘上的 `.v` 文件在 driver 眼里没有区别。
- 默认参数注入（[ src/slang_frontend.cc:3695](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3695)）：把 `slang_defaults -add` 预存的 `default_options` 插到 `args[1]` 起的位置（即紧跟命令名之后、用户选项之前），让默认参数像用户自己写的一样被解析。

**(b) fixup_options：sv-elab 的编译环境补丁** [ src/slang_frontend.cc:3477-3522](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3477-L3522)

这个函数改了四样东西，每样都「只在用户没显式指定时」才动手（这正是它放在 `parseCommandLine` 之后的意义）：

1. **`SYNTHESIS` 宏** [ src/slang_frontend.cc:3490-3492](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3490-L3492)：除非用户给 `--no-synthesis-define`，否则往 `driver.options.defines` 里塞 `SYNTHESIS=1`。源码里常见的 `` `ifdef SYNTHESIS `` 分支就是靠它生效的——综合时走可综合分支，仿真时走另一套。

   ```cpp
   if (!settings.no_synthesis_define.value_or(false)) {
       driver.options.defines.push_back("SYNTHESIS=1");
   }
   ```

2. **translate-off 注释格式** [ src/slang_frontend.cc:3494-3504](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3494-L3504)：除非 `--no-default-translate-off-format`，否则登记一批常见的「综合时忽略」注释 pragma（`pragma translate_off/translate_on`、`synopsys synthesis_off/...`、Xilinx 风格等）。这让源码里夹带的「仅供仿真」片段在综合时被跳过。

3. **编译开关 compilationFlags** [ src/slang_frontend.cc:3506-3515](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3506-L3515)：两条关键设置——

   ```cpp
   auto &disable_inst_caching = flags[ast::CompilationFlags::DisableInstanceCaching];
   if (!disable_inst_caching.has_value()) {
       disable_inst_caching = true;
   }
   settings.disable_instance_caching = disable_inst_caching.value();
   // ...
   flags[ast::CompilationFlags::DisallowRefsToUnknownInstances] = true;
   ```

   - slang 默认会做**实例体缓存**（`DisableInstanceCaching` 默认 false）：内容完全相同的实例体（同参数、同连接）会被去重共享，省一次精化。sv-elab 反手把它**强制设为 true**（禁用缓存），除非用户显式开启。源码注释 `// revisit slang#1326 in case of issues` 表明这是一个已知需要绕开的点。这个选择会直接影响 4.4 节 `get_instance_body` 走哪条分支。
   - `DisallowRefsToUnknownInstances = true`：禁止跨模块引用「未知实例」——因为 sv-elab 后续要把层次展平，引用解析必须闭合，不能有悬空目标。

4. **timescale** [ src/slang_frontend.cc:3518-3521](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3518-L3521)：用户没指定就默认 `1ns/1ns`，避免因缺少 timescale 而报错。

另外，`fixup_options` 还会为两个**已废弃**的选项（`--compat-mode`、`--extern-modules`）发一条 `DeprecatedOption` 诊断（[ src/slang_frontend.cc:3479-3488](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3479-L3488)）——它们现在「事实上始终开启」，只保留用于向后兼容。

**(c) processOptions + catch_forbidden_options** [ src/slang_frontend.cc:3711-3714](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3711-L3714)

```cpp
fixup_options(settings, driver);
if (!driver.processOptions())
    log_cmd_error("Bad command\n");
catch_forbidden_options(driver);
```

`processOptions()` 把前面填好的 `driver.options` 真正落地（整理 include 路径、装填宏、配置 pragma 引擎等）。之后 `catch_forbidden_options`（[ src/slang_frontend.cc:3530-3547](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3530-L3547)）做两件「安全审计」：

- 不允许用户把 `UnknownSystemName`（遇到未知系统函数名）这个诊断**降级**成 warning/ignore（[ src/slang_frontend.cc:3534-3542](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3534-L3542)）。因为综合器若静默吞掉未知系统调用，会产生与仿真不一致的网表，所以强制保持 Error。
- 不允许 `IgnoreUnknownModules`（[ src/slang_frontend.cc:3544-3546](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3544-L3546)），原因同上：未知模块必须显式处理（导入黑盒或报错），不能装作没看见。

#### 4.2.4 代码实践

**实践目标**：体会「fixup_options 夹在 parse 与 process 之间」带来的可覆盖默认值机制。

**操作步骤**（源码阅读型 + 可选运行）：

1. 在 [src/slang_frontend.cc:3490-3492](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3490-L3492) 确认 `SYNTHESIS=1` 只在 `!no_synthesis_define` 时添加。
2. 准备一段含 `` `ifdef SYNTHESIS `` 分支的最小 SV（示例代码，非项目原有）：
   ```systemverilog
   module top(input logic clk, output logic q);
       logic r;
       always_ff @(posedge clk) begin
   `ifdef SYNTHESIS
           r <= 1'b1;
   `else
           r <= $urandom;   // 仿真专用，综合时不应进入
   `endif
           q <= r;
       end
   endmodule
   ```
3.（待本地验证，需装好带 sv-elab 的 yosys）分别用 `read_slang` 默认方式与 `read_slang --no-synthesis-define` 综合上面的模块，用 `show` 或导出 RTLIL 对比 `r` 的驱动：默认情况下应走 `r <= 1'b1` 分支，加 `--no-synthesis-define` 后会因 `$urandom` 不被支持而报诊断。

**需要观察的现象**：默认综合走 `` `ifdef SYNTHESIS `` 的真分支；`--no-synthesis-define` 关掉宏后，预处理改走假分支，触发不同的下游行为。

**预期结果**：你能说清「`SYNTHESIS` 宏不是源码里定义的，而是 `fixup_options` 在解析后、处理前偷偷加进 `driver.options.defines` 的」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `fixup_options` 必须在 `parseCommandLine` 之后、`processOptions` 之前调用，顺序不能换？  
**答案**：在 `parseCommandLine` 之后，才能知道用户有没有显式指定（例如 `--no-synthesis-define`），从而做到「用户优先、否则补默认」；在 `processOptions` 之前，sv-elab 改写的 `driver.options` 才会被 slang 真正落地使用。换了顺序，要么看不到用户输入，要么改了也白改。

**练习 2**：`DisableInstanceCaching` 被 sv-elab 强制成什么值？这对 `get_instance_body` 有什么影响？  
**答案**：用户未指定时被强制为 `true`（禁用 slang 的实例体缓存），并同步写入 `settings.disable_instance_caching`。于是 `get_instance_body`（见 4.4）会走 `else` 分支返回 `instance.body`，而不是共享的 canonical body。

**练习 3**：`catch_forbidden_options` 为什么不许把 `UnknownSystemName` 降级？  
**答案**：综合器若把「未知系统函数」降级成警告并继续，产出的网表会与仿真语义不一致（仿真里那个系统调用可能改变状态，网表里却消失了）。为避免这种静默的不一致，sv-elab 强制它保持为 Error。

---

### 4.3 parseAllSources → createCompilation：slang 产出 Compilation

#### 4.3.1 概念说明

参数和环境就绪后，进入真正的「编译」环节。slang driver 提供两个阶段函数，sv-elab 依次调用：

- `parseAllSources()`：让 slang 读取所有源文件（包括 heredoc 注入的 `<inlined>` 缓冲区、命令行所列文件、`+incdir` 找到的头文件等），做**词法 + 语法分析**，得到一批语法树（SyntaxTree）。这一步只看「形式合不合法」，还不知道符号的含义。
- `createCompilation()`：把所有语法树组装成一个 `ast::Compilation`，并完成**语义分析与精化**——查找符号、类型检查、参数求值、`generate` 展开、模块实例化。返回的 `Compilation` 里挂着一棵已经展开好的、带完整语义的符号树（AST）。

两者的分工是经典的「语法 vs 语义」两段式。`Compilation` 就是这两段跑完后 slang 交给 sv-elab 的「成品」——它同时也是后续所有诊断和源码定位的总入口。

#### 4.3.2 核心流程

```
driver.parseAllSources()   ──返回 false→ log_error("Parsing failed; see full log for details")
        │  产出：一批 SyntaxTree（语法层面）
        ▼
driver.createCompilation()
        │  产出：ast::Compilation（语义层面，含 Root 符号、topInstances、所有诊断）
        ▼
（可选）import_blackboxes_from_rtlil(...)   ← 把 RTLIL 里已有模块作为黑盒导入 slang
（可选）dump_ast                            ← 把整棵 AST 序列化成 JSON 打印
        ▼
driver.reportCompilation(*compilation, false)   ← 打印编译摘要
check_diagnostics(... compilation->getAllDiagnostics() ...)   ← 测试模式用
        ▼
if (driver.diagEngine.getNumErrors())   ──有 error→ reportDiagnostics + log_error + return
```

关键决策点在最后那个 `getNumErrors()` 检查：**只有当 AST 没有任何 error 诊断时，才允许进入下一步翻译**。

#### 4.3.3 源码精读

**(a) 解析所有源文件** [ src/slang_frontend.cc:3717-3718](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3717-L3718)

```cpp
if (!driver.parseAllSources())
    log_error("Parsing failed; see full log for details\n");
```

`parseAllSources` 返回 `false` 表示词法/语法阶段就出了让它无法继续的问题，sv-elab 用 `log_error` 终止本次命令。那句「see full log for details」是当前 HEAD 刚加上的提示（提交 `3dddccd "Add hint about full log to error message"`）——因为 slang 的详细诊断已经通过 `OS::captureOutput`（[ src/slang_frontend.cc:3680](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3680)）转投到 Yosys 日志了，这里只给一句简短终止语。

**(b) 创建 Compilation** [ src/slang_frontend.cc:3720](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3720)

```cpp
auto compilation = driver.createCompilation();
```

返回一个 `std::unique_ptr<ast::Compilation>`（注意后面 [ src/slang_frontend.cc:3753](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3753) 写的是 `global_compilation = &(*compilation)`，对智能指针解引用取地址）。`Compilation` 是 slang 的「设计对象」，sv-elab 后续会从它身上取四样东西：

| 取法 | 含义 |
|------|------|
| `compilation->getRoot()` | 拿到根符号 `RootSymbol`，它是整棵符号树的根 |
| `getRoot().topInstances` | 顶层实例列表（可能多个，对应多个 `--top` 或自动推断的顶层） |
| `compilation->getAllDiagnostics()` | 本次编译累积的全部诊断 |
| `compilation->getSourceManager()` | 源码管理器（行号/列号/文件名），供诊断格式化用 |

**(c) 黑盒导入（可选）** [ src/slang_frontend.cc:3722-3723](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3722-L3723)

```cpp
if (settings.extern_modules.value_or(true))
    import_blackboxes_from_rtlil(driver.sourceManager, *compilation, design);
```

`extern_modules` 是个已废弃选项（`fixup_options` 里会发废弃提示），「事实上始终开启」。它的作用是**反向桥接**：把当前 RTLIL `design` 里已经存在的模块（可能由别的 Yosys 命令读入）作为「黑盒」注入 slang 的 `Compilation`，让 SV 源码里引用到的非 SV 模块能被解析通过。细节由 u7-l3 展开。

**(d) dump_ast（可选）** [ src/slang_frontend.cc:3725-3731](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3725-L3731)

```cpp
if (settings.dump_ast.value_or(false)) {
    slang::JsonWriter writer;
    writer.setPrettyPrint(true);
    ast::ASTSerializer serializer(*compilation, writer);
    serializer.serialize(compilation->getRoot());
    std::cout << writer.view() << std::endl;
}
```

这是本讲最直接的「观察 slang 产出」的手段：`--dump-ast` 把整棵 AST（从 `getRoot()` 开始）序列化成 JSON 打到 stdout。对学习本讲而言，它能让你**亲眼看到** sv-elab 即将消费的那棵符号树长什么样——顶层实例、子实例、端口、变量都在里面。

**(e) 上报编译结果并检查错误** [ src/slang_frontend.cc:3735-3751](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3735-L3751)

```cpp
driver.reportCompilation(*compilation,/* quiet */ false);
if (check_diagnostics(driver.diagEngine, compilation->getAllDiagnostics(), /*last=*/false))
    in_succesful_failtest = true;

if (driver.diagEngine.getNumErrors()) {
    // Stop here should there have been any errors from AST compilation,
    // PopulateNetlist requires a well-formed AST without error nodes
    (void) driver.reportDiagnostics(/* quiet */ false);
    if (!in_succesful_failtest)
        log_error("Design elaboration failed; see full log for details\n");
    return;
}

if (settings.ast_compilation_only.value_or(false)) {
    (void) driver.reportDiagnostics(/* quiet */ false);
    return;
}
```

- `reportCompilation` 打印编译摘要；`check_diagnostics` 是测试模式的钩子（用 `test_slangdiag -expect` 预期某条诊断），命中则进入 `in_succesful_failtest`（详见 u2-l4）。
- `getNumErrors()` 非零就**立即 return**。注释点明原因：`PopulateNetlist` 要求一棵没有错误节点的良构 AST。带 error 的 AST 结构不完整，继续翻译会触发断言失败或产出无意义网表。
- `ast_compilation_only`（`--ast-compilation-only`）是给开发者的选项：到这一步就停下，不进入翻译段——方便单独调试 slang 侧的产出。

#### 4.3.4 代码实践

**实践目标**：用 `--dump-ast` 直观看到 slang 产出的 Compilation，建立「AST 长这样」的感性认识。

**操作步骤**（待本地验证，需装好带 sv-elab 的 yosys；源码阅读部分无需运行）：

1.（源码阅读型）打开 [src/slang_frontend.cc:3725-3731](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3725-L3731)，确认 `dump_ast` 序列化的起点是 `compilation->getRoot()`，与 4.4 节取 `topInstances` 的起点一致——都是 Root。
2.（待本地验证）写一个最小 heredoc 脚本（示例代码，非项目原有）：
   ```
   read_slang --dump-ast <<EOF
   module top(input logic clk, output logic [3:0] q);
       logic [3:0] r;
       always_ff @(posedge clk) r <= r + 1;
       assign q = r;
   endmodule
   EOF
   ```
3. 把 stdout 的 JSON 里搜 `"kind":"Instance"` 或 `"name":"top"`，找到顶层实例节点；再观察它下面的端口、变量、过程块节点。

**需要观察的现象**：JSON 里出现一棵以 `top` 实例为根的树，包含 `clk`/`q`/`r` 等符号与 `always_ff` 过程块——这就是 sv-elab 即将在 4.4 节开始遍历的对象。

**预期结果**：你能在 dump 出的 JSON 里指出「顶层实例」对应 `getRoot().topInstances` 的一个元素，从而把抽象的 `Compilation` 与一份可见的树对上号。

#### 4.3.5 小练习与答案

**练习 1**：`parseAllSources` 和 `createCompilation` 各自对应「语法分析」还是「语义分析」？  
**答案**：`parseAllSources` 对应词法 + 语法分析，产出语法树（只看形式合法）；`createCompilation` 对应语义分析与精化，产出带完整语义的 `Compilation`（符号查找、类型检查、参数求值、generate 展开、实例化都在这里完成）。

**练习 2**：为什么 `getNumErrors()` 非零时一定要 `return`，哪怕只报了一个 error？  
**答案**：因为 `PopulateNetlist` 要求一棵没有错误节点的良构 AST（见代码注释）。任何 error 都意味着符号树某处残缺，继续翻译会触发断言失败或产出与设计不符的网表，所以宁可早停。

**练习 3**：`--ast-compilation-only` 选项对调试本讲的内容有什么用？  
**答案**：它让流程停在 `createCompilation` 之后、翻译之前（[ src/slang_frontend.cc:3748-3751](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3748-L3751)），配合 `--dump-ast` 可以单独观察 slang 的产出，把「slang 侧的问题」和「sv-elab 翻译侧的问题」隔离开来排查。

---

### 4.4 从 topInstances 进入精化：sv-elab 消费 Compilation

#### 4.4.1 概念说明

走到这里，slang 已经交付了一个良构的 `Compilation`。从这一刻起，**主导权从 slang 交到 sv-elab 手里**。sv-elab 消费这棵 AST 的入口非常集中：从 `compilation->getRoot().topInstances` 取出顶层实例，逐个创建 `NetlistContext`（一张 RTLIL 模块画布），再用 `PopulateNetlist` 访问者遍历实例体，把符号翻译成 RTLIL。

这里有一个关键的桥接函数 `get_instance_body`：slang 的 `InstanceSymbol` 分「外壳」和「实例体（InstanceBodySymbol）」两层，sv-elab 需要拿到实例体才是真正可遍历的内容；而实例体又有「缓存（canonical）版」与「直接版」之分，受 4.2 节那个 `DisableInstanceCaching` 开关控制。

> 概念：slang 里 `InstanceSymbol` 是「一次模块例化」，`InstanceBodySymbol` 是「这次例化的具体内容」（端口连接、内部变量、子实例等）。`parentInstance` 指回所属实例，`getCanonicalBody()` 返回 slang 去重共享后的「规范体」。sv-elab 把每个被翻译的实例体称为一个 **realm**（见 `NetlistContext::realm`），即「这块网表对应哪段实例体」。

#### 4.4.2 核心流程

```
global_compilation = &(*compilation)        ← 缓存全局指针，供后续格式化诊断/源码文本
global_sourcemgr   = compilation->getSourceManager()
        │
        ▼
HierarchyQueue hqueue;                       ← 工作列表：待翻译的模块队列
for (auto instance : compilation->getRoot().topInstances) {
        │   ① 跳过 Program 块（sv-elab 不支持）
        │   ② ref_body = get_instance_body(settings, *instance)
        │   ③ hqueue.get_or_emplace(ref_body, ...) → 新建 NetlistContext 并入队
        │   ④ netlist.canvas->attributes[ID::top] = 1   标记顶层
}
        │
        ▼
for (i = 0; i < hqueue.queue.size(); i++) {  ← 注意：上界是实时 size()
    NetlistContext &netlist = *hqueue.queue[i];
    PopulateNetlist populate(hqueue, netlist);
    netlist.realm.visit(populate);            ← 真正翻译 AST → RTLIL（可能往 hqueue 追加新模块）
    收集 populate/netlist 的诊断
}
```

这条链的精髓是：**slang 给出 `topInstances`（顶层实例符号）→ sv-elab 取实例体 → 建 NetlistContext → PopulateNetlist 访问者接管**。之后就是 u2-l1 说过的「边遍历边扩张」的工作列表循环（翻译子模块时往 `hqueue` 追加新模块），具体翻译细节落到第 3~7 单元。

#### 4.4.3 源码精读

**(a) 缓存全局指针** [ src/slang_frontend.cc:3753-3754](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3753-L3754)

```cpp
global_compilation = &(*compilation);
global_sourcemgr = compilation->getSourceManager();
```

这两个文件级全局变量（声明在 [ src/slang_frontend.cc:146-147](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L146-L147)）在后续翻译中被频繁使用：`global_compilation` 用来查询符号属性（`transfer_attrs` 里 `global_compilation->getAttributes(from)`，见 [ src/slang_frontend.cc:312](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L312)），`global_sourcemgr` 用来把源码位置格式化成 `文件:行.列` 字符串（`format_src`，[ src/slang_frontend.cc:154-172](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L154-L172)）。把它们缓存成全局，是为了让任意深处的翻译代码都能方便地回查 Compilation 与源码信息。

**(b) 遍历 topInstances，建 NetlistContext** [ src/slang_frontend.cc:3756-3770](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3756-L3770)

```cpp
HierarchyQueue hqueue;
for (auto instance : compilation->getRoot().topInstances) {
    if (instance->getDefinition().definitionKind == ast::DefinitionKind::Program) {
        slang::Diagnostic program_diag(diag::ProgramUnsupported, instance->location);
        driver.diagEngine.issue(program_diag);
        continue;
    }

    auto ref_body = &get_instance_body(settings, *instance);
    log_assert(ref_body->parentInstance);
    auto [netlist, new_] = hqueue.get_or_emplace(ref_body, design, settings,
                                                 *compilation, *ref_body->parentInstance);
    log_assert(new_);
    netlist.canvas->attributes[ID::top] = 1;
}
```

- `compilation->getRoot().topInstances` 就是 slang 交付的顶层实例集合（可能不止一个，对应多个 `--top`，或 slang 自动推断的顶层）。
- `Program` 块被显式跳过并报 `ProgramUnsupported`（sv-elab 不综合 program block）。
- `get_instance_body` 拿到要翻译的实例体；`ref_body->parentInstance` 必须存在（`log_assert`）。
- `hqueue.get_or_emplace(ref_body, ...)` 按实例体去重地创建 `NetlistContext`：若该实例体已建过则复用，否则 `new` 一个并入队。返回的 `new_` 为 true 表示新建（[ src/slang_frontend.cc:1696-1718](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L1696-L1718)）。顶层实例必然是新建，故 `log_assert(new_)`。
- 给顶层模块的画布打上 `top` 属性，方便下游识别根模块。

`NetlistContext` 的构造签名（[ src/slang_frontend.h:589-592](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L589-L592)）正好接收 `(design, settings, compilation, instance)`——这正是 `get_or_emplace` 转发的那几个参数，把「RTLIL 设计、综合选项、slang Compilation、对应实例」四者绑进一个网表上下文。

**(c) get_instance_body：实例体选择** [ src/slang_frontend.cc:501-507](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L501-L507)

```cpp
const ast::InstanceBodySymbol &get_instance_body(SynthesisSettings &settings, const ast::InstanceSymbol &instance)
{
    if (!settings.disable_instance_caching && instance.getCanonicalBody())
        return *instance.getCanonicalBody();
    else
        return instance.body;
}
```

把这个函数和 4.2 节的 `fixup_options` 连起来看就完整了：

- `fixup_options` 默认把 `settings.disable_instance_caching` 设为 `true`（[ src/slang_frontend.cc:3508-3512](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3508-L3512)）。
- 于是 `!settings.disable_instance_caching` 为 `false`，`get_instance_body` 走 `else`，返回 `instance.body`（每个实例自己的体），而不是 slang 去重共享的 `getCanonicalBody()`。
- 只有当用户显式开启实例缓存（让 `disable_instance_caching` 变回 false）时，才会走 `if` 分支用 canonical body。

这条分支直接影响 `NetlistContext::realm` 指向哪个体，进而影响层次展平与源码定位的行为。

**(d) 翻译循环** [ src/slang_frontend.cc:3772-3780](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3772-L3780)

```cpp
for (int i = 0; i < (int) hqueue.queue.size(); i++) {
    NetlistContext &netlist = *hqueue.queue[i];
    emitted_module_names.push_back(netlist.canvas->name);

    if (netlist.disabled)
        continue;

    PopulateNetlist populate(hqueue, netlist);
    netlist.realm.visit(populate);
    ...
}
```

`netlist.realm.visit(populate)` 是数据流的「最后一跳」：对实例体（realm）这棵符号树发起 AST 访问，`PopulateNetlist` 在访问过程中把端口、线网、过程块、子实例等逐一翻译成 RTLIL，并在遇到子模块实例时往 `hqueue` 追加新的 `NetlistContext`（所以循环上界要用实时 `size()`）。这段是后续单元的主场，本讲只定位到「它从这里启动」。

#### 4.4.4 代码实践

**实践目标**：把「slang 产出 → sv-elab 消费」的接缝在源码里走一遍，确认数据交接点。

**操作步骤**（源码阅读型）：

1. 从 [src/slang_frontend.cc:3757](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3757) `compilation->getRoot().topInstances` 出发，这是 slang 侧的「交付物」。
2. 跟到 [src/slang_frontend.cc:3764](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3764) `get_instance_body(settings, *instance)`，确认它返回的是 `InstanceBodySymbol`（实例体）。
3. 再到 [src/slang_frontend.cc:3766-3767](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3766-L3767) `hqueue.get_or_emplace(ref_body, design, settings, *compilation, *ref_body->parentInstance)`，看清这五个参数如何被转发给 `NetlistContext` 构造函数（对照 [src/slang_frontend.h:589-592](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L589-L592)）。
4. 最后落在 [src/slang_frontend.cc:3780](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3780) `netlist.realm.visit(populate)`，这是 sv-elab 翻译段的起点。

**需要观察的现象**：从 `topInstances`（slang 的符号）到 `netlist.realm.visit(populate)`（sv-elab 的 RTLIL 生成），中间只经过 `get_instance_body` 与 `get_or_emplace` 两个中转——接缝非常窄。

**预期结果**：你能画出 4.4.2 那张数据流图，并指出 slang 与 sv-elab 的「握手点」就是 `getRoot().topInstances`。

#### 4.4.5 小练习与答案

**练习 1**：`topInstances` 可能包含多个实例吗？为什么？  
**答案**：可能。它对应 slang 解析到的顶层实例集合——用户可以通过多个 `--top` 指定多个顶层，或 slang 自动推断出多个顶层。所以代码用 `for` 遍历而非只取第一个。

**练习 2**：默认情况下（用户没动 `DisableInstanceCaching`），`get_instance_body` 返回 `instance.body` 还是 `instance.getCanonicalBody()`？为什么？  
**答案**：返回 `instance.body`。因为 `fixup_options` 默认把 `settings.disable_instance_caching` 置为 `true`（[ src/slang_frontend.cc:3508-3512](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3508-L3512)），使 `get_instance_body` 的 `if` 条件为假，走 `else` 分支。

**练习 3**：为什么要在进入翻译前缓存 `global_compilation` 和 `global_sourcemgr` 这两个全局指针？  
**答案**：因为后续翻译（包括 `PopulateNetlist` 及其深处）需要频繁回查 Compilation（取符号属性）与 SourceManager（把源码位置格式化成可读的 `文件:行.列`）。缓存成全局变量，任意深处的翻译代码都能直接取用，而不必把这两个指针层层传递。

---

## 5. 综合实践

把本讲四节串起来，完成下面这个「画数据流」的核心任务（即本讲规格指定的实践）：

**任务**：跟踪 [`execute()`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3674) 中从 [`driver.parseAllSources`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3717) 到 [`compilation->getRoot().topInstances`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3757) 的调用链，画一张「slang 侧产出 / sv-elab 侧消费」的数据流图。

**建议步骤**：

1. 在一张纸上画两个泳道：左道「slang 侧（产出）」，右道「sv-elab 侧（消费/装配）」。
2. 在 slang 道自上而下标出：`parseAllSources`（产出 SyntaxTree）→ `createCompilation`（产出 `Compilation`：含 Root、topInstances、诊断、SourceManager）。
3. 在 sv-elab 道标出装配动作：`addStandardArgs` / `settings.addOptions` / `diag::setup_messages`（参数合流）→ `parseCommandLine` → `fixup_options`（注入 SYNTHESIS 宏、translate-off、compilationFlags、timescale）→ `processOptions` → `catch_forbidden_options`。
4. 用箭头标出两个「跨界点」：
   - `parseCommandLine` 之前，sv-elab 把 heredoc 文本与 `default_options` 注入 driver（[ src/slang_frontend.cc:3690-3695](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3690-L3695)）。
   - `createCompilation` 之后，sv-elab 从 `compilation->getRoot().topInstances` 接手，经 `get_instance_body` → `get_or_emplace` → `netlist.realm.visit(populate)` 进入翻译（[ src/slang_frontend.cc:3757-3780](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3757-L3780)）。
5. 在图上标注「致命错误闸门」：`parseAllSources` 返回 false → `log_error("Parsing failed...")`；`getNumErrors()` 非零 → `log_error("Design elaboration failed...")` 并 return。

**交付物**：一张完整的数据流图。完成后，你应当能一眼指出：**slang 的产出是 `Compilation`，sv-elab 的消费入口是 `getRoot().topInstances`，两者之间唯一的「加工层」是 `fixup_options` 与黑盒导入**。

> 想再加深印象（待本地验证）：用 `tests/unit/dff.ys` 的第一段 `read_slang <<EOF ... EOF` 作为输入，对照你的图，说出 heredoc 里的 `dff_iff01_gate` 模块在哪一步被解析、在哪一步被精化、又在哪一步被 `PopulateNetlist` 翻译成 RTLIL。

## 6. 本讲小结

- sv-elab 不自己写解析器，而是实例化 [`slang::driver::Driver`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3683) 复用 slang 的命令行/源码加载框架；driver 暴露 `cmdLine` / `options` / `sourceManager` / `sourceLoader` / `diagEngine` 五大成员。
- 参数有「三来源」：`addStandardArgs`（slang 标准）、`settings.addOptions`（sv-elab 专属，合流到同一 `cmdLine`）、`diag::setup_messages`（自定义诊断）。
- 处理顺序是 `parseCommandLine` → `fixup_options` → `processOptions` → `catch_forbidden_options`；sv-elab 特意把 `fixup_options` 夹在中间，在尊重用户显式值的前提下注入 `SYNTHESIS=1` 宏、translate-off 格式、`compilationFlags`（含默认禁用实例缓存）、`timescale` 等综合必备默认值。
- `parseAllSources` 做词法/语法分析产出语法树，`createCompilation` 做语义分析/精化产出 [`ast::Compilation`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3720)；`getNumErrors()` 非零必须 return，因为翻译需要一棵无错误节点的良构 AST。
- slang 与 sv-elab 的「握手点」是 [`compilation->getRoot().topInstances`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3757)：sv-elab 逐个取顶层实例 → `get_instance_body` 拿实例体（默认走 `instance.body`，因实例缓存被禁用）→ `get_or_emplace` 建 `NetlistContext` → `netlist.realm.visit(populate)` 进入翻译。
- `global_compilation` / `global_sourcemgr` 被缓存为全局指针，供后续翻译随处回查符号属性与源码位置。

## 7. 下一步学习建议

本讲把 slang driver 这条「输入侧」数据流走通了。接下来建议：

- **u2-l3 SynthesisSettings**：把本讲多次提到的 `settings.addOptions`、`fixup_options` 里各选项（`dump_ast` / `no_proc` / `keep_hierarchy` / `unroll_limit` / `extern_modules` 等）逐一讲清，并说明 `hierarchy_mode()`、`unroll_limit()` 等派生方法。
- **u2-l4 诊断系统**：展开 `diag::setup_messages`、`check_diagnostics` 与 `in_succesful_failtest`，理解「期望失败」的测试模式如何与 `getNumErrors()` 的硬停逻辑配合。
- **跨前看翻译段**：若想立刻看 sv-elab 如何消费 `topInstances` 之后的内容，可跳到单元 3（`NetlistContext` / `RTLILBuilder` / `Variable`），那里会把本讲末尾的 `netlist.realm.visit(populate)` 拆开细讲。

读完 u2-l2 ~ u2-l4，你就能把「一行 `read_slang` 命令」从命令行参数一路追到 RTLIL 模块画布，完整掌握入口这条主线。
