# CLI 入口与命令行选项

## 1. 本讲目标

通过上一篇你已经能把 njs 「跑起来」（`./configure && make njs → build/njs`），并知道 CLI 可以脱离 NGINX 独立运行。本讲不再讨论「怎么编译」，而是深入到 CLI 程序自身的源码，搞清楚：

- 命令行程序的真实入口在哪里（`main` 函数），它把命令行参数加工成了什么结构。
- 所有命令行选项（`-c`/`-d`/`-e`/`-n`/`-m`/`-p`/`-q`/`-r`/`-s`/`-v`/`-a`/`-j`/`-o`/`-h` 等）分别映射到哪个字段、产生什么行为。
- 环境变量（`NJS_ENGINE`、`NJS_PATH`、`NJS_EXIT_CODE` 等）如何在不修改命令行的情况下改变运行行为。
- `-n` 选项是如何在「内置 njs 引擎」和「QuickJS 引擎」之间做切换，并据此决定哪些选项可用。

学完后，你应该能：看到一行 `./build/njs -d -n njs -c '...'` 命令时，在脑中（或对照源码）画出它从 `main` 到真正执行脚本的完整调用路径，并能针对不同需求选择正确的选项组合。

## 2. 前置知识

本讲假设你已经掌握上一篇（u1-l3）的内容，特别是：

- njs 有两种交付形态：嵌入 NGINX 的模块、独立 CLI（`build/njs`）。本讲只讨论 CLI。
- CLI 内部可以选用两套可互换的引擎：自 1.0.0 起弃用的内置 **njs 引擎**（ES5.1 strict 子集），以及官方推荐的 **QuickJS 引擎**（ES2023）。
- QuickJS 是可选链接的外部库，只有编译时探测到（`NJS_HAVE_QUICKJS`）才能用 `-n QuickJS`。

此外需要一点 C 语言常识：

- **命令行参数**：C 程序的入口 `int main(int argc, char **argv)` 中，`argc` 是参数个数，`argv` 是参数字符串数组，`argv[0]` 通常是程序名本身。
- **switch/case**：C 里常见的多分支选择语句，本讲会看到它被用来逐个字符地匹配 `-c`、`-d` 等选项。
- **`getenv`**：C 标准库函数，读取环境变量的值。

不需要你熟悉 njs 的 VM 内部，本讲只走到「创建并启动引擎」这一步就停下来——VM 内部的生命周期（创建/编译/执行/销毁）属于第二单元（u2-l1）。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开：

| 文件 | 作用 |
|---|---|
| [external/njs_shell.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c) | CLI 的全部实现：`main` 入口、选项结构 `njs_opts_t`、选项解析 `njs_options_parse`、环境变量读取、引擎分发 `njs_create_engine`、三种运行模式（交互式 / `-c` 命令 / 文件）的调度。 |

辅助参考资料（用于理解选项的副作用，但不是本讲主线）：

- `auto/options`：定义 `--debug-opcode` / `--debug-generator` 等**编译期**开关，它们决定了运行期 `-o` / `-g` 选项是否可用。
- `README.md`：给出 CLI 的用法示例（交互式与非交互式）。

需要特别说明：CLI 源码里大量代码是「QuickJS 专属」的（用 `#if (NJS_HAVE_QUICKJS)` 包裹），还有一整块是 fuzzer（模糊测试）入口（用 `#ifndef NJS_FUZZER_TARGET` 包裹）。本讲聚焦于**正常 CLI 路径**，即 `main → njs_options_parse → njs_main → njs_create_engine` 这条主线。

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：

1. **main 入口与 njs_opts_t**：程序从哪里开始，命令行参数被加工成什么结构。
2. **选项解析 njs_options_parse**：逐个选项看它写入了哪个字段、做了什么。
3. **环境变量与引擎分发**：`NJS_*` 环境变量的作用，以及 `-n` 如何在两套引擎之间切换。

### 4.1 main 入口与 njs_opts_t 选项结构

#### 4.1.1 概念说明

任何命令行程序都要解决同一个问题：把一串「字符串形式的参数」（`-d -c 'console.log(1)'`）翻译成「程序内部能用的、结构化的配置」。njs 的做法很经典：

1. 定义一个结构体 `njs_opts_t`，每个字段对应一个开关或一个值。
2. 写一个 `njs_options_parse` 函数，遍历 `argv`，逐个把选项填进这个结构体。
3. 后续所有逻辑（创建引擎、决定是否反汇编、决定模块模式……）都只读这个结构体，不再关心原始的命令行字符串。

这样做的好处是**关注点分离**：解析逻辑集中在 `njs_options_parse`，业务逻辑只依赖结构体字段。你以后看 NGINX 集成（u8 单元）会发现，nginx 指令（`js_engine`、`js_import`）走的是完全不同的解析路径，但最终也是把配置塞进类似的「上下文结构体」里。

#### 4.1.2 核心流程

CLI 的顶层调用链可以概括为：

```text
main(argc, argv)                         # 真正的 C 入口
  └─ njs_memzero(&opts, ...)             # 把选项结构体清零
  └─ opts.interactive = 1                # 默认进入交互式
  └─ njs_options_parse(&opts, argc, argv)# 解析命令行 → 填充 opts
  └─ 若 stdin 不是终端 → 关闭交互式, 改读 stdin
  └─ 若 -v → 打印版本号后退出
  └─ njs_main(&opts)                     # 根据 opts 选择运行模式
        ├─ opts.command 非空 (-c)        → njs_create_engine + njs_process_script
        ├─ opts.interactive (无参数)     → njs_interactive_shell (REPL)
        └─ 其它 (给了文件名/ -)          → njs_process_file
  └─ njs_options_free(&opts)             # 释放解析期分配的内存
  └─ return exit_code
```

