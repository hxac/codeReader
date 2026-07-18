# SynthesisSettings：read_slang 的全部选项

## 1. 本讲目标

学完本讲后，你应该能够：

- 看懂 [`SynthesisSettings`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L492-L534) 这个结构体里每一个字段代表什么、默认值是什么，以及它为什么大量使用 `std::optional<bool>`。
- 在 [`SynthesisSettings::addOptions`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L89-L140) 里，把「命令行开关 `--dump-ast`」和「C++ 字段 `dump_ast`」一一对应起来，并理解重复出现的选项（如 `--blackboxed-module`）与枚举选项（如 `--udp-handling`）的注册方式。
- 解释 [`fixup_options`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3477-L3522) 与 [`catch_forbidden_options`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3530-L3547) 在「用户参数 → slang driver 选项」这条链路里扮演的角色：前者负责**在尊重用户显式值的前提下注入综合必备默认值**，后者负责**禁止某些会破坏综合语义的选项降级**。
- 学会通过命令行开关（如 `--dump-ast`、`--keep-hierarchy`、`--unroll-limit`）控制 `read_slang` 的综合行为，并能预测每个开关对最终网表的影响。

本讲紧承 u2-l1（前端注册与 `read_slang` 命令）和 u2-l2（slang 驱动流水线）。u2-l2 把 `execute()` 的中段放大讲了 slang driver 做了什么；本讲则把镜头对准那条处理链里的一句话——「`settings.addOptions` 注册 sv-elab 专属选项」「`fixup_options` 注入默认值」，把这一句话展开成一整张「选项地图」。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自 u1-l1、u2-l1、u2-l2）：

- sv-elab（原名 yosys-slang）借助内嵌的 slang 库完成词法/语法/语义分析得到 AST，再自己把 AST 翻译成 Yosys 的 RTLIL 网表。用户命令是 `read_slang`。
- sv-elab 不重写命令行解析器，而是复用 slang 的 [`slang::driver::Driver`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3683)。`Driver` 暴露 `cmdLine`、`options`、`diagEngine` 等成员，参数有三来源并合流到同一个 `cmdLine`：slang 标准参数（`addStandardArgs`）、sv-elab 专属参数（`settings.addOptions`）、自定义诊断（`diag::setup_messages`）。
- `execute()` 里选项的处理链是：`parseCommandLine` → `fixup_options` → `processOptions` → `catch_forbidden_options`。其中 `fixup_options` 特意夹在中间，目的是在 slang 真正「消化」参数（`processOptions`）之前，把综合必备的默认值补上。

此外需要一点 C++ 直觉：

- **`std::optional<T>`** 是一个「可能持有值、也可能空」的包装类型。在本讲里，几乎所有布尔开关都用 `std::optional<bool>` 而非 `bool`。差别很关键：`bool` 字段只有「真/假」两态，无法区分「用户没写」和「用户写了 false」；`optional<bool>` 多了一个「空（未设置）」态，让 `fixup_options` 和后续代码能用 `value_or(默认)` 在「用户没写时用默认、用户写了时尊重用户」。

> 名词速查：**slang driver options** 是 slang driver 解析命令行后填好的一组结构体（`driver.options.defines`、`driver.options.compilationFlags`、`driver.options.timeScale` 等），slang 后续的解析与编译都读它。sv-elab 的 `SynthesisSettings` 是它**之外**的一组「sv-elab 专属」选项，由 sv-elab 自己读、自己消费。

## 3. 本讲源码地图

本讲集中在两个文件：

| 文件 | 作用 |
|------|------|
| [src/slang_frontend.h](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h) | 声明 `SynthesisSettings` 结构体（字段、`hierarchy_mode()`、`unroll_limit()` 派生方法）。 |
| [src/slang_frontend.cc](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc) | 实现 `addOptions`（注册命令行开关）、`fixup_options`（注入默认值）、`catch_forbidden_options`（禁止危险降级），以及 `execute()` 里消费这些选项的若干分支。 |

为了说明「每个开关最终在哪里被消费」，本讲还会点到几个**真实存在**的消费点（不在上述两文件内的，会单独标注），但不会展开它们——那是后续讲义（u5/u6/u7）的任务。

只引用真实存在的文件；所有行号均对应当前 HEAD `3dddccd`。

## 4. 核心概念与源码讲解

本讲按三个最小模块推进：

1. **4.1 SynthesisSettings**：选项的「数据模型」——一个装满 `optional` 的结构体。
2. **4.2 addOptions**：把数据模型挂到 slang 命令行上，让 `--xxx` 能写入字段。
3. **4.3 fixup_options 与 catch_forbidden_options**：在用户参数和 slang 选项之间，注入综合默认值、拦截危险选项。

### 4.1 SynthesisSettings：选项的数据模型

