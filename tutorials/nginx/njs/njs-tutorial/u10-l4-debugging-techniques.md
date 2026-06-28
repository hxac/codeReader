# 调试技巧：反汇编、opcode 跟踪与 NGINX 测试

## 1. 本讲目标

本讲是「测试、类型定义与工程实践」单元的收尾篇，把前面所有讲义里零散出现的调试手段汇总成一份可操作的「工具箱」。读完本讲，你应该能够：

- 用 CLI 的 `-d`（反汇编）、`-o`（逐指令跟踪）、`-g`（生成器跟踪）三把刀定位控制流与字节码层面的问题，并理解它们为何只在「用对应 `--debug-*` 选项编译」后才会出现；
- 读懂 `src/njs_disassembler.c` 的输出格式，把 `MOVE 0123 0133` 这类 hex 操作数拆解成「存储层级 + 变量类型 + 槽位号」；
- 在 NGINX 集成测试中用 `TEST_NGINX_LEAVE` / `TEST_NGINX_CATLOG` 保留并查看 `nginx.conf` 与 `error.log`；
- 牢记提交前要跑的验证清单（`-Werror`、`unit_test`、`test262`、双引擎 `prove`）。

本讲不引入新的引擎机制，而是教你「如何让引擎把它的内部状态打印给你看」。前置讲义 u3-l5（字节码格式与反汇编）、u4-l1（解释器主循环）、u4-l2（作用域寻址）建立的 hex 操作数解码能力是本讲的基础。

## 2. 前置知识

- **反汇编（disassembly）**：把字节码翻译回人类可读的助记符（如 `ADD`、`MOVE`、`RETURN`）。它不是把字节码还原成源码，而是还原成「指令级」的中间形态。
- **opcode 跟踪（opcode trace）**：在解释器**真正执行每一条指令时**把它打印出来，相当于给 VM 装一行行日志。与反汇编的区别是：反汇编只看「编译产物长什么样」，opcode 跟踪看「运行时实际走了哪条路」，因此能看到跳转、循环、函数调用带来的真实执行顺序。
- **生成器跟踪（generator trace）**：字节码生成器（u3-l4）遍历 AST 发射指令时打印自己的决策过程，用于排查「AST 没问题、但生成的字节码不对」这类 bug。
- **条件编译（conditional compilation）**：用 `#ifdef NJS_DEBUG_OPCODE` 这类宏，让 `-o`、`-g` 这两段代码只在「编译期开了对应开关」时才存在。这是它们「有时能用、有时报错 unknown option」的根因。
- **`Test::Nginx`**：Perl 的一个测试框架，njs 的 `nginx/t/*.t` 用它启动真实 nginx、打真实 HTTP 请求来验证 `r` / `s` 对象的行为（见 u10-l1）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [external/njs_shell.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c) | CLI 入口。`-d`/`-o`/`-g` 三个开关在这里解析，并把它们写进 `njs_vm_opt_t` 传给引擎。 |
| [src/njs_disassembler.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_disassembler.c) | 反汇编器实现。维护助记符表 `code_names[]`，`-d` 和 `-o` 最终都调用这里的 `njs_disassemble()`。 |
| [src/njs_vmcode.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h) | 定义 `njs_vmcode_debug` / `njs_vmcode_debug_opcode` 两个调试宏，被解释器主循环埋在每条指令的执行点上。 |
| [src/njs_vm.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vm.c) | `njs_vm_compile` 末尾，当 `disassemble` 打开时调用 `njs_disassembler(vm)` 一次性吐出全部字节码。 |
| [src/njs_generator.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_generator.c) | 生成器调试宏 `njs_debug_generator` 的定义，受 `NJS_DEBUG_GENERATOR` 守卫。 |
| [auto/options](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/options) / [auto/cc](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/cc) | `--debug-opcode` / `--debug-generator` 选项解析与对应 `#define` 的生成。 |
| [nginx/t/js.t](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/t/js.t) | NGINX HTTP 集成测试范例，用于演示 `prove` 怎么跑、产物留在哪。 |
| [docs/agent/engine-dev.md](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md) | 官方维护的构建/测试/验证清单，本讲很多约定直接来自它。 |

## 4. 核心概念与源码讲解

### 4.1 CLI 调试开关：-d / -o / -g 与构建期条件编译

#### 4.1.1 概念说明

njs 的 CLI 提供三个面向字节码的调试开关：

| 开关 | 名称 | 看到的层面 | 何时打印 |
|---|---|---|---|
| `-d` | 反汇编 | 编译产物（静态） | 编译完成、执行之前 |
| `-o` | opcode 跟踪 | 运行时执行流（动态） | 每条指令被取指执行时 |
| `-g` | 生成器跟踪 | AST → 字节码的发射过程 | 生成器遍历 AST 时 |