注意一个细节：`main` 一开始就把 `opts.interactive = 1`，也就是说**默认值是「进入交互式 shell」**。只有当解析到 `-c`、文件名等参数时，`interactive` 才被显式清零；或者当 stdin 不是终端（比如用管道喂入脚本）时，`main` 会再次强制关闭交互式并改从 stdin 读。

#### 4.1.3 源码精读

先看真正的 C 入口 `main`：

[njs_shell.c:467-500 main 函数：CLI 的真正入口](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L467-L500)

```c
int
main(int argc, char **argv)
{
    njs_int_t   ret;
    njs_opts_t  opts;

    njs_memzero(&opts, sizeof(njs_opts_t));
    opts.interactive = 1;

    ret = njs_options_parse(&opts, argc, argv);
    ...
    if (opts.interactive && !isatty(STDIN_FILENO)) {
        opts.interactive = 0;
        opts.file = (char *) "-";
    }

    if (opts.version != 0) {
        njs_printf("%s\n", NJS_VERSION);
        ret = NJS_OK;
        goto done;
    }

    ret = njs_main(&opts);

done:
    njs_options_free(&opts);
    return (ret == NJS_OK) ? EXIT_SUCCESS : opts.exit_code;
}
```

要点解读：

- `njs_memzero` 把整个 `opts` 清零，保证所有开关默认为 `0`/`false`，避免读到未初始化内存。
- `opts.interactive = 1`：默认值是「交互式」。这是为什么直接敲 `./build/njs`（不带任何参数）会进入 REPL。
- `!isatty(STDIN_FILENO)`：检测标准输入是不是「真正的终端」。当用 `echo "2**3" | ./build/njs` 这种管道喂脚本时，stdin 不是终端，于是关闭交互式、把 `file` 设为 `"-"`（表示从 stdin 读）。这正好对应 README 里的例子 `echo "2**3" | njs -q`。
- `opts.version`：`-v` 选项，直接打印 `NJS_VERSION` 后走 `done`，根本不创建引擎。
- 返回值用 `opts.exit_code`，这个字段的默认值与 `-e` 选项、`NJS_EXIT_CODE` 环境变量有关（见 4.3 节）。

接下来看选项结构体本身。这是「命令行的内部表示」：

[njs_shell.c:36-65 njs_opts_t：命令行选项的内部表示](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L36-L65)

```c
typedef struct {
    uint8_t                 disassemble;       /* -d 反汇编 */
    uint8_t                 denormals;         /* -f denormals */
    uint8_t                 interactive;       /* 交互式 */
    uint8_t                 module;            /* -m ES 模块 */
    uint8_t                 quiet;             /* -q 关闭提示 */
    uint8_t                 sandbox;           /* -s 沙箱 */
    uint8_t                 safe;              /* -u 安全模式 */
    uint8_t                 version;           /* -v 版本 */
    uint8_t                 ast;               /* -a 打印 AST */
    uint8_t                 unhandled_rejection;/* -r 忽略未处理拒绝 */
    uint8_t                 suppress_stdout;
    uint8_t                 opcode_debug;      /* -o opcode 跟踪 */
    uint8_t                 generator_debug;   /* -g 生成器调试 */
    uint8_t                 can_block;         /* NJS_CAN_BLOCK */
    int                     exit_code;         /* -e 失败退出码 */
    int                     stack_size;        /* -j 栈大小 */

    char                    *file;             /* 脚本文件名 */
    njs_str_t               command;           /* -c 的内联代码 */
    size_t                  n_paths;           /* -p/NJS_PATH 路径数 */
    njs_str_t               *paths;
    char                    **argv;            /* 传给脚本的 argv */
    njs_uint_t              argc;

    enum {
        NJS_ENGINE_NJS = 0,
        NJS_ENGINE_QUICKJS = 1,
    }                       engine;            /* -n / NJS_ENGINE */
} njs_opts_t;
```

可以看到，几乎每个命令行选项都对应结构体里一个同名字段。其中最关键的是末尾的 `engine` 枚举——它就是 `-n` 选项的归宿，取值 `NJS_ENGINE_NJS`（默认）或 `NJS_ENGINE_QUICKJS`。整个 CLI 后续的一切「双引擎」分支判断，本质上都在读这一个字段。

`main` 把解析后的 `opts` 交给 `njs_main`，由它决定运行模式：

[njs_shell.c:410-462 njs_main：根据 opts 选择运行模式](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L410-L462)

```c
static njs_int_t
njs_main(njs_opts_t *opts)
{
    ...
    if (opts->file == NULL) {
        if (opts->command.length != 0) {
            opts->file = (char *) "string";     /* -c 模式 */
        }
        else if (opts->interactive) {
            opts->file = (char *) "shell";      /* REPL 模式 */
        }
        ...
    }

    ret = njs_console_init(opts, &njs_console);

    if (opts->interactive) {
        ret = njs_interactive_shell(opts);      /* 交互式 REPL */
    } else if (opts->command.length != 0) {
        engine = njs_create_engine(opts);
        ret = njs_process_script(engine, &njs_console, &opts->command);  /* -c */
    } else {
        ret = njs_process_file(opts);           /* 文件 / stdin */
    }

    return ret;
}
```