#### 4.1.1 概念说明

`read_slang` 有两类参数：

- **slang 标准参数**：比如 `-D`、`-I`、`--top`、`--timescale` 等，由 `driver.addStandardArgs()` 注册，sv-elab 几乎不碰。
- **sv-elab 专属参数**：比如 `--dump-ast`、`--keep-hierarchy`、`--ignore-timing` 等，这些是 sv-elab 作为「综合前端」特有的开关，全部收拢在 `SynthesisSettings` 里。

`SynthesisSettings` 就是「sv-elab 专属参数的内存表示」。它是一个普通结构体，每条命令调用都会 [`new`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3685) 出一份局部的 `SynthesisSettings settings`，把命令行写进它，再带着它走完整个翻译流程（最终它会被存进 [`NetlistContext::settings`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L538)，全程可读）。

设计上有两个值得注意的点：

- **几乎全部用 `std::optional<bool>` / `std::optional<int>`**：保留「用户没指定」这一态，使默认值可以集中在 `value_or(...)` 处统一给出，而不是散落在构造函数里。
- **派生方法**：`hierarchy_mode()` 和 `unroll_limit()` 不是字段，而是把一两个字段翻译成下游真正需要的枚举/整数。这样「命令行开关」与「内部使用的值」之间有一层清晰的换算。

#### 4.1.2 核心流程

```
用户敲 read_slang --keep-hierarchy --unroll-limit 1000 foo.sv
        │
        ▼
SynthesisSettings settings;            // 局部对象，所有 optional 都是「空」
        │
        ▼
settings.addOptions(driver.cmdLine);   // 把字段绑定到命令行（见 4.2）
        │
        ▼
driver.parseCommandLine(...)           // slang 解析命令行，把 --keep-hierarchy 写进 settings.keep_hierarchy = true
        │
        ▼
fixup_options(settings, driver);       // 注入默认值（见 4.3），不动用户已写的值
        │
        ▼
（翻译全程）下游代码读 settings.keep_hierarchy.value_or(false)、settings.unroll_limit() 等
```

#### 4.1.3 源码精读

结构体定义在头文件里，字段就是一张「选项清单」：

[ src/slang_frontend.h:492-534](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L492-L534) — `SynthesisSettings` 的全部字段：

```cpp
struct SynthesisSettings {
    std::optional<bool> dump_ast;
    std::optional<bool> no_proc;
    std::optional<bool> compat_mode;
    std::optional<bool> keep_hierarchy;
    std::optional<bool> best_effort_hierarchy;
    std::optional<bool> ignore_timing;
    std::optional<bool> ignore_initial;
    std::optional<bool> ignore_assertions;
    std::optional<int> unroll_limit_;
    std::optional<bool> extern_modules;
    std::optional<bool> no_implicit_memories;
    std::optional<bool> empty_blackboxes;
    std::optional<bool> ast_compilation_only;
    std::optional<bool> no_default_translate_off;
    std::optional<bool> allow_dual_edge_ff;
    std::optional<bool> no_synthesis_define;
    std::optional<UdpHandleMode> udp_handling;
    // pass std::less<> to enable transparent lookup
    std::set<std::string, std::less<>> blackboxed_modules;
    bool disable_instance_caching = false;

    enum HierMode { NONE, BEST_EFFORT, ALL };

    HierMode hierarchy_mode() { /* ... */ }
    int unroll_limit() { /* ... */ }

    void addOptions(slang::CommandLine &cmdLine);
};
```

注意几个非 `optional` 的成员：

- `blackboxed_modules` 是 `std::set<std::string>`，因为 `--blackboxed-module` 是**可重复**开关（一次可指定多个模块名），用集合累积。
- `disable_instance_caching` 是普通 `bool`（默认 `false`），但它**不来自命令行**，而是由 `fixup_options` 根据 slang 的 `CompilationFlags::DisableInstanceCaching` 反写回来（见 4.3.3）。

两个派生方法把字段换算成下游真正要用的值：

[ src/slang_frontend.h:520-531](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L520-L531) — 把两个层次开关合并成一个三态枚举、给展开限额兜底默认值：

```cpp
HierMode hierarchy_mode()
{
    if (keep_hierarchy.value_or(false))
        return ALL;
    if (best_effort_hierarchy.value_or(false))
        return BEST_EFFORT;
    return NONE;
}

int unroll_limit() {
    return unroll_limit_.value_or(4000);
}
```

`hierarchy_mode()` 的优先级很明确：`--keep-hierarchy` 优先于 `--best-effort-hierarchy`；两者都不给就是 `NONE`（默认全部展平）。`unroll_limit()` 在用户没写 `--unroll-limit` 时返回 4000——这个「4000」就是命令行 help 里写的默认值，两处必须一致。