直觉上：`-d` 告诉你「程序编译成了什么」，`-o` 告诉你「程序实际怎么跑」，`-g` 告诉你「为什么编译成了这样」。三者各自只解决一类问题，互相补充。

一个关键认知：**`-o` 和 `-g` 默认不存在**。它们包裹在 `#ifdef NJS_DEBUG_OPCODE` / `#ifdef NJS_DEBUG_GENERATOR` 里，只有用 `./configure --debug-opcode=YES` 或 `--debug-generator=YES` 编译时才被编进二进制。没编进去时，帮助信息里不会出现这两行，传 `-o` 会被当成「未知选项」或直接忽略。

#### 4.1.2 核心流程

调试开关从命令行到引擎要经过三步：

1. **选项解析**：`njs_options_parse` 的 `switch` 里，`-d` 置 `opts->disassemble=1`，`-o` 置 `opts->opcode_debug=1`，`-g` 置 `opts->generator_debug=1`。
2. **写入 VM 选项**：`njs_engine_njs_init` 把这三个布尔值拷进 `njs_vm_opt_t`，随 `njs_vm_create` 进入引擎内部，最终存到 `vm->options.*`。
3. **运行期消费**：
   - `disassemble`：`njs_vm_compile` 末尾若为真，调用 `njs_disassembler(vm)` 打印全部字节码；
   - `opcode_debug`：解释器主循环里，每条指令的执行点都埋了 `njs_vmcode_debug_opcode()` 宏，为真时反汇编「当前这一条」指令；
   - `generator_debug`：生成器在发射指令前后埋了 `njs_debug_generator` 宏，为真时打印决策日志。

#### 4.1.3 源码精读

**第一步：选项解析。** 三个 `case` 分支，注意 `-o` 和 `-g` 各自被 `#ifdef` 包住：

[external/njs_shell.c:630-632](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L630-L632) —— `-d` 无条件可用，置 `disassemble=1`：

```c
case 'd':
    opts->disassemble = 1;
    break;
```

[external/njs_shell.c:647-651](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L647-L651) —— `-g` 只在 `NJS_DEBUG_GENERATOR` 定义时存在：

```c
#ifdef NJS_DEBUG_GENERATOR
case 'g':
    opts->generator_debug = 1;
    break;
#endif
```

[external/njs_shell.c:678-682](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L678-L682) —— `-o` 同理，受 `NJS_DEBUG_OPCODE` 守卫：

```c
#ifdef NJS_DEBUG_OPCODE
case 'o':
    opts->opcode_debug = 1;
    break;
#endif
```

帮助文本里这两行同样被 `#ifdef` 包住，所以「`-h` 里看不到 `-o`」就是「没编进去」的可靠信号：[external/njs_shell.c:527-529](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L527-L529) 与 [external/njs_shell.c:535-537](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L535-L537)。

**第二步：写入 VM 选项。** [external/njs_shell.c:1329-1340](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L1329-L1340) 把三个标志拷进 `vm_options`，注意 `-o`/`-g` 又被一层 `#ifdef` 守卫（防止选项结构与宏定义不同步）：

```c
vm_options.disassemble = opts->disassemble;
...
#ifdef NJS_DEBUG_GENERATOR
vm_options.generator_debug = opts->generator_debug;
#endif
#ifdef NJS_DEBUG_OPCODE
vm_options.opcode_debug = opts->opcode_debug;
#endif
```

**第三步：双引擎限制。** 这三个开关都是**内置 njs 引擎专属**。`njs_options_parse` 在解析完成后做了一次兼容性校验，选了 QuickJS 还传 `-d`/`-o`/`-g` 会被拒绝：[external/njs_shell.c:752-765](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L752-L765)。

```c
if (opts->disassemble) {
    njs_stderror("option \"-d\" is not supported for quickjs\n");
    return NJS_ERROR;
}
...
if (opts->opcode_debug) {
    njs_stderror("option \"-o\" is not supported for quickjs\n");
    return NJS_ERROR;
}
```

这是因为反汇编器、opcode 跟踪、生成器跟踪全部针对自研 njs 引擎的字节码格式，QuickJS 走的是上游的字节码，njs 没有给它写对应的调试钩子（见 u6-l1/u6-l4）。