注意 `opts->file` 在这里被赋予一个**逻辑文件名**（`"string"` / `"shell"` / 真实路径），它后面会传给引擎作为脚本来源标识，用于错误信息（比如 `SyntaxError at string:1:3`）。这解释了为什么用 `-c` 跑出错时，错误位置里的文件名是 `string` 而不是真实文件。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「默认交互式」与「`-v` 早退」这两条最简单的分支。

**操作步骤**：

1. 在已构建好的仓库里（产物为 `build/njs`），运行：
   ```bash
   ./build/njs -v
   ```
2. 不带任何参数运行，观察提示符：
   ```bash
   ./build/njs
   ```
3. 用管道喂一行脚本，观察它**不**进入交互式：
   ```bash
   echo "2**3" | ./build/njs -q
   ```

**需要观察的现象**：

- 第 1 步应打印一行版本号（如 `1.0.0`，具体数字待本地验证）后立即退出，对应源码里 `opts.version != 0 → goto done` 的早退分支。
- 第 2 步进入交互式，提示符为 `>> `（由 `njs_interactive_shell` 中的 `rl_callback_handler_install(">> ", ...)` 设置）。
- 第 3 步因为 stdin 不是终端（`!isatty`），`main` 关闭了交互式并从 stdin 读取，输出 `8`。

**预期结果**：第 1、3 步有确定输出；交互式提示符外观与本机 readline/editline 实现有关，若构建时未装 `libedit-dev`/`readline`，交互式可能被禁用（源码里用 `#ifdef NJS_HAVE_READLINE` 控制）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `main` 要在解析之前就把 `opts.interactive` 设为 `1`，而不是依赖 `njs_memzero` 把它清成 `0`？

**参考答案**：因为「无参数运行 → 进入交互式 REPL」是 CLI 的默认行为。`njs_memzero` 会把字段清成 0（即「非交互式」），如果不显式置 1，直接敲 `./build/njs` 就会因为没有文件、没有 `-c`、又「非交互式」而走到 `file name is required` 的报错分支。显式置 1 让「交互式」成为默认，只有解析到 `-c`/文件名等参数时才被清零。

**练习 2**：`opts.file` 在 `-c` 模式下被设成字符串 `"string"`，这个值在后续会起什么作用？

**参考答案**：它作为「脚本来源标识」传给引擎（`vm_options.file`），主要用于编译/运行期的错误定位信息，比如语法错误会报成 `at string:行:列`，让用户知道这段代码来自 `-c` 的内联字符串而不是某个文件。它不影响代码的实际内容。

---

### 4.2 选项解析 njs_options_parse

#### 4.2.1 概念说明

`njs_options_parse` 是本讲的「重头戏」。它做三件事：

1. 先读取若干**环境变量**并写入 `opts`（环境变量是「默认值的一种来源」，优先级低于命令行选项）。
2. 设置一批**硬编码默认值**（如 `denormals=1`、`can_block=1`、`engine=NJS_ENGINE_NJS`）。
3. 遍历 `argv`，用一个大的 `switch` 语句逐个字符匹配选项，把对应字段填上。

它还负责一个容易踩坑的校验：QuickJS 引擎**不支持** `-a`/`-d`/`-g`/`-o`/`-s`/`-u` 这几个选项，如果同时用了 `-n QuickJS` 和这些选项，解析结束前会直接报错退出。

#### 4.2.2 核心流程

```text
njs_options_parse(opts, argc, argv)
  1. 设置硬编码默认值: denormals=1, can_block=1,
     exit_code=EXIT_FAILURE, engine=NJS_ENGINE_NJS,
     unhandled_rejection=1
  2. 读取环境变量: NJS_EXIT_CODE / NJS_CAN_BLOCK /
                   NJS_LOAD_AS_MODULE / NJS_ENGINE / NJS_PATH
  3. for 每个 argv[i]:
        若不是 '-' 开头（或就是 '-'）→ 当作文件名, 跳出
        否则取 argv[i][1] 作为选项字符, switch 分发:
          'a' → ast=1         'c' → command=argv[++i]
          'd' → disassemble=1 'e' → exit_code=atoi(argv[++i])
          'f' → denormals=0   'g' → generator_debug=1  (条件编译)
          'j' → stack_size    'm' → module=1
          'n' → engine=parse_engine(argv[++i])
          'o' → opcode_debug=1 (条件编译)
          'p' → add_path(argv[++i])
          'q' → quiet=1       'r' → unhandled_rejection=0
          's' → sandbox=1     't' → module/script 类型
          'u' → safe=1        'v'/'V' → version=1
          'h'/'?' → 打印 help, 返回 NJS_DONE
  4. 若 engine==QUICKJS 且用了 -a/-d/-g/-o/-s/-u → 报错
  5. 组装传给脚本的 argv[] 数组
```

其中几个返回值约定值得记住：`NJS_OK` 表示正常解析完成；`NJS_DONE` 表示「打印完帮助/版本后就该退出」（`-h` 走这条路）；`NJS_ERROR` 表示解析出错（未知选项、缺参数等）。`main` 据此决定是继续还是退出。

#### 4.2.3 源码精读

首先是 `help` 文本和默认值设置。`help` 文本是认识所有选项最快的「权威清单」：

[njs_shell.c:511-550 help 文本与默认值](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L511-L550)