#### 4.1.4 代码实践

**实践目标**：建立「字段 ↔ 命令行开关 ↔ 默认值」的三角对应关系。

**操作步骤**：

1. 打开 [src/slang_frontend.h:492-534](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L492-L534)，挑三个字段，比如 `keep_hierarchy`、`unroll_limit_`、`ignore_timing`。
2. 在 [src/slang_frontend.cc:89-140](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L89-L140)（`addOptions`）里找到它们各自对应的 `--xxx` 开关名。
3. 在 `hierarchy_mode()` / `unroll_limit()` 或下游消费点，找到它们的默认值。

**需要观察的现象**：你会看到「字段名」「开关名」「默认值」三者并不完全相同（如字段 `unroll_limit_` 带下划线、开关 `--unroll-limit` 用连字符、默认 `4000`），这正是它们各自服务于不同读者（C++ 代码 / 命令行用户 / 帮助文本）的结果。

**预期结果**：你能不看源码，说出 `--keep-hierarchy` 对应字段 `keep_hierarchy`、派生值 `ALL`、默认（不给）是 `NONE`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `keep_hierarchy` 用 `std::optional<bool>` 而不是 `bool keep_hierarchy = false`？

> **答案**：因为 `hierarchy_mode()` 需要区分「用户没写」和「用户写了 false」。如果用 `bool`，两者都会是 `false`，就没法在「用户没写」时回退去检查 `best_effort_hierarchy`。`optional` 多出的「空」态正是用来表达「未指定」。

**练习 2**：`disable_instance_caching` 为什么是普通 `bool` 而不是 `optional`？

> **答案**：因为它不直接来自命令行开关，没有「用户指定 vs 默认」的歧义；它的值由 `fixup_options` 在综合时统一确定（默认禁用实例缓存），下游直接读这个确定的布尔值即可。

### 4.2 addOptions：把选项挂到命令行

#### 4.2.1 概念说明

字段定义好了，但它们还不会自动和命令行联系起来。`addOptions` 的职责就是**把每个字段绑定到一个 `--xxx` 开关上**。它调用的是 slang 的 `slang::CommandLine::add(...)` 接口：传入「开关名」「一个 optional 引用」「帮助文本」，slang 在解析命令行时就会自动把开关的值写进那个 optional。

这一步完全是「声明式」的：每行 `cmdLine.add(...)` 就是「我声明一个开关」。sv-elab 不需要写任何解析逻辑——slang 替你做了。

#### 4.2.2 核心流程

```
对每个字段 f，调用 cmdLine.add("--name", f, "帮助文本")
        │
        ├── 布尔开关：cmdLine.add("--dump-ast", dump_ast, "...")       // 出现即 true
        ├── 带值开关：cmdLine.add("--unroll-limit", unroll_limit_, "...", "<limit>")
        ├── 可重复开关：cmdLine.add("--blackboxed-module", lambda 插入 set, "...")
        └── 枚举开关：cmdLine.addEnum<UdpHandleMode>(...)
        │
        ▼
driver.parseCommandLine(...) 时，slang 遇到 --xxx 就把对应 optional 置为值
```

#### 4.2.3 源码精读

`addOptions` 是一张紧凑的「开关登记表」，逐行注释如下：

[ src/slang_frontend.cc:89-129](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L89-L129) — 注册所有「现行」开关（节选关键行）：

```cpp
void SynthesisSettings::addOptions(slang::CommandLine &cmdLine) {
    cmdLine.add("--dump-ast", dump_ast, "Dump the AST");
    cmdLine.add("--no-proc", no_proc, "Disable lowering of processes");
    cmdLine.add("--keep-hierarchy", keep_hierarchy,
                "Keep hierarchy (experimental; may crash)");
    cmdLine.add("--best-effort-hierarchy", best_effort_hierarchy,
                "Keep hierarchy in a 'best effort' mode");
    cmdLine.add("--ignore-timing", ignore_timing, "Ignore delays for synthesis");
    cmdLine.add("--ignore-initial", ignore_initial,
                "Ignore initial blocks for synthesis");
    cmdLine.add("--ignore-assertions", ignore_assertions,
                "Ignore assertions and formal statements in input");
    cmdLine.add("--unroll-limit", unroll_limit_,
                "Set unrolling limit (default: 4000)", "<limit>");
    cmdLine.add("--no-implicit-memories", no_implicit_memories, /* 长帮助 */);
    cmdLine.add("--empty-blackboxes", empty_blackboxes,
                "Assume empty modules are blackboxes");
    cmdLine.add("--ast-compilation-only", ast_compilation_only,
                "For developers: stop after the AST is fully compiled");
    cmdLine.add("--no-default-translate-off-format", no_default_translate_off, /* ... */);
    cmdLine.add("--allow-dual-edge-ff", allow_dual_edge_ff,
                "Allow synthesis of dual-edge flip-flops (@(edge))");
    cmdLine.add("--no-synthesis-define", no_synthesis_define,
                "Don't add implicit -D SYNTHESIS");
    cmdLine.add("--blackboxed-module", /* lambda 插入 blackboxed_modules */, /* ... */);
    cmdLine.addEnum<UdpHandleMode, UdpHandleMode_traits>(
            "--udp-handling", udp_handling, /* ... */);
    // ...（见下方「已弃用」段落）
}
```