**构建期开关如何变成 `#define`。** [auto/options:53-54](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/options#L53-L54) 解析这两个 `configure` 选项写入 shell 变量：

```sh
--debug-opcode=*)                NJS_DEBUG_OPCODE="$value"           ;;
--debug-generator=*)             NJS_DEBUG_GENERATOR="$value"        ;;
```

随后 [auto/cc:194-199](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/cc#L194-L199) 在变量为 `YES` 时生成对应的 `#define` 进 `njs_auto_config.h`：

```sh
if [ "$NJS_DEBUG_OPCODE" = "YES" ]; then
        njs_define=NJS_DEBUG_OPCODE . auto/define
fi
if [ "$NJS_DEBUG_GENERATOR" = "YES" ]; then
        njs_define=NJS_DEBUG_GENERATOR . auto/define
fi
```

这一条链路解释了本模块的核心结论：**「选项是否存在」是构建期决定的，「选项是否生效」是运行期决定的**——前者由 `#ifdef` 控制，后者由 `vm->options.*` 控制。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`-o` 在普通构建里不存在、在 `--debug-opcode` 构建里才出现」。

**操作步骤**：

1. 先用默认选项构建：`./configure && make -j$(nproc) njs`。
2. 跑 `./build/njs -h`，观察帮助信息里**没有** `-o` 和 `-g` 这两行。
3. 重新构建：`make clean && ./configure --debug-opcode=YES --debug-generator=YES && make -j$(nproc) njs`。
4. 再跑 `./build/njs -h`，这次能看到 `-o`（enable opcode debug）和 `-g`（enable generator debug）两行。

**需要观察的现象**：第 2 步看不到 `-o`/`-g`，第 4 步能看到——证明这两个开关是条件编译产物。

**预期结果**：帮助文本随构建选项变化。

> 说明：本环境（只读仓库）下步骤 1-4 需要 C 工具链。若无法实际构建，可改为「源码阅读型实践」：在 [external/njs_shell.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c) 中确认 `-o` 的 `case` 与帮助文本是否被同一个 `NJS_DEBUG_OPCODE` 宏包住，从而推出「未定义该宏时 `-o` 既不被识别也不被打印」——结论一致，标注「待本地验证」即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `-d` 不需要 `--debug-*` 选项编译就能用，而 `-o` 需要？

**参考答案**：`-d` 对应的 `case 'd'` 与 `vm_options.disassemble` 赋值都没有 `#ifdef` 包裹，始终编入二进制；反汇编器 `njs_disassembler.c` 也是无条件编译的。而 `-o`（以及 `-g`）的选项解析分支、帮助文本、VM 选项字段、解释器里的埋点宏全部受 `NJS_DEBUG_OPCODE`（`NJS_DEBUG_GENERATOR`）守卫，只有 `./configure --debug-opcode=YES` 才会生成该 `#define`，否则这些代码根本不存在。

**练习 2**：用户执行 `./build/njs -n QuickJS -d -c '1+1'` 会得到什么？为什么？

**参考答案**：报错 `option "-d" is not supported for quickjs` 并退出。因为 `-d`/`-o`/`-g` 全部针对自研 njs 引擎的字节码，[external/njs_shell.c:745-776](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_shell.c#L745-L776) 的兼容性校验会在选了 QuickJS 时拒绝这些开关。

### 4.2 反汇编器 njs_disassembler.c 的输出阅读

#### 4.2.1 概念说明

`-d` 和 `-o` 的输出都由同一个反汇编器产生。要会读这些输出，必须先理解它的格式。反汇编器做两件事：

1. **遍历编译产物**：`njs_disassembler(vm)` 遍历 `vm->codes`（一段段字节码，每段对应一个函数或全局代码），对每段调用 `njs_disassemble(start, end, -1, lines)`。
2. **逐条解码**：`njs_disassemble` 用一个 `while` 循环从 `start` 走到 `end`，每读到一个操作码就查 `code_names[]` 助记符表，按指令宽度（1/2/3 地址）打印操作数，再把指针 `p` 前进对应字节数。

#### 4.2.2 核心流程

输出的每一行长这样（来自 `njs_printf` 的格式串）：

```
行号 | 偏移  助记符            操作数...
  1 | 00000 MOVE     0123 0133
```

- **`行号`**（`%5uD`）：源码行号，由 `njs_lookup_line(lines, p - start)` 反查，方便你把指令对应回源码。
- **`偏移`**（`%05uz`）：本指令在该段字节码中的字节偏移，等于「跳转目标」的坐标系——跳转类指令的 `offset` 就是相对这里的偏移量。
- **助记符**：如 `MOVE`、`ADD`、`RETURN`，来自 `code_names[]`。
- **操作数**（`%04Xz`）：以 hex 打印的 `njs_index_t`，少数是跳转偏移。

少数「特殊指令」（跳转、try/catch、函数帧等）走各自的专属打印分支，格式略有不同（例如 `JUMP IF TRUE 0042z 0016` 第二个操作数是跳转偏移而非索引）。

#### 4.2.3 源码精读

**助记符表。** [src/njs_disassembler.c:18-166](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_disassembler.c#L18-L166) 用一张 `code_names[]` 静态表把操作码枚举映射到「助记符 + 指令宽度」，这是反汇编的核心数据：

```c
static njs_code_name_t  code_names[] = {
    { NJS_VMCODE_PUT_ARG, sizeof(njs_vmcode_1addr_t),
          njs_str("PUT ARG         ") },
    ...
    { NJS_VMCODE_ADDITION, sizeof(njs_vmcode_3addr_t),
          njs_str("ADD             ") },
    ...
    { NJS_VMCODE_MOVE, sizeof(njs_vmcode_move_t),
          njs_str("MOVE            ") },
    ...
};
```

每条记录三个字段：操作码、该指令占多少字节（决定按 1/2/3 地址格式打印、以及指针前进多少）、助记符字符串。这张表与 u3-l5 讲的 `NJS_VMCODE_*` 操作码枚举一一对应。

**遍历入口。** [src/njs_disassembler.c:169-186](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_disassembler.c#L169-L186) 的 `njs_disassembler(vm)`：先打印段名（`%V:%V` = `文件:函数名`，如 `shell:main`、`shell:f`），再对每段调 `njs_disassemble`：

```c
while (n != 0) {
    njs_printf("%V:%V\n", &code->file, &code->name);
    njs_disassemble(code->start, code->end, -1, code->lines);
    code++;
    n--;
}
```

**通用 1/2/3 地址打印。** 跳过一堆「特殊指令」的 `if` 分支后，[src/njs_disassembler.c:531-569](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_disassembler.c#L531-L569) 用 `code_name->size` 判定宽度，分别按 3/2/1 地址格式打印：

```c
if (code_name->size == sizeof(njs_vmcode_3addr_t)) {
    code3 = (njs_vmcode_3addr_t *) p;
    njs_printf("%5uD | %05uz %*s  %04Xz %04Xz %04Xz\n",
               line, p - start, name->length, name->start,
               (size_t) code3->dst, (size_t) code3->src1, (size_t) code3->src2);
} else if (code_name->size == sizeof(njs_vmcode_2addr_t)) {
    ...   // 打印 dst src
} else if (code_name->size == sizeof(njs_vmcode_1addr_t)) {
    ...   // 只打印一个 index
}
```

如果操作码既不匹配任何特殊分支、也不在 `code_names[]` 里，就落到 [src/njs_disassembler.c:571-574](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_disassembler.c#L571-L574) 的 `UNKNOWN` 兜底——看到 `UNKNOWN` 通常意味着字节码被破坏或反汇编器与操作码表不同步。

**opcode 跟踪如何复用它。** `-o` 不走 `njs_disassembler`，而是直接调 `njs_disassemble(pc, NULL, 1, NULL)`——第三个参数 `count=1` 表示「只反汇编一条」。这由埋在解释器里的宏完成：[src/njs_vmcode.h:426-429](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h#L426-L429)：

```c
#define njs_vmcode_debug_opcode()                                             \
    if (vm->options.opcode_debug) {                                           \
        njs_disassemble(pc, NULL, 1, NULL);                                   \
    }
```

注意 `count=1` 时循环条件 `(count-- > 0)` 恰好执行一次就退出（见 `njs_disassemble` 的 `while` 判定 [src/njs_disassembler.c:229](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_disassembler.c#L229)），所以 `-o` 是「执行一条、打印一条」。

而函数边界（进入/退出函数）由另一个宏打印：[src/njs_vmcode.h:415-424](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.h#L415-L424) 的 `njs_vmcode_debug` 会打印 `ENTER <段名>` / `EXIT STOP` / `EXIT RETURN` / `EXIT AWAIT` / `RESUME` 等标记，这正是 [docs/agent/engine-dev.md:289-294](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md#L289-L294) 所说的「`ENTER`/`EXIT` for function boundaries」。这些埋点散布在 `njs_vmcode.c` 主循环的各处，例如入口处 [src/njs_vmcode.c:115](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_vmcode.c#L115) 的 `njs_vmcode_debug(vm, pc, "ENTER")`。

#### 4.2.4 怎么读 hex 操作数：一个完整例子

官方在 [docs/agent/engine-dev.md:230-247](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md#L230-L247) 给的标准示例：

```
$ ./build/njs -d
>> var a = 42; function f(v) { return v + 1 }

shell:main
    1 | 00000 MOVE     0123 0133
    1 | 00024 STOP     0033

shell:f
    1 | 00000 ADD      0203 0103 0233
    1 | 00032 RETURN   0203
```

结合 u4-l2 讲的 index 位编码 `槽位号(高24位) : 存储层级(中4位) : 变量类型(低4位)`，把 `0133`、`0123`、`0203`、`0103`、`0233` 逐位拆开：

```text
索引(hex)   变量类型(低4位)  存储层级(次4位)  槽位号(高24位)   含义
0123        3 = VAR          2 = GLOBAL       1               全局变量 a
0133        3 = VAR          3 = STATIC       1               静态字面量槽1 (常量 42)
0203        3 = VAR          0 = LOCAL        2               局部槽2 (函数返回值临时位)
0103        3 = VAR          0 = LOCAL        1               局部槽1 (参数 v)
0233        3 = VAR          3 = STATIC       2               静态字面量槽2 (常量 1)
```

于是 `MOVE 0123 0133` 的含义立刻清楚：把「静态常量 42」（`0133`）搬到「全局变量 a」（`0123`），即 `var a = 42`。`ADD 0203 0103 0233` 是 `local[2] = local[1] + static[2]`，即 `临时位 = v + 1`；`RETURN 0203` 返回那个临时位。

解码三步法（与 u3-l5、u4-l2 一致）：

```text
变量类型 = index & 0xF            // CONST/LET/CATCH/VAR/FUNCTION
存储层级 = (index >> 4) & 0xF     // LOCAL/CLOSURE/GLOBAL/STATIC
槽位号   = index >> 8            // vm->levels[层级][槽位号]
```

#### 4.2.5 代码实践

**实践目标**：用 `-d` 反汇编一段含循环与函数调用的代码，并逐条解读。

**操作步骤**：

1. 准备脚本 `dbg.js`：

   ```js
   function sum(n) {
       var s = 0;
       for (var i = 0; i < n; i++) { s += i; }
       return s;
   }
   sum(3);
   ```

2. 运行 `./build/njs -d dbg.js`（或 `./build/njs -d` 后在交互式 REPL 粘贴）。

3. 在 `shell:sum` 段里找出循环对应的指令：你会看到 `JUMP IF FALSE`（或 `IF_FALSE_JUMP`）+ `JUMP` 这对「条件跳转 + 回跳」结构，以及 `POST INC`、`ADD` 等。

**需要观察的现象**：循环体被编译成一个「条件跳转跳出 + 无条件跳转回头部」的回路；`<` 比较对应 `LESS` 指令；`i++` 对应 `POST INC`。

**预期结果**：能指出哪条指令是循环退出判定、哪条是回跳、`s` 与 `i` 各自的 hex 索引属于哪一级存储。若无法运行，标注「待本地验证」，但可对照 [src/njs_disassembler.c:245-278](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_disassembler.c#L245-L278) 的 `JUMP IF TRUE` / `JUMP IF FALSE` / `JUMP` 打印分支确认它们的助记符与操作数含义。

#### 4.2.6 小练习与答案

**练习 1**：`-d` 输出里出现 `UNKNOWN` 意味着什么？

**参考答案**：当前操作码既没命中任何「特殊指令」分支（跳转、try/catch、函数帧等），也不在 `code_names[]` 助记符表里。反汇编器只能按最小宽度（`sizeof(njs_vmcode_t)`）前进并打印 `UNKNOWN`（[src/njs_disassembler.c:571-574](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/njs_disassembler.c#L571-L574)）。正常用户代码不应出现，它的出现通常意味着字节码损坏、或新增了操作码却忘了同步登记进 `code_names[]`。

**练习 2**：为什么 `-o`（opcode 跟踪）和 `-d`（反汇编）看到的指令顺序可能不同？

**参考答案**：`-d` 是**静态**打印编译产物的线性布局，按字节偏移从小到大；`-o` 是**动态**打印真实执行流，遇到跳转、循环、函数调用时会按实际走过的顺序打印，同一段字节码可能被多次执行（如循环体），从而在 `-o` 输出里反复出现。换言之，`-d` 看「代码长什么样」，`-o` 看「运行时实际怎么走」。

### 4.3 NGINX 测试日志：保留产物与定位 error.log

#### 4.3.1 概念说明

CLI 调试解决「JS 语义对不对」，NGINX 集成测试解决「`r`/`s` 对象、`js_content`/`js_filter` 等指令在真实 nginx 里行为对不对」。后者用 `prove` 跑 `nginx/t/*.t`（基于 Perl 的 `Test::Nginx`），每个 `.t` 文件会启动一个临时 nginx、打 HTTP 请求、用 `like()` / `is()` 断言响应（见 [nginx/t/js.t:251-302](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/t/js.t#L251-L302)）。

测试失败时，最常需要的两样东西是：**生成的 `nginx.conf`**（确认指令是否如预期被解析）和 **`error.log`**（看 nginx/njs 报了什么错）。`Test::Nginx` 默认跑完就清理临时目录，所以要用环境变量让产物留下来。

#### 4.3.2 核心流程

关键环境变量（来自 [docs/agent/engine-dev.md:104-119](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md#L104-L119)）：

| 变量 | 作用 |
|---|---|
| `TEST_NGINX_BINARY` | 指定被测的 nginx 二进制路径（必需）。 |
| `TEST_NGINX_LEAVE=1` | 跑完**不清理**临时目录，保留 `nginx.conf` / `error.log` / 进程产物。 |
| `TEST_NGINX_CATLOG=1` | 跑完自动把 `error.log` dump 到 stdout，省去手动找文件。 |
| `TEST_NGINX_VERBOSE=1` | 让 harness 输出更详细。 |
| `TEST_NGINX_GLOBALS_HTTP='js_engine qjs;'` | 注入 http 块全局配置，用于切到 QuickJS 跑同一批测试。 |
| `TEST_NGINX_GLOBALS_STREAM='js_engine qjs;'` | 同上，stream 块。 |

推荐每个 run 用独立 `TMPDIR=$(mktemp -d)` 隔离产物，避免并发跑互相覆盖或触发危险的 `rm -fr /tmp/nginx-test*`。

#### 4.3.3 源码精读

**测试入口与配置模板。** [nginx/t/js.t:27-52](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/t/js.t#L27-L52) 展示了一个典型 `.t` 的开头：用 `Test::Nginx->new()` 创建实例，`write_file_expand('nginx.conf', <<'EOF')` 内联写出配置模板，模板里的 `%%TEST_GLOBALS%%` / `%%TEST_GLOBALS_HTTP%%` 占位符会被 harness 替换成 `TEST_NGINX_GLOBALS*` 环境变量的内容——这正是「同一个 `.t`、靠环境变量切引擎」的机制：

```perl
my $t = Test::Nginx->new()->has(qw/http rewrite/)
    ->write_file_expand('nginx.conf', <<'EOF');
%%TEST_GLOBALS%%
...
http {
    %%TEST_GLOBALS_HTTP%%
    js_set $test_method   test.method;
    ...
    js_import test.js;
```

**断言形态。** [nginx/t/js.t:251-262](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/t/js.t#L251-L262) 用 `like(http_get('/path'), qr/正则/, '说明')` 断言响应匹配。失败时 harness 会打印期望与实际，但要看 nginx 侧的报错（例如 `js_content` 抛了未捕获异常），就得看 `error.log`：

```perl
like(http_get('/method'), qr/method=GET/, 'r.method');
like(http_get('/version'), qr/version=1.0/, 'r.httpVersion');
```

**官方对留产物的说明。** [docs/agent/engine-dev.md:296-301](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md#L296-L301) 明确：`TEST_NGINX_LEAVE=1` 时每个测试会留下 `$TMPDIR/nginx-test-<random>/`，内含生成的 `nginx.conf`、`error.log` 及其它产物；`TEST_NGINX_CATLOG=1` 则自动把日志打到 stdout。

#### 4.3.4 代码实践

**实践目标**：跑一个 `nginx/t/*.t`，定位并阅读它生成的 `nginx.conf` 与 `error.log`。

**操作步骤**：

1. 准备一个带 njs 模块的 nginx 二进制（见 u10-l3 / engine-dev.md 的 `--add-module` 构建）。
2. 跑测试并留产物：

   ```bash
   TMPDIR=$(mktemp -d) \
   TEST_NGINX_BINARY=<你的nginx二进制> \
   TEST_NGINX_LEAVE=1 \
   prove -I <TESTS_LIB> nginx/t/js.t
   ```

3. 进到 `$TMPDIR` 下找 `nginx-test-*` 目录，查看里面的 `nginx.conf`（确认 `js_import`/`js_content` 解析正确）与 `error.log`（看有无 njs 异常、resolver 报错等）。
4. 再加 `TEST_NGINX_CATLOG=1` 重跑，对比这次 `error.log` 是否直接打到 stdout。

**需要观察的现象**：第 3 步能在临时目录里看到完整的 `nginx.conf`（含 `%%TEST_GLOBALS%%` 替换后的真实内容）和 `error.log`；第 4 步日志直接出现在终端。

**预期结果**：能独立找到 `nginx.conf` 与 `error.log` 并解释其中一条日志。若本机无 nginx 二进制，标注「待本地验证」，可改为阅读 [nginx/t/js.t](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/nginx/t/js.t) 的配置模板，指明 `%%TEST_GLOBALS_HTTP%%` 占位符会被 `TEST_NGINX_GLOBALS_HTTP` 替换。

#### 4.3.5 小练习与答案

**练习**：为什么官方强调「用 `TMPDIR=$(mktemp -d)` 而非直接清 `/tmp/nginx-test*`」？

**参考答案**：`Test::Nginx` 默认把每个测试的临时目录放在 `TMPDIR`（默认 `/tmp`）下，名字形如 `nginx-test-<random>`。并发跑多个 `prove` 时，若都去 `rm -fr /tmp/nginx-test*` 会互相误删、甚至误伤系统其它文件。给每个 run 一个独立 `mktemp -d` 目录，既隔离产物、又避免危险的全局清理（[docs/agent/engine-dev.md:116-117](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md#L116-L117)）。

### 4.4 提交前验证清单

#### 4.4.1 概念说明

njs 是双引擎 + 多平台 + 严格代码风格的项目，改一处可能波及两份实现、两套测试。官方在 [docs/agent/engine-dev.md:121-135](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md#L121-L135) 给了一份提交前必跑的验证清单。它不是「建议」，而是合并前的事实门槛。

#### 4.4.2 核心流程（清单本体）

按改动范围，逐项对照：

1. **编译零警告**：`./configure && make -j$(nproc)` 必须在 `-Werror` 下通过（[docs/agent/engine-dev.md:125](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md#L125)）。
2. **C 单元测试**：`make unit_test` 和 `make lib_test` 通过（[docs/agent/engine-dev.md:126](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md#L126)）。
3. **动了 `src/` 要跑 test262**：`make test262`（[docs/agent/engine-dev.md:127-128](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md#L127-L128)）。
4. **动了 `nginx/` 要跑双引擎 prove**：`prove -I <TESTS_LIB> nginx/t/`，一次默认引擎、一次 `TEST_NGINX_GLOBALS_HTTP='js_engine qjs;'`（[docs/agent/engine-dev.md:129-130](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md#L129-L130)）。
5. **新增源文件要登记清单**：njs 内核进 `auto/sources`、njs 扩展进 `auto/modules`、QuickJS 扩展进 `auto/qjs_modules`（[docs/agent/engine-dev.md:131-133](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md#L131-L133)）。
6. **双引擎镜像**：改了 `njs_*.c` 的行为，要同步改对应的 `qjs_*.c`，反之亦然（[docs/agent/engine-dev.md:134-135](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md#L134-L135)）。

#### 4.4.3 源码精读

清单的每一项都能在仓库里找到落点：

- **`-Werror`**：默认开启，[docs/agent/engine-dev.md:146](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md#L146) 明确 `-Werror` is on by default — fix all warnings。
- **测试目标**：[docs/agent/engine-dev.md:88-93](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md#L88-L93) 列出 `make unit_test` / `lib_test` / `test262` / `test` 各自覆盖范围（u10-l1 有详述）。
- **双引擎 prove 的环境变量**：[docs/agent/engine-dev.md:113-114](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md#L113-L114) 给出 `TEST_NGINX_GLOBALS_HTTP='js_engine qjs;'` 与 `_STREAM` 的用法——这正是「同一批 `.t` 跑两遍、分别用两个引擎」的标准做法。
- **配置选项全表**：[docs/agent/engine-dev.md:71-83](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md#L71-L83) 汇总 `--debug*`、`--address-sanitizer`、`--no-*` 等选项，便于在「需要更强调试构建」时快速查阅（与 u10-l3 互补）。

#### 4.4.4 代码实践

**实践目标**：模拟一次「改了 `src/`」的提交前自检，确认清单第 1-3 项。

**操作步骤**：

1. （可选）在 `src/` 某个非关键函数里临时加一行 `printf`（仅本地实验，事后还原）。
2. 重新构建并检查零警告：`make clean && ./configure && make -j$(nproc) 2>&1 | grep -i warning`，应为空。
3. 跑 `make unit_test` 和 `make lib_test`，确认全绿。
4. 因为动了 `src/`，再跑 `make test262`。

**需要观察的现象**：第 2 步无 warning（`-Werror` 下有 warning 会直接编译失败）；第 3、4 步测试全部通过。

**预期结果**：四项均通过即满足「动了 `src/`」的最低提交门槛。若无法构建，标注「待本地验证」，可改为阅读清单并口述「动了 `external/njs_fs_module.c` 一个方法时，清单第 6 项要求同步改 `external/qjs_fs_module.c`、第 4 项要求 nginx prove 跑双引擎」（参考 u6-l2 双实现铁律）。

> 注意：实践结束后务必还原对 `src/` 的临时改动，本任务禁止修改源码。

#### 4.4.5 小练习与答案

**练习**：你只改了 `external/qjs_crypto_module.c` 里的一个分支，提交前清单里哪几项必须跑？哪一项「双引擎镜像」可以略过？

**参考答案**：必跑——编译零警告（第 1 项）、`unit_test`/`lib_test`（第 2 项）；若该改动影响 nginx 暴露的 crypto 行为，还要跑 `prove nginx/t/` 的双引擎版本（第 4 项）。第 3 项 `test262` 针对的是 `src/` 内核语言特性，`external/` 扩展改动通常不强制。第 6 项「双引擎镜像」要求反向同步 `njs_crypto_module.c`——但题设是「只改了 qjs 版」，说明你应当确认 njs 侧是否也有同样的 bug 需要修；若 njs 侧本来就正确，则可略过，否则必须双改。

## 5. 综合实践

把本讲三块内容（CLI 调试、反汇编阅读、NGINX 测试）串成一个端到端的调试任务。

**场景**：你怀疑某段 njs 代码「函数没被调用」，想用字节码层面证据确认。

1. **构建带 opcode 跟踪的 CLI**：

   ```bash
   make clean
   ./configure --debug-opcode=YES
   make -j$(nproc) njs
   ```

2. **准备脚本** `dbg.js`：

   ```js
   function greet(name) {
       return 'hi ' + name;
   }
   var r = greet('njs');
   ```

3. **先用 `-d` 看静态布局**：`./build/njs -d dbg.js`。在 `shell:greet` 段定位 `PROP SET` / `ADD` / `RETURN` 等指令，把 `greet` 的参数与返回值临时位的 hex 索引按「`&0xF` / `(>>4)&0xF` / `>>8`」三步法解码，确认它们属于 LOCAL 层。

4. **再用 `-o` 看动态执行**：`./build/njs -o dbg.js`。观察输出里是否出现 `ENTER shell:greet` 与随后的 `EXIT RETURN`——这两行是函数被真实调用并返回的直接证据。同时注意 `shell:main` 段的 `FUNCTION FRAME` + `PUT ARG` + `FUNCTION CALL` 序列，对应「建立调用帧 → 压实参 → 发起调用」。

5. **（延伸）切到 NGINX 侧**：把同样的脚本包进一个 `js_content` handler，用 `TEST_NGINX_LEAVE=1` 跑一个自写的 `nginx/t/*.t`，在保留的 `error.log` 里确认请求确实进入了 handler（若 handler 里 `r.log('hit')`，日志应出现）。

**预期产出**：你能用 `-d` + `-o` 两份输出证明「`greet` 被调用了」并指出调用帧建立的关键指令；能用 `TEST_NGINX_LEAVE` 找到 `error.log` 佐证 NGINX 侧也进入了 handler。任何无法在本机运行的步骤标注「待本地验证」。

## 6. 本讲小结

- njs CLI 有三把字节码调试刀：`-d` 反汇编（静态产物）、`-o` opcode 跟踪（动态执行流）、`-g` 生成器跟踪（AST→字节码决策）。
- `-o` 与 `-g` 是**条件编译**产物，只有 `./configure --debug-opcode=YES` / `--debug-generator=YES` 才会编入；默认构建里既不在 `-h` 出现、也不被识别。
- 三个开关都是**内置 njs 引擎专属**，选 `-n QuickJS` 时会被 `njs_options_parse` 的兼容性校验拒绝。
- 反汇编器 `src/njs_disassembler.c` 靠 `code_names[]` 助记符表把操作码翻译成人话；`-o` 复用它但传 `count=1` 实现「执行一条打一条」，函数边界由 `njs_vmcode_debug` 宏打 `ENTER`/`EXIT`。
- hex 操作数按 `变量类型(低4) : 存储层级(次4) : 槽位号(高24)` 解码；`MOVE 0123 0133` = 把静态常量搬到全局变量。
- NGINX 测试用 `TEST_NGINX_LEAVE=1` 留 `nginx.conf`/`error.log`、`TEST_NGINX_CATLOG=1` 自动 dump 日志、`TEST_NGINX_GLOBALS_HTTP='js_engine qjs;'` 切引擎；用独立 `TMPDIR` 隔离产物。
- 提交前验证清单：编译零警告（`-Werror`）、`unit_test`/`lib_test`、动 `src/` 跑 `test262`、动 `nginx/` 跑双引擎 `prove`、新文件登记 `auto/*` 清单、`njs_*.c`/`qjs_*.c` 双改。

## 7. 下一步学习建议

本讲是整套手册的最后一篇，建议按以下方向巩固与拓展：

- **回看内核讲义验证调试输出**：用 `-o` 跑一段含 `try/catch`、`Promise`、`async/await` 的代码，对照 u4-l4（异常）、u4-l5（异步）在输出里找到 `TRY START`/`CATCH`/`AWAIT`/`RESUME` 等指令，把抽象机制落到具体字节码。
- **结合 ASan 构建**：u10-l3 讲的 `--address-sanitizer=YES` 与本讲的 opcode 跟踪互补——ASan 抓「内存越界/释放后使用」，`-o` 抓「控制流不符」，组合使用能定位大多数引擎 bug。
- **给 njs 贡献一个修复**：按本讲的验证清单，挑一个 `auto/sources`/`nginx/t` 里的小问题（如补一条 test262 回归用例），完整走一遍「改 → `-Werror` → unit_test → 双引擎 prove」流程，作为整套手册的毕业实践。
- **持续阅读**：把 [docs/agent/engine-dev.md](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/engine-dev.md) 与 [docs/agent/js-dev.md](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev.md) 当作日常工作的速查表，它们是官方维护、与本仓库代码同步演进的「活文档」。