```c
static const char  help[] =
    "njs [options] [-c string | script.js | -] [script args]\n"
    ...
    "Options:\n"
    "  -a                print AST.\n"
    "  -c                specify the command to execute.\n"
    "  -d                print disassembled code.\n"
    "  -e <code>         set failure exit code.\n"
    "  -f                disabled denormals mode.\n"
    "  -g                enable generator debug.\n"   /* 条件编译 */
    "  -j <size>         set the maximum stack size in bytes.\n"
    "  -m                load as ES6 module (script is default).\n"
    "  -n njs|QuickJS    set JS engine (njs is default)\n"
    "  -o                enable opcode debug.\n"      /* 条件编译 */
    "  -p <path>         set path prefix for modules.\n"
    "  -q                disable interactive introduction prompt.\n"
    "  -r                ignore unhandled promise rejection.\n"
    "  -s                sandbox mode.\n"
    "  -v                print njs version and exit.\n"
    "  -u                disable \"unsafe\" mode.\n"
    "  script.js | -     run code from a file or stdin.\n";

opts->denormals = 1;
opts->can_block = 1;
opts->exit_code = EXIT_FAILURE;
opts->engine = NJS_ENGINE_NJS;
opts->unhandled_rejection = 1;
```

注意几个细节：

- `-g`（generator debug）和 `-o`（opcode debug）这两行被 `#ifdef NJS_DEBUG_GENERATOR` / `#ifdef NJS_DEBUG_OPCODE` 包裹。也就是说，**它们只有在你用 `./configure --debug-generator=YES` / `--debug-opcode=YES` 编译时才存在**。这一点非常容易让初学者困惑：「为什么文档里有 `-o`，我的 `build/njs` 却报 `Unknown argument`？」——答案就在 `auto/options` 的 `--debug-opcode`/`--debug-generator` 开关里。
- `-n njs|QuickJS` 这一行也被 `#ifdef NJS_HAVE_QUICKJS` 包裹：没链接 QuickJS 的构建里，根本没有 `-n` 选项。
- 默认 `engine = NJS_ENGINE_NJS`：即便你链接了 QuickJS，**不指定 `-n` 时默认仍是内置 njs 引擎**。

接着是核心的 `switch` 循环。这里只摘几段最具代表性的分支：

[njs_shell.c:596-632 选项主循环与 -a/-c/-d 分支](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L596-L632)

```c
for (i = 1; i < argc; i++) {
    p = argv[i];

    if (p[0] != '-' || (p[0] == '-' && p[1] == '\0')) {
        opts->interactive = 0;
        opts->file = argv[i];          /* 第一个非选项参数 = 文件名 */
        goto done;
    }

    p++;
    switch (*p) {
    case '?':
    case 'h':
        njs_printf("%*s", njs_length(help), help);
        return NJS_DONE;

    case 'a':
        opts->ast = 1;                 /* 打印 AST */
        break;

    case 'c':
        opts->interactive = 0;
        if (++i < argc) {              /* -c 需要一个参数 */
            opts->command.start = (u_char *) argv[i];
            opts->command.length = njs_strlen(argv[i]);
            goto done;                 /* -c 后面整段都是脚本, 不再解析 */
        }
        njs_stderror("option \"-c\" requires argument\n");
        return NJS_ERROR;

    case 'd':
        opts->disassemble = 1;         /* 反汇编 */
        break;
    ...
```

几个值得记住的模式：

- **「第一个非 `-` 开头的参数即文件名」**：`p[0] != '-'` 这一判定让 `./build/njs foo.js` 把 `foo.js` 当文件，并立刻关闭交互式。`-`（单个短横）是特例，表示「从 stdin 读」。
- **需要参数的选项用 `++i` 取下一个 argv**：`-c`、`-e`、`-j`、`-n`、`-p`、`-t` 都这样。若 `++i >= argc` 说明用户漏了参数，报错返回 `NJS_ERROR`。
- **`-c` 后用 `goto done`**：这是因为 `-c` 后面的整个字符串就是脚本内容，里面可能含有任何字符（包括看起来像选项的），不应再被当作选项解析。

`-n` 是本讲最重要的选项，它调用专门的引擎解析函数：

[njs_shell.c:665-676 -n 选项分支](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L665-L676)

```c
case 'n':
    if (++i < argc) {
        ret = njs_options_parse_engine(opts, argv[i]);
        if (ret != NJS_OK) {
            return NJS_ERROR;
        }
        break;
    }
    njs_stderror("option \"-n\" requires argument\n");
    return NJS_ERROR;
```

[njs_shell.c:796-813 njs_options_parse_engine：把字符串映射成枚举](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L796-L813)

```c
static njs_int_t
njs_options_parse_engine(njs_opts_t *opts, const char *engine)
{
    if (strncasecmp(engine, "njs", 3) == 0) {
        opts->engine = NJS_ENGINE_NJS;

    } else if (strncasecmp(engine, "QuickJS", 7) == 0) {
        opts->engine = NJS_ENGINE_QUICKJS;     /* 仅 NJS_HAVE_QUICKJS 时 */

    } else {
        njs_stderror("unknown engine \"%s\"\n", engine);
        return NJS_ERROR;
    }
    return NJS_OK;
}
```

`strncasecmp` 表示**大小写不敏感**，所以 `-n njs`、`-n NJS`、`-n QuickJS`、`-n quickjs` 都能识别。注意 `QuickJS` 分支被 `#ifdef NJS_HAVE_QUICKJS` 包裹——在没链接 QuickJS 的构建里，写 `-n QuickJS` 会落入 `else` 报 `unknown engine`。

最后是循环结束后的**兼容性校验**，这是初学者最容易踩的坑：