把上表整理成「选项速查表」（默认值来自各消费点的 `value_or(...)` 或派生方法）：

| 开关 | 字段 | 默认 | 作用 |
|------|------|------|------|
| `--dump-ast` | `dump_ast` | false | 编译完 AST 后把整棵 AST 以 JSON 打印到 stdout，便于调试 |
| `--no-proc` | `no_proc` | false | 跳过 `execute()` 末尾的 `proc_*` 流程，网表里保留 RTLIL `Process` |
| `--keep-hierarchy` | `keep_hierarchy` | false | 保留模块层次（`hierarchy_mode()==ALL`），实验性、可能崩溃 |
| `--best-effort-hierarchy` | `best_effort_hierarchy` | false | 尽力保留层次（`BEST_EFFORT`），只对可保留的实例保留 |
| `--ignore-timing` | `ignore_timing` | false | 忽略延迟（`#n`、`Delay` 时序控制），把它们当综合无关处理 |
| `--ignore-initial` | `ignore_initial` | false | 丢弃 `initial` 块 |
| `--ignore-assertions` | `ignore_assertions` | false | 忽略断言与形式化语句 |
| `--unroll-limit <n>` | `unroll_limit_` | 4000 | 循环/递归展开次数上限 |
| `--no-implicit-memories` | `no_implicit_memories` | false | 关闭「无标注数组自动推断为存储器」 |
| `--empty-blackboxes` | `empty_blackboxes` | false | 把空模块当作黑盒 |
| `--ast-compilation-only` | `ast_compilation_only` | false | 开发用：AST 编译完即停，不做翻译 |
| `--no-default-translate-off-format` | `no_default_translate_off` | false | 不默认识别任何 translate-off 注释格式 |
| `--allow-dual-edge-ff` | `allow_dual_edge_ff` | false | 允许双沿触发器 `@(edge)` 综合 |
| `--no-synthesis-define` | `no_synthesis_define` | false | 不隐式添加 `-D SYNTHESIS` |
| `--blackboxed-module <name>` | `blackboxed_modules`（集合） | 空 | 标记某模块为黑盒，可重复 |
| `--udp-handling <mode>` | `udp_handling`（枚举） | error | UDP 处理方式：`error` 或 `blackboxes` |

「可重复开关」用 lambda 实现——每出现一次 `--blackboxed-module X`，就把 `X` 插进 `blackboxed_modules` 集合：

[ src/slang_frontend.cc:119-125](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L119-L125) — `--blackboxed-module` 的回调把名字塞进集合：

```cpp
cmdLine.add("--blackboxed-module",
    [this](std::string_view value) {
        blackboxed_modules.insert(std::string(value));
        return "";
    },
    "Mark the named module for blackboxing. ...");
```

「枚举开关」用 `addEnum`，把字符串（`error`/`blackboxes`）映射到 `UdpHandleMode` 枚举：

[ src/slang_frontend.cc:126-129](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L126-L129) — `--udp-handling` 接受枚举值：

```cpp
cmdLine.addEnum<UdpHandleMode, UdpHandleMode_traits>(
        "--udp-handling", udp_handling, "Set the processing mode for user defined primitives."
        " When set to 'blackboxes' the UDP is treated as a blackboxed instance."
        " When set to 'error', an error is emitted if a UDP is encountered. By default, the frontend emits an error.");
```

最后一段是「已弃用开关」——它们仍被接受（为了不破坏旧脚本），但实际已恒为开启，`fixup_options` 会针对它们发出弃用诊断（见 4.3.3）：

[ src/slang_frontend.cc:131-139](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L131-L139) — 两个已弃用开关：

```cpp
// Deprecated section
cmdLine.add("--compat-mode", compat_mode,
            "Deprecated option which is effectively always on. ...");
cmdLine.add("--extern-modules", extern_modules,
            "Deprecated option which is effectively always on. ...");
```

> 提示：完整的、带换行排版的帮助文本由 [`SlangFrontend::help`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3589-L3608) 生成——它在内部也调用 `settings.addOptions(driver.cmdLine)`，再遍历 `driver.cmdLine.getHelpOptions()` 逐行打印。也就是说，`help read_slang` 的内容**直接来自 `addOptions` 的注册**，两者永远同步。