[njs_shell.c:745-777 QuickJS 不支持的选项校验](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L745-L777)

```c
if (opts->engine == NJS_ENGINE_QUICKJS) {
    if (opts->ast) {
        njs_stderror("option \"-a\" is not supported for quickjs\n");
        return NJS_ERROR;
    }
    if (opts->disassemble) {
        njs_stderror("option \"-d\" is not supported for quickjs\n");
        return NJS_ERROR;
    }
    ... /* 同样拒绝 -g / -o / -s / -u */
}
```

原因很直观：`-a`（AST）、`-d`（反汇编）、`-g`（生成器调试）、`-o`（opcode 跟踪）都是针对**内置 njs 引擎的字节码/AST 中间表示**的调试手段；QuickJS 有自己完全不同的字节码格式，这些选项对它没有意义。`-s`（sandbox）和 `-u`（unsafe）也是 njs 引擎专属的安全开关。所以**反汇编、AST、opcode 调试只能在 `-n njs` 下使用**——这是本讲综合实践的关键前提。

#### 4.2.4 代码实践

**实践目标**：用 `./build/njs -h` 取得「权威选项清单」，再亲手触发一次 QuickJS 兼容性报错，加深对上面那段校验的记忆。

**操作步骤**：

1. 打印帮助：
   ```bash
   ./build/njs -h
   ```
   对照源码里的 `help[]` 字符串，确认你本机构建是否含 `-g`/`-o`/`-n` 三行（取决于编译期开关与是否链接 QuickJS）。
2. 故意触发兼容性报错（前提：本机已链接 QuickJS）：
   ```bash
   ./build/njs -n QuickJS -d -c 'console.log(1)'
   ```
3. 对照正确用法，反汇编只能在 njs 引擎下：
   ```bash
   ./build/njs -n njs -d -c 'var a=42; function f(v){return v+1}'
   ```

**需要观察的现象**：

- 第 1 步打印的 help 应与源码 `help[]` 一致。
- 第 2 步应在解析阶段就报 `option "-d" is not supported for quickjs` 并以非零码退出，**根本不会执行脚本**。
- 第 3 步会先把脚本反汇编打印到 stdout（包含 `MOVE`/`ADDITION`/`RETURN`/`STOP` 之类的助记符与十六进制操作数），具体指令序列取决于 njs 版本，待本地验证。

**预期结果**：第 1、2 步行为确定；第 3 步的反汇编输出格式可参考 `docs/agent/engine-dev.md` 的字节码示例，本讲不要求逐条读懂（那是 u3-l5「字节码格式与反汇编」的内容），只需确认「`-d` 在 njs 引擎下确实产出了反汇编」即可。

> 说明：本讲只关心「`-d` 选项被正确解析并打开了 `disassemble` 开关」；反汇编输出的具体含义留给第三单元。

#### 4.2.5 小练习与答案

**练习 1**：用户执行 `./build/njs -d -n QuickJS -c 'x'`，会发生什么？为什么？

**参考答案**：会报 `option "-d" is not supported for quickjs` 并以失败码退出，脚本不会执行。因为 `njs_options_parse` 在主循环结束后有一段兼容性校验：一旦 `engine == NJS_ENGINE_QUICKJS`，就禁止 `-a`/`-d`/`-g`/`-o`/`-s`/`-u`。这些选项都依赖内置 njs 引擎的中间表示（AST/字节码）或安全模型，QuickJS 不提供等价物。

**练习 2**：为什么 `-c` 分支里取到脚本字符串后要用 `goto done`，而不是 `break` 继续循环？

**参考答案**：`-c` 的参数本身就是一段任意 JS 源码，里面可能含有以 `-` 开头的子串（比如 `console.log("-v")`）。如果继续循环解析，这些子串会被误当成命令行选项。`goto done` 直接跳出解析循环，保证 `-c` 之后的整段字符串原封不动地作为脚本内容。

**练习 3**：`-e 5` 这个选项影响的是什么？它和返回值有什么关系？

**参考答案**：`-e` 设置的是 `opts->exit_code`，即脚本**运行失败时**进程的退出码。在 `main` 末尾，`return (ret == NJS_OK) ? EXIT_SUCCESS : opts.exit_code;`——成功返回 0，失败返回 `exit_code`。默认值是 `EXIT_FAILURE`（通常为 1），用 `-e 5` 可让失败时返回 5，方便在外层脚本/CI 里据退出码区分错误。

---

### 4.3 环境变量与引擎分发

#### 4.3.1 概念说明

除了命令行选项，CLI 还读取一组 `NJS_*` 环境变量。它们的作用是「在不修改命令行的前提下提供默认值」。需要特别注意优先级：**命令行选项会覆盖环境变量**。这是因为环境变量在 `njs_options_parse` 一开始、主 `for` 循环**之前**就被读入 `opts`；随后循环里的 `-n`、`-p` 等会覆盖这些值。

njs 读取的环境变量有五个：

| 环境变量 | 写入字段 | 等价命令行选项 | 含义 |
|---|---|---|---|
| `NJS_ENGINE` | `engine` | `-n` | 选择引擎 |
| `NJS_PATH` | `paths`（可多个） | `-p`（可多次） | 模块搜索路径，冒号分隔 |
| `NJS_EXIT_CODE` | `exit_code` | `-e` | 失败退出码 |
| `NJS_CAN_BLOCK` | `can_block` | （无） | QuickJS 运行时是否允许阻塞（`JS_SetCanBlock`） |
| `NJS_LOAD_AS_MODULE` | `module=1` | `-m` / `-t module` | 默认按 ES 模块加载 |

环境变量解析完后，`opts->engine` 已经确定了用哪套引擎。真正「按引擎分发」发生在 `njs_create_engine`：它是一个 `switch(opts->engine)`，为每套引擎填充一组**函数指针**（`eval`/`destroy`/`output` 等），此后上层代码只通过这些函数指针调用引擎，完全不关心底层是 njs 还是 QuickJS。这是一种典型的「用函数指针表实现多态」的手法。

#### 4.3.2 核心流程

```text
# 环境变量读取（位于 njs_options_parse 开头，主循环之前）
getenv("NJS_EXIT_CODE")   → opts.exit_code
getenv("NJS_CAN_BLOCK")   → opts.can_block
getenv("NJS_LOAD_AS_MODULE") → opts.module = 1
getenv("NJS_ENGINE")      → njs_options_parse_engine(opts, 值)
getenv("NJS_PATH")        → 按 ':' 切分, 逐段 njs_options_add_path

# 引擎分发（位于 njs_create_engine）
switch (opts->engine):
  case NJS_ENGINE_NJS:     njs_engine_njs_init()   → 创建 njs_vm_t
                           绑定 njs_engine_njs_* 函数指针
  case NJS_ENGINE_QUICKJS: njs_engine_qjs_init()   → 创建 JSRuntime/JSContext
                           绑定 njs_engine_qjs_* 函数指针
```

#### 4.3.3 源码精读

环境变量读取位于 `njs_options_parse` 中、默认值设置之后、主循环之前：

[njs_shell.c:552-594 五个 NJS_* 环境变量的读取](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L552-L594)

```c
p = getenv("NJS_EXIT_CODE");
if (p != NULL) {
    opts->exit_code = atoi(p);
}

p = getenv("NJS_CAN_BLOCK");
if (p != NULL) {
    opts->can_block = atoi(p);
}

p = getenv("NJS_LOAD_AS_MODULE");
if (p != NULL) {
    opts->module = 1;
}

p = getenv("NJS_ENGINE");
if (p != NULL) {
    ret = njs_options_parse_engine(opts, p);   /* 复用 -n 的解析逻辑 */
    if (ret != NJS_OK) {
        return NJS_ERROR;
    }
}

start = getenv("NJS_PATH");
if (start != NULL) {
    for ( ;; ) {                                /* 按 ':' 切分成多段 */
        p = (char *) njs_strchr(start, ':');
        len = (p != NULL) ? (size_t) (p - start) : njs_strlen(start);
        ret = njs_options_add_path(opts, start, len);
        ...
        if (p == NULL) { break; }
        start = p + 1;
    }
}
```

要点：

- `NJS_ENGINE` 复用了 `-n` 选项的 `njs_options_parse_engine` 函数，所以环境变量和命令行选项接受完全相同的取值（`njs` / `QuickJS`，大小写不敏感）。
- `NJS_PATH` 用 `:` 分隔（与 `PATH` 风格一致），逐段调用 `njs_options_add_path` 追加进 `opts->paths` 数组。这个数组在脚本里 `import`/`require` 模块时用于搜索（见 `njs_module_lookup`）。
- 因为这些读取在主循环之前，所以**命令行的 `-n`/`-p` 会覆盖环境变量**。例如 `NJS_ENGINE=QuickJS ./build/njs -n njs ...` 最终用的是 njs 引擎。

引擎分发集中在 `njs_create_engine`：

[njs_shell.c:3213-3277 njs_create_engine：按 engine 填充函数指针表](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L3213-L3277)

```c
static njs_engine_t *
njs_create_engine(njs_opts_t *opts)
{
    ...
    engine = njs_mp_zalloc(mp, sizeof(njs_engine_t));
    ...
    engine->pool = mp;
    njs_console.engine = engine;

    switch (opts->engine) {
    case NJS_ENGINE_NJS:
        ret = njs_engine_njs_init(engine, opts);    /* 创建 njs_vm_t */
        ...
        engine->type = NJS_ENGINE_NJS;
        engine->eval = njs_engine_njs_eval;
        engine->execute_pending_job = njs_engine_njs_execute_pending_job;
        engine->process_events = njs_engine_njs_process_events;
        engine->destroy = njs_engine_njs_destroy;
        engine->output = njs_engine_njs_output;
        engine->complete = njs_engine_njs_complete;
        break;

    case NJS_ENGINE_QUICKJS:
        ret = njs_engine_qjs_init(engine, opts);    /* 创建 JSRuntime/JSContext */
        ...
        engine->type = NJS_ENGINE_QUICKJS;
        engine->eval = njs_engine_qjs_eval;
        ...
        engine->destroy = njs_engine_qjs_destroy;
        ...
        break;
    }

    return engine;
}
```

`njs_engine_t` 结构体（见 [njs_shell.c:137-164](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L137-L164)）里有一组函数指针：`eval`、`execute_pending_job`、`unhandled_rejection`、`process_events`、`destroy`、`output`、`complete`。这就是 CLI 对两套引擎做的**统一抽象**：无论底层是 njs 还是 QuickJS，上层 `njs_process_script` 只需调用 `engine->eval(engine, script)`、`engine->destroy(engine)`，不必关心差异。

两套初始化各自长什么样？以 njs 引擎为例，它把 `opts` 的开关翻译成 `njs_vm_opt_t`，再调用 `njs_vm_create`：

[njs_shell.c:1315-1374 njs_engine_njs_init：把 opts 翻译成 VM 选项](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L1315-L1374)