#### 4.2.4 代码实践

**实践目标**：用 `help read_slang` 验证「帮助文本 = addOptions 注册内容」，并学会查每个开关的官方描述。

**操作步骤**：

1. 加载插件后运行 `help read_slang`（或在 Yosys 交互模式敲 `help read_slang`）。
2. 对照本节的「选项速查表」，确认 `--unroll-limit`、`--no-implicit-memories`、`--udp-handling` 三项的帮助文字与 [src/slang_frontend.cc:89-129](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L89-L129) 里的字符串字面量一致。

**需要观察的现象**：help 输出里既有 sv-elab 专属开关（来自 `addOptions`），也有大量 slang 标准开关（来自 `driver.addStandardArgs()`），两者混排按字母序。

**预期结果**：你能在 help 里找到 `--unroll-limit` 的描述「Set unrolling limit (default: 4000)」，与本讲表格一致。

**待本地验证**：具体输出取决于本机 Yosys/slang 版本；若暂无环境，可改为阅读源码里 `wrap_text` 排版后的字符串字面量。

#### 4.2.5 小练习与答案

**练习 1**：`--unroll-limit` 的第四个参数 `"<limit>"` 是什么意思？为什么 `--dump-ast` 没有这个参数？

> **答案**：`"<limit>"` 是帮助文本里显示的「占位参数名」，表示这个开关**需要一个值**（`add` 对 `optional<int>` 的重载会去读下一个 token 作为整数）。`--dump-ast` 绑定的是 `optional<bool>`，是「出现即 true」的标志开关，不需要额外取值，所以没有占位参数。

**练习 2**：如果你想新增一个「忽略 `$readmem`」的开关，应该在 `addOptions` 的哪一行加？字段应该是什么类型？