```c
static njs_int_t
njs_engine_njs_init(njs_engine_t *engine, njs_opts_t *opts)
{
    njs_vm_t      *vm;
    njs_vm_opt_t  vm_options;

    njs_vm_opt_init(&vm_options);
    vm_options.file = ...opts->file...;
    vm_options.interactive = opts->interactive;
    vm_options.disassemble = opts->disassemble;   /* -d 在这里生效 */
    vm_options.sandbox = opts->sandbox;           /* -s */
    vm_options.unsafe = !opts->safe;              /* -u */
    vm_options.module = opts->module;             /* -m */
    vm_options.ast = opts->ast;                   /* -a */
    vm_options.opcode_debug = opts->opcode_debug; /* -o */
    vm_options.generator_debug = opts->generator_debug; /* -g */
    ...
    vm = njs_vm_create(&vm_options);              /* 真正创建 VM */
    ...
}
```

可以看到，本讲讨论的所有开关（`-d`/`-s`/`-u`/`-m`/`-a`/`-o`/`-g`）最终都通过这条路径流入 VM。`njs_vm_create` 内部如何消费这些开关（比如 `disassemble` 如何触发反汇编打印）属于第二单元（u2-l1）的内容，本讲到此为止。

QuickJS 侧的初始化 `njs_engine_qjs_init`（[njs_shell.c:2713-2783](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L2713-L2783)）走的是另一条路：`JS_NewRuntime()` + `qjs_new_context()`，并且会用 `JS_SetCanBlock(rt, opts->can_block)` 应用 `NJS_CAN_BLOCK`。它不读 `disassemble`/`ast` 等字段——这正是 4.2 节那段兼容性校验存在的理由。

#### 4.3.4 代码实践

**实践目标**：用环境变量代替命令行选项切换引擎，并验证「命令行覆盖环境变量」的优先级。

**操作步骤**：

1. 用 `NJS_ENGINE` 切换到 QuickJS（前提：已链接 QuickJS），打印引擎类型。CLI 没有直接「打印当前引擎」的选项，但可以利用 4.2 节的兼容性校验间接确认：
   ```bash
   NJS_ENGINE=QuickJS ./build/njs -c 'console.log(1)'
   # 再故意加上 -d, 应报 "not supported for quickjs", 证明环境变量生效
   NJS_ENGINE=QuickJS ./build/njs -d -c 'console.log(1)'
   ```
2. 验证命令行覆盖环境变量：
   ```bash
   NJS_ENGINE=QuickJS ./build/njs -n njs -d -c 'var a=42'
   ```
   此时 `-n njs` 覆盖了 `NJS_ENGINE=QuickJS`，`-d` 应当**正常**反汇编而不是报错。
3. 用 `NJS_PATH` 给模块搜索提供一个目录（准备一个 `m.js` 导出某个值，再从 `-c` 脚本里 `import` 它），观察路径是否生效。这一步需要 ES 模块语法，待本地验证。

**需要观察的现象**：

- 第 1 步第二条命令报 `option "-d" is not supported for quickjs`，说明 `NJS_ENGINE=QuickJS` 确实把 `opts.engine` 设成了 QuickJS。
- 第 2 步不报错且产出反汇编，说明 `-n njs` 覆盖了环境变量。

**预期结果**：第 1、2 步行为确定（取决于源码逻辑）；第 3 步模块解析行为待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`NJS_PATH` 里如果有多个目录，是怎么分隔的？源码中是哪个函数负责切分？

**参考答案**：用 `:` 分隔（与 Unix `PATH` 风格一致）。在 `njs_options_parse` 中，读取 `NJS_PATH` 后用一个 `for ( ;; )` 循环配合 `njs_strchr(start, ':')` 逐段切分，每段调用 `njs_options_add_path(opts, start, len)` 追加进 `opts->paths` 数组。命令行的 `-p` 也可以多次使用，追加到同一个数组。

**练习 2**：`njs_create_engine` 为什么用「函数指针表」而不是 `if/else` 直接在调用处区分引擎？

**参考答案**：为了把「引擎差异」封装在初始化阶段。`njs_create_engine` 一次性填好 `engine->eval`/`destroy`/`output` 等函数指针后，上层（`njs_process_script`、`njs_interactive_shell` 等）只需要统一地写 `engine->eval(...)`，代码只有一份，不需要在每个调用点都写 `if (type == NJS) ... else ...`。这是 C 里实现「运行时多态」的常见手法，降低了双引擎维护的重复代码。

**练习 3**：为什么 `NJS_CAN_BLOCK` 没有对应的命令行选项？

**参考答案**：从源码看，`opts->can_block` 只在 QuickJS 初始化时被 `JS_SetCanBlock(rt, opts->can_block)` 使用，是 QuickJS 运行时协程调度的一个底层参数，对绝大多数 CLI 用户透明、几乎不需要在命令行上频繁调整，因此只暴露为环境变量。这也体现了「环境变量面向自动化/脚本场景，命令行选项面向交互场景」的分工。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个「读反汇编 + 对比双引擎」的小任务。

**任务背景**：你想给同事演示「njs CLI 的 `-d` 反汇编」和「`-n` 切换引擎导致语言能力差异」，需要先确认本机构建具备这两个能力。

**操作步骤**：

1. **确认能力清单**：运行 `./build/njs -h`，检查输出里是否同时包含 `-d` 行、`-n njs|QuickJS` 行。如果缺 `-n` 行，说明本机 `build/njs` 没有链接 QuickJS（参考 u1-l3 的 `--with-quickjs` / `--cc-opt`/`--ld-opt` 重新构建）；如果缺 `-o`/`-g` 行，说明没有用 `--debug-opcode=YES`/`--debug-generator=YES` 编译（本任务不需要它们，可忽略）。

2. **反汇编一段含函数的脚本（njs 引擎）**：
   ```bash
   ./build/njs -d -c 'var a=42; function f(v){return v+1}'
   ```
   对照本讲 4.2.3 节，确认 `-d` 把 `opts->disassemble` 置 1，并最终经 `njs_engine_njs_init` 流入 `njs_vm_create`。把输出里你认得的助记符（如 `STOP`、`RETURN`）圈出来；不必逐条读懂，那是 u3-l5 的内容。

3. **对比两套引擎的语言能力**。题目原本建议对比 `typeof Map`，但 njs 引擎也内置了 `Map`，两者很可能都返回 `"function"`，差异不明显。**更可靠的对照是 `BigInt`**——根据 CLAUDE.md 记载的语言基线（QuickJS 为 ES2023，njs 引擎为 ES5.1 strict 子集），BigInt 仅 QuickJS 支持：
   ```bash
   ./build/njs -n njs     -c 'console.log(typeof BigInt)'
   ./build/njs -n QuickJS -c 'console.log(typeof BigInt)'
   ```
   预期：njs 引擎下多为 `undefined`（不支持 BigInt），QuickJS 下为 `function`。具体输出**待本地验证**（不同 njs 版本对 ES6+ 子集的支持范围可能不同；如果你想完全遵照题目用 `typeof Map`，也可一并运行，记录两者是否真的不同）。

4. **复现兼容性校验**：把第 2 步的 `-d` 直接搬到 QuickJS 下，观察被拒：
   ```bash
   ./build/njs -n QuickJS -d -c 'var a=42'
   ```
   应输出 `option "-d" is not supported for quickjs` 并以非零码退出。回到源码 [njs_shell.c:752-755](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L752-L755) 比对报错文案是否一致。

**预期结果**：第 1 步得到本机能力清单；第 2、4 步行为由源码逻辑决定、可预测；第 3 步的精确输出待本地验证，但「两引擎对现代语法支持不同」这一结论应能成立。

> 如果你没有本机构建环境，第 2～4 步属于「源码阅读型验证」：仅凭本讲引用的源码片段，你也可以推断出 `-d` 在 QuickJS 下必然被拒、`-n` 必然改写 `opts->engine`，从而完成对调用链的理解。

## 6. 本讲小结

- CLI 的真正 C 入口是 [external/njs_shell.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c) 中的 `main`，它把命令行加工成 `njs_opts_t` 结构体后交给 `njs_main` 选择运行模式（交互式 / `-c` / 文件）。默认行为是「交互式」，由 `opts.interactive = 1` 与 `isatty(stdin)` 共同决定。
- 所有命令行选项在 `njs_options_parse` 的一个大 `switch` 里被逐字符匹配，写入 `njs_opts_t` 的对应字段；其中 `-c`/`-e`/`-j`/`-n`/`-p`/`-t` 需要跟参数，`-c` 取到脚本后用 `goto done` 跳出循环。
- `-n njs|QuickJS` 通过 `njs_options_parse_engine` 把字符串映射成 `engine` 枚举（大小写不敏感），QuickJS 分支受 `NJS_HAVE_QUICKJS` 编译开关控制；不指定时默认是 njs 引擎。
- `-a`/`-d`/`-g`/`-o`/`-s`/`-u` 是 njs 引擎专属选项，`-g`/`-o` 还依赖 `--debug-generator`/`--debug-opcode` 编译期开关；同时使用 `-n QuickJS` 与这些选项会在解析阶段被兼容性校验拒绝。
- 五个 `NJS_*` 环境变量（`NJS_ENGINE`/`NJS_PATH`/`NJS_EXIT_CODE`/`NJS_CAN_BLOCK`/`NJS_LOAD_AS_MODULE`）在主循环之前读入，作为默认值；命令行选项可覆盖它们。
- `njs_create_engine` 用「函数指针表」（`eval`/`destroy`/`output`/…）对两套引擎做统一抽象，上层代码只通过指针调用，不关心底层差异——这是 CLI 应对「双引擎」的核心设计。

## 7. 下一步学习建议

本讲止步于「创建并启动引擎」。建议接下来：

- **进入 VM 内部**：学 [u2-l1 njs_vm_t 生命周期](u2-l1-vm-lifecycle-api.md)。本讲里 `njs_engine_njs_init` 调用的 `njs_vm_create`、`njs_engine_njs_eval` 调用的 `njs_vm_compile`/`njs_vm_start`，正是 u2-l1 的主线。学完后你会看清「`-d`/`-s`/`-m` 这些开关在 VM 内部到底触发了什么」。
- **读懂反汇编输出**：学 [u3-l5 字节码格式与反汇编](u3-l5-bytecode-and-disassembler.md)，本讲综合实践里 `-d` 打印的那堆助记符就会变得有意义。
- **理解双引擎的全貌**：学 [u6-l1 QuickJS 引擎包装层](u6-l1-quickjs-wrapper.md)，看本讲提到的 `qjs_new_context` 如何在 QuickJS 之上做 njs 定制。
- 如果你想直接动手写跑在 NGINX 里的 JS，可以跳到第八单元（u8），但建议先过一遍 u2-l1，建立「VM 生命周期」的心智模型。