> **答案**：在 [src/slang_frontend.cc:89-140](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L89-L140) 的现行开关区（弃用段之前）加一行 `cmdLine.add("--ignore-readmem", ignore_readmem, "...");`；字段应在 [SynthesisSettings](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.h#L492-L534) 里声明为 `std::optional<bool> ignore_readmem;`（保持「未指定」语义，与同类 `ignore_*` 一致）。

### 4.3 fixup_options 与 catch_forbidden_options：注入默认、拦截危险

#### 4.3.1 概念说明

光把 sv-elab 专属开关挂上去还不够。综合 frontend 还需要保证 slang 在解析和编译时处在「综合友好」的状态——比如：

- 必须定义 `SYNTHESIS` 宏，让源码里的 `` `ifdef SYNTHESIS `` 走综合分支；
- 必须识别常见的 translate-off 注释（`// synthesis_off` 等），把仿真专用代码排除；
- 必须禁用实例缓存（slang 的实例缓存与 sv-elab 的展平策略不兼容）；
- 必须禁止把「未知系统任务名」从错误降级成警告（否则网表与仿真语义会分叉）。

这些「综合必备默认值」如果让用户每次手敲，既繁琐又容易漏。`fixup_options` 的职责就是**在 `parseCommandLine` 之后、`processOptions` 之前**，把这些默认值补进 `driver.options`，并且**只在用户没有显式给出时才补**（尊重用户）。而 `catch_forbidden_options` 则在 `processOptions` 之后兜底，**禁止某些会破坏综合语义的选项降级**。

为什么 `fixup_options` 必须夹在 `parseCommandLine` 和 `processOptions` 之间？因为：

- 必须在 `parseCommandLine` **之后**：才知道用户到底写了什么，才能判断「该不该补默认」。
- 必须在 `processOptions` **之前**：`processOptions` 会把 `driver.options` 真正「消化」进 Compilation 的配置；默认值必须在它之前生效。

#### 4.3.2 核心流程

```
driver.parseCommandLine(...)   // 用户参数写进 driver.options / settings
        │
        ▼
fixup_options(settings, driver)
        │  ├─ 对弃用开关发诊断
        │  ├─ 若未禁用 → 加 SYNTHESIS=1 宏
        │  ├─ 若未禁用 → 加 translate-off 注释格式
        │  ├─ 默认启用 DisableInstanceCaching（除非用户已设）
        │  ├─ 强制 DisallowRefsToUnknownInstances = true
        │  └─ 默认 timescale = 1ns/1ns（除非用户已设）
        ▼
driver.processOptions()        // slang 真正消化 driver.options
        │
        ▼
catch_forbidden_options(driver)
        │  ├─ 禁止 UnknownSystemName 被降级为非错误
        │  └─ 禁止 IgnoreUnknownModules
```

#### 4.3.3 源码精读

先看 `fixup_options`。它通篇是「若用户未指定，则补默认」的模式：

[ src/slang_frontend.cc:3477-3522](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3477-L3522) — 注入综合默认值：

```cpp
void fixup_options(SynthesisSettings &settings, slang::driver::Driver &driver)
{
    // ① 对两个已弃用开关发诊断
    if (settings.compat_mode.has_value()) {
        /* issue diag::DeprecatedOption for --compat-mode */
    }
    if (settings.extern_modules.has_value()) {
        /* issue diag::DeprecatedOption for --extern-modules */
    }

    // ② 隐式 -D SYNTHESIS=1（除非 --no-synthesis-define）
    if (!settings.no_synthesis_define.value_or(false)) {
        driver.options.defines.push_back("SYNTHESIS=1");
    }

    // ③ 默认识别一批 translate-off 注释格式（除非 --no-default-translate-off-format）
    if (!settings.no_default_translate_off.value_or(false)) {
        auto &format_list = driver.options.translateOffOptions;
        format_list.insert(format_list.end(), {
            "pragma,synthesis_off,synthesis_on",
            "pragma,translate_off,translate_on",
            "synopsys,synthesis_off,synthesis_on",
            "synopsys,translate_off,translate_on",
            "synthesis,translate_off,translate_on",
            "xilinx,translate_off,translate_on",
        });
    }

    auto &flags = driver.options.compilationFlags;

    // ④ 默认禁用实例缓存（除非用户已显式设置该 flag）
    auto &disable_inst_caching = flags[ast::CompilationFlags::DisableInstanceCaching];
    if (!disable_inst_caching.has_value()) {
        disable_inst_caching = true;
    }
    settings.disable_instance_caching = disable_inst_caching.value();

    // ⑤ 强制：不允许引用未知模块（综合无法处理）
    flags[ast::CompilationFlags::DisallowRefsToUnknownInstances] = true;

    // ⑥ 默认 timescale = 1ns/1ns（除非用户已设）
    auto &time_scale = driver.options.timeScale;
    if (!time_scale.has_value()) {
        time_scale = "1ns/1ns";
    }
}
```

逐条理解：

- **② `SYNTHESIS=1`**：很多 IP 用 `` `ifdef SYNTHESIS `` 区分综合与仿真路径。综合前端必须自动定义它。`--no-synthesis-define` 是少数需要关掉它的场景（如调试宏展开）。
- **③ translate-off**：这一串格式是各厂商约定的「综合时跳过」注释。每条形如 `前缀,开始标记,结束标记`。比如 `pragma,translate_off,translate_on` 表示遇到 `` `pragma translate_off ... translate_on`` 就跳过。`--no-default-translate-off-format` 关闭这套默认。
- **④ 实例缓存**：slang 默认会缓存「规范实例体」（canonical body）以省内存，但 sv-elab 的层次展平与 [`get_instance_body`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L501-L507) 需要确定的实例体，因此默认禁用缓存。注意这里把结果**回写**到 `settings.disable_instance_caching`，供 [`get_instance_body`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L503) 读取：

[ src/slang_frontend.cc:501-507](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L501-L507) — 据此决定要不要用 slang 缓存的 canonical body：

```cpp
const ast::InstanceBodySymbol &get_instance_body(SynthesisSettings &settings, const ast::InstanceSymbol &instance)
{
    if (!settings.disable_instance_caching && instance.getCanonicalBody())
        return *instance.getCanonicalBody();
    else
        return instance.body;
}
```

- **⑤ `DisallowRefsToUnknownInstances`**：直接强制为 `true`，不给用户开关。因为综合时若允许引用未定义模块，下游翻译会撞上空指针/悬空符号，这是不可恢复的。

再看 `catch_forbidden_options`。它在 `processOptions` 之后运行，专门检查「某些诊断/选项是否被用户通过 slang 标准参数偷偷降级了」：

[ src/slang_frontend.cc:3526-3547](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3526-L3547) — 拦截两类危险降级：

```cpp
std::vector<slang::DiagCode> forbidden_diag_demotions = {
    slang::diag::UnknownSystemName
};

void catch_forbidden_options(slang::driver::Driver &driver) {
    slang::DiagnosticEngine &engine = driver.diagEngine;

    // 若 UnknownSystemName 被降级为非错误，则发 ForbiddenDemotion 并强制恢复为 Error
    for (auto code : forbidden_diag_demotions) {
        if (engine.getSeverity(code, slang::SourceLocation::NoLocation) !=
                slang::DiagnosticSeverity::Error) {
            slang::Diagnostic demotion_diag(diag::ForbiddenDemotion, slang::SourceLocation::NoLocation);
            demotion_diag << engine.getOptionName(code);
            engine.issue(demotion_diag);
            engine.setSeverity(slang::diag::UnknownSystemName, slang::DiagnosticSeverity::Error);
        }
    }

    // 禁止 IgnoreUnknownModules：综合无法容忍未知模块
    if (driver.options.compilationFlags[ast::CompilationFlags::IgnoreUnknownModules]) {
        engine.issue({diag::NoIgnoreUnknownModules, slang::SourceLocation::NoLocation});
    }
}
```

为什么这两条必须拦？

- **`UnknownSystemName`**：如果用户用 slang 的 `-Wno-error` 之类把「未知系统任务名」（比如拼错的 `$foo`）从错误降成警告，源码里那个调用就会被悄悄跳过，生成的网表和仿真行为就不一致了。综合前端必须把它钉死为错误。
- **`IgnoreUnknownModules`**：同理，允许「忽略未知模块」会让实例引用悬空，翻译阶段必然出错。

这正呼应了 u2-l2 里强调的「`catch_forbidden_options` 保证网表与仿真语义一致」。

最后，把 `execute()` 里这三步的真实调用顺序贴出来，确认它们的相对位置：

[ src/slang_frontend.cc:3711-3714](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3711-L3714) — `fixup` → `processOptions` → `catch` 的顺序：

```cpp
fixup_options(settings, driver);
if (!driver.processOptions())
    log_cmd_error("Bad command\n");
catch_forbidden_options(driver);
```

#### 4.3.4 代码实践

**实践目标**：通过「改一个参数、看下游行为」的方式，验证 `fixup_options` 注入的默认值确实在起作用。

**操作步骤（源码阅读型 + 待本地验证）**：

1. **追踪 `SYNTHESIS` 宏**：准备一段最小 SV，含 `` `ifdef SYNTHESIS ... `else ... `endif `` 的差异赋值。
2. 分别用 `read_slang foo.sv` 和 `read_slang --no-synthesis-define foo.sv` 读入（后者对应 [src/slang_frontend.cc:3490-3492](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3490-L3492) 跳过注入）。
3. 用 `show` 或写出到 RTLIL，对比两次产生的电路是否走了不同分支。

**需要观察的现象**：默认调用应走 `` `ifdef SYNTHESIS `` 分支（因为 `fixup_options` 自动加了 `SYNTHESIS=1`）；加 `--no-synthesis-define` 后应走 `` `else `` 分支。

**预期结果**：两次网表不同，证明 `SYNTHESIS` 宏默认被注入且可被 `--no-synthesis-define` 关闭。

**待本地验证**：若无综合环境，可改为只阅读 [src/slang_frontend.cc:3490-3492](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3490-L3492)，并解释「`no_synthesis_define` 为假时 `value_or(false)` 返回 false，`!false` 为真，于是 push_back 执行」这条逻辑链。

#### 4.3.5 小练习与答案

**练习 1**：`fixup_options` 里 `DisallowRefsToUnknownInstances` 直接赋 `true`，为什么不像 `DisableInstanceCaching` 那样判断「用户是否已设」？

> **答案**：因为 sv-elab 在翻译阶段强依赖「所有被引用的模块都有定义」，未知模块引用是不可恢复的硬错误，没有任何「尊重用户」的余地。`DisableInstanceCaching` 则允许高级用户在调试时手动重新启用缓存，所以保留了「用户优先」。

**练习 2**：`catch_forbidden_options` 为什么放在 `processOptions` **之后**而不是之前？

> **答案**：用户对诊断严重级别的降级（如 `-Wno-error=...`）是在 `processOptions` 里才被 slang 真正应用到 `diagEngine` 的。放在 `processOptions` 之后，才能读到「用户最终想要的有效严重级别」，从而判断它有没有被偷偷降级。

**练习 3**：用户能通过 sv-elab 专属开关把 `UnknownSystemName` 降级吗？

> **答案**：不能。sv-elab 专属开关里没有任何一项控制诊断严重级别；用户只能通过 slang 标准参数（如 `-W...`）去降级，而那恰恰是 `catch_forbidden_options` 监控并强制恢复的对象。

## 5. 综合实践

把本讲三个模块串起来：**写一段 `read_slang` 调用，分别启用 `--dump-ast` 与 `--keep-hierarchy`，并解释每个选项从「命令行 → 字段 → 默认值/派生值 → 下游消费」的完整链路，以及对最终网表的影响。**

具体任务：

1. **准备设计**：写一个含子模块层次的简单设计，例如顶层 `top` 实例化一个子模块 `sub`，`sub` 内有一个 `always` 组合逻辑。
2. **第一次调用（基线）**：

   ```tcl
   read_slang top.sv --top top
   show top
   ```

   记录生成的模块数量。默认情况下 [`should_dissolve`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3269-L3271) 因 `hierarchy_mode()==NONE` 返回 `true`，`sub` 会被**展平**进 `top`，最终只有一个模块。

3. **第二次调用（保留层次）**：

   ```tcl
   read_slang top.sv --top top --keep-hierarchy
   show
   ```

   现在 `hierarchy_mode()==ALL`，[`should_dissolve`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3293-L3298) 对普通模块返回 `false`，`sub` 作为独立模块保留，`top` 里出现一个 `sub` 类型的 cell。预期：模块数量从 1 变成 2（`top` + `sub`）。

4. **第三次调用（转储 AST）**：

   ```tcl
   read_slang top.sv --top top --dump-ast
   ```

   对应 [src/slang_frontend.cc:3725-3731](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3725-L3731)：在创建 `Compilation` 之后、翻译之前，把整棵 AST 以 JSON 打到 stdout。注意 `--dump-ast` **只影响是否打印 AST，不改变最终网表**——网表仍照常生成。

5. **对照源码解释**：对每个开关，写出对应的字段名、默认值、`fixup_options` 是否介入、下游消费点（行号）。例如 `--keep-hierarchy` → 字段 `keep_hierarchy` → 派生 `hierarchy_mode()==ALL` → 消费于 [`should_dissolve` 的 switch](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3269-L3301)，`fixup_options` 不介入。

**预期结果**：你能用一张表把「开关 → 字段 → 默认 → 消费点 → 网表影响」列清，并亲手观察到层次展平 vs 保留的模块数差异。

**待本地验证**：步骤 2-4 需要可运行的 Yosys + sv-elab 环境；若无环境，可退化为「源码阅读型」——只完成步骤 5 的表格，并标注「网表影响」一列为「据源码推断，待本地验证」。

> 安全提示：README 与 [`addOptions`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L92-L93) 都标注 `--keep-hierarchy` 是「experimental; may crash」。若你的设计较复杂，可能触发崩溃，建议先用最小设计试。

## 6. 本讲小结

- `SynthesisSettings` 是「sv-elab 专属选项的内存表示」，几乎全用 `std::optional<bool>`/`optional<int>` 来保留「用户未指定」态；`blackboxed_modules` 是可重复集合、`disable_instance_caching` 是由 `fixup_options` 回写的普通 `bool`。
- `addOptions` 用 slang 的 `cmdLine.add(...)` 把每个字段绑定到一个 `--xxx` 开关；布尔、带值、可重复（lambda）、枚举（`addEnum`）四种注册方式各对应一类开关。`help read_slang` 的输出直接来自这里。
- 派生方法 `hierarchy_mode()`（`NONE`/`BEST_EFFORT`/`ALL`，`--keep-hierarchy` 优先于 `--best-effort-hierarchy`）与 `unroll_limit()`（默认 4000）把字段换算成下游真正使用的值。
- `fixup_options` 夹在 `parseCommandLine` 与 `processOptions` 之间，负责「在尊重用户显式值的前提下」注入综合必备默认值：`SYNTHESIS=1` 宏、translate-off 注释格式、禁用实例缓存、禁止引用未知实例、`1ns/1ns` timescale。
- `catch_forbidden_options` 在 `processOptions` 之后兜底，禁止把 `UnknownSystemName` 降级、禁止 `IgnoreUnknownModules`，以保证网表与仿真语义一致。
- 两个已弃用开关 `--compat-mode`、`--extern-modules` 仍被接受但恒为开启，`fixup_options` 会针对它们发出 `diag::DeprecatedOption` 弃用诊断。

## 7. 下一步学习建议

- 本讲只点到了「每个开关在哪里被消费」，但没有展开消费逻辑。建议下一讲学 **u2-l4 诊断系统**，理解 `fixup_options` 与 `catch_forbidden_options` 里反复出现的 `diag::DeprecatedOption`、`diag::ForbiddenDemotion`、`diag::NoIgnoreUnknownModules` 等诊断码是如何注册与上报的。
- 想深入「层次」相关三个开关的真实效果，可跳到 **u7-l2 层次处理**，那里会完整讲解 [`should_dissolve`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/slang_frontend.cc#L3226-L3302) 与 `HierarchyQueue`。
- 想深入 `--unroll-limit`、`--ignore-timing`、`--ignore-initial`、`--allow-dual-edge-ff` 等过程块/时序相关开关，可在学完单元 5（过程块建模）后，对照 **u6 时序逻辑** 与 [`UnrollLimitTracking::unroll_tick`](https://github.com/povik/yosys-slang/blob/3dddccd478618d68f8a5e160fb4b5783c4da35d4/src/procedural.cc#L520-L536) 阅读它们的真实消费点。
- 想验证选项行为，最直接的方式是跑 **u8-l1 测试体系** 里介绍的等价性测试范式，用 `read_slang <选项>` 配合 `equiv_make`/`equiv_induct` 观察选项对网表的影响。
