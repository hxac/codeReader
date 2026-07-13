# 配置文件解析器 ngx_conf_parse

## 1. 本讲目标

nginx 是一个「能力由编译进来的模块决定、行为由配置文件决定」的程序（这一点我们在 u1-l1 已经建立）。本讲要回答的核心问题是：**一段纯文本的 `nginx.conf`，是怎么变成内存里可以驱动整个服务器的配置结构的？**

学完本讲，你应当能够：

1. 说清楚 nginx 配置的「词法结构」——什么算一个 token，`;` `{` `}` `#` 各自的含义，引号与变量如何处理。
2. 画出一次指令从「文件里的字符」到「`cmd->set` 回调被调用」的完整调用链，并解释 `ngx_conf_parse` / `ngx_conf_read_token` / `ngx_conf_handler` 三者的分工。
3. 读懂 `ngx_command_t` 这个「指令描述符」结构体，理解 `type` 字段里的两大类标志（参数个数 + 作用域），理解 `NGX_HTTP_MAIN_CONF / SRV_CONF / LOC_CONF` 等作用域标志如何控制「某条指令只能写在某个块里」。
4. 掌握 `ngx_conf_set_*_slot` 这一族「通用解析函数」如何用 `offsetof` 反射式地把文本值直接写进模块的 conf 结构体。

本讲是后续 u3-l2（cycle 生命周期）、u3-l3（模块系统）、u3-l4（指令与地址解析）以及整个 HTTP 模块体系（第六单元）的地基。不理解配置解析，就无法理解 `http {}` 里的 `server {}`、`location {}` 是如何一层层嵌套并最终生成运行时配置的。

---

## 2. 前置知识

在进入源码前，先用通俗语言铺几个概念。

### 2.1 什么是「配置解析器」

你可以把 nginx 配置文件看成一种非常简单的语言，它只有四种基本元素：

| 元素 | 含义 | 例子 |
|------|------|------|
| 指令名（directive name） | 一个单词 | `worker_processes` |
| 参数（argument） | 跟在指令名后面的值 | `1`、`on`、`1024` |
| `;` | 一条简单指令的结束 | `worker_processes 1;` |
| `{ }` | 一条「块指令」的开始与结束，块里可以再嵌套指令 | `events { ... }` |

所以解析器的任务很朴素：**把字符流切成一个个 token，识别出「这是一条以 `;` 结尾的简单指令」还是「这是一条以 `{` 开头的块指令」，再把指令名交给对应的处理函数。** 不需要复杂的语法树，nginx 用的是「边解析边执行」的方式。

### 2.2 三层概念：词法、分发、执行

- **词法（lexing）**：逐字符扫描，把字符聚合成 token。对应 `ngx_conf_read_token`。
- **分发（dispatch）**：拿着指令名，在所有模块的指令表里查「这是谁的指令、它接受几个参数、它属于哪个作用域」。对应 `ngx_conf_handler`。
- **执行（set 回调）**：找到指令后，调用它注册的 `set` 函数，把参数真正写进配置结构体。对应每个模块自己的回调，或通用的 `ngx_conf_set_*_slot`。

### 2.3 与前序讲义的衔接

- u1-l4 讲过 `main()` 启动流程，其中 `ngx_init_cycle` 会调用本讲的 `ngx_conf_parse` 来读取主配置文件——这是本讲的入口。
- u2-l2 讲过 `ngx_str_t`、`ngx_atoi`、`ngx_parse_size`、`ngx_parse_time`。本讲里的通用 slot 函数（如 `ngx_conf_set_num_slot`、`ngx_conf_set_size_slot`）内部正是调用它们来把字符串转成数值。
- u2-l3 讲过 `ngx_array_t`。本讲的 `cf->args` 就是一个 `ngx_array_t`，用来存放「一条指令的名字 + 所有参数」。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/core/ngx_conf_file.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c) | 解析器全部实现：主驱动 `ngx_conf_parse`、词法器 `ngx_conf_read_token`、分发器 `ngx_conf_handler`、所有通用 slot 函数、`include` 处理。本讲的主战场。 |
| [src/core/ngx_conf_file.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.h) | `ngx_command_t`（指令描述符）、`ngx_conf_t`（解析上下文）、所有类型标志宏（`NGX_CONF_TAKE1` 等）、所有「未设置」哨兵值的定义。 |
| [src/http/ngx_http_config.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_config.h) | HTTP 层的「作用域标志」定义：`NGX_HTTP_MAIN_CONF / SRV_CONF / LOC_CONF` 等，以及 HTTP 三层配置上下文 `ngx_http_conf_ctx_t`。 |
| [conf/nginx.conf](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/conf/nginx.conf) | 默认配置文件，本讲实践任务的分析对象。 |
| [src/core/ngx_cycle.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c) | `ngx_init_cycle` 里对 `ngx_conf_parse` 的最初调用（入口）。 |
| [src/event/ngx_event.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c) | `ngx_events_block`——`events {}` 块的 set 回调，展示「块指令如何递归调用 `ngx_conf_parse`」。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 主驱动 `ngx_conf_parse` 与词法器 `ngx_conf_read_token`**：字符怎么变成 token，token 怎么组成指令。
- **4.2 指令分发 `ngx_conf_handler`**：拿到指令名后，如何在模块表里查到它、做哪些合法性校验。
- **4.3 `ngx_command_t` 与 conf slot 类型**：指令描述符长什么样、作用域标志如何工作、通用 slot 函数如何用 offset 把值写进结构体。

### 4.1 主驱动 ngx_conf_parse 与词法器 ngx_conf_read_token

#### 4.1.1 概念说明

`ngx_conf_parse` 是整个配置解析的「主循环」。它的工作模式非常简单：**循环调用词法器 `ngx_conf_read_token` 取下一条指令，根据词法器返回的状态码决定是「执行这条指令」「进入子块」「退出当前块」还是「文件结束」。**

注意它是一个**可重入**的函数：

- 第一次调用（最外层）传入文件名，打开主配置文件，从头解析；
- 当遇到一个块指令（比如 `events {`），该块的 set 回调会**再次调用** `ngx_conf_parse(cf, NULL)`（第二个参数为 `NULL` 表示「不重新打开文件，就在当前已打开的文件流上继续读，直到读到匹配的 `}`」）。

这就是 nginx 实现「块嵌套」的方式——**不是用一个显式的递归下降语法树，而是靠「同一个函数 + 同一个文件缓冲区 + 栈式保存/恢复解析上下文」**。每一层 `ngx_conf_parse` 调用负责消费从 `{` 到匹配 `}` 之间的内容。

`ngx_conf_read_token` 则是词法器，逐字符扫描，把一条指令的「名字 + 参数」塞进 `cf->args` 数组，并返回一个状态码告诉主循环这条指令是怎么结尾的。

#### 4.1.2 核心流程

先看词法器的五种返回值，它们是主循环做判断的唯一依据：

| 返回值 | 含义 |
|--------|------|
| `NGX_ERROR` | 词法错误（如未闭合的引号、意外的字符） |
| `NGX_OK` | 读到一条以 `;` 结尾的简单指令，参数已放进 `cf->args` |
| `NGX_CONF_BLOCK_START` | 读到一条以 `{` 开头的块指令，名字已放进 `cf->args` |
| `NGX_CONF_BLOCK_DONE` | 读到 `}`，当前块结束 |
| `NGX_CONF_FILE_DONE` | 读到文件末尾 |

主循环 `ngx_conf_parse` 的伪代码：

```
ngx_conf_parse(cf, filename):
    if filename != NULL:
        打开文件，分配 4KB 读缓冲，type = parse_file
    else if 当前正挂在某个打开的文件上:
        type = parse_block        # 这是块内的递归调用
    else:
        type = parse_param        # 这是命令行 -g 选项的解析

    for ( ;; ):
        rc = ngx_conf_read_token(cf)

        if rc == NGX_ERROR:                  出错返回
        if rc == NGX_CONF_BLOCK_DONE:        # 读到 }
            若 type != parse_block: 报 "unexpected }"
            否则: 返回（本层块解析完成，回到上一层）
        if rc == NGX_CONF_FILE_DONE:         # 文件尾
            若 type == parse_block: 报 "缺 }"
            否则: 正常返回
        if rc == NGX_CONF_BLOCK_START:       # 读到 {
            若 type == parse_param: 报 "-g 不支持块指令"

        # 到这里 rc 是 NGX_OK 或 NGX_CONF_BLOCK_START
        if cf->handler != NULL:              # 专用 handler（如 types {}）
            调用 cf->handler
        else:
            ngx_conf_handler(cf, rc)         # 通用分发
```

词法器 `ngx_conf_read_token` 内部是一个字符状态机，维护一组布尔标志（含义见名）：

```
found         # 当前 token 已结束，准备入库
need_space    # 一个 token 刚读完，期待后续是空白/;/{
last_space    # 上一字符是空白（即正处在「词与词之间」）
sharp_comment # 进入 # 注释，本行剩余字符忽略
variable      # 遇到了 $（变量语法，${...}）
quoted        # 上一字符是反斜杠 \（转义）
s_quoted      # 在单引号内
d_quoted      # 在双引号内
```

核心逻辑：每读一个字符 `ch`，按「是否处在空白间隙（`last_space`）」分两大支：

- **空白间隙支**：跳过空白；遇到 `;`/`{` 表示上一条指令结束（若无参数则报错）；遇到 `}` 表示块结束；`#` 进注释；`\` 转义；`"`/`'` 进引号模式；`$` 进变量模式；否则标记「进入一个新 token」。
- **token 中间支**：在引号内则等闭合引号；遇到空白/`;`/`{` 表示当前 token 结束，调用 `ngx_array_push(cf->args)` 把这个 token 存进参数数组，并对转义符做展开（`\t` `\r` `\n` 等）。

一个关键设计：**读缓冲只有 4KB（`NGX_CONF_BUFFER`），不够时会把「半个 token」前移到缓冲区开头再续读文件**，所以单个 token 可以比缓冲区长（但有上限保护）。

#### 4.1.3 源码精读

先看主驱动 `ngx_conf_parse` 的签名与「决定本次调用类型」的三分支逻辑：

[ngx_conf_parse 三分支：打开文件 / 块内递归 / 命令行参数 — src/core/ngx_conf_file.c:158-239](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L158-L239)

其中 `filename != NULL` 分支负责打开文件、分配 4KB 缓冲（`buf.start = ngx_alloc(NGX_CONF_BUFFER, ...)`）、记录行号 `cf->conf_file->line = 1`，并置 `type = parse_file`。第二分支 `cf->conf_file->file.fd != NGX_INVALID_FILE` 表示「当前还挂在某个已打开文件上」，置 `type = parse_block`——这就是块指令递归调用时走的路径。第三分支 `type = parse_param` 用于命令行 `-g` 选项。

接下来是主循环本体，集中体现「五种返回码如何驱动状态机」：

[主循环 for(;;)：读 token、按返回码分发 — src/core/ngx_conf_file.c:242-324](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L242-L324)

几个要点：

- `rc == NGX_CONF_BLOCK_DONE`（读到 `}`）且 `type == parse_block` 时 `goto done`，**直接返回**，把控制权交回上一层的 `ngx_conf_parse`——这是「块结束 = 本层函数返回」的实现。
- `rc == NGX_CONF_BLOCK_START`（读到 `{`）且 `type == parse_param` 时报错「`-g` 选项里不允许块指令」。
- 第 292 行的 `if (cf->handler)` 是一个**逃生口**：某些块（如 `types { ... }`）需要自定义的解析逻辑，会临时把 `cf->handler` 设成自己的函数，从而绕过通用分发。多数情况下 `cf->handler` 为 `NULL`，走第 319 行的 `ngx_conf_handler(cf, rc)`。
- 注意第 319 行把 `rc`（`NGX_OK` 或 `NGX_CONF_BLOCK_START`）原样传给 `ngx_conf_handler`，后者需要据此判断「这条指令该以 `;` 结尾还是以 `{` 开头」。

再看词法器。先看它的局部状态变量初始化和主循环开头：

[ngx_conf_read_token 状态变量与主循环 — src/core/ngx_conf_file.c:502-531](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L502-L531)

注意第 523 行 `cf->args->nelts = 0`——**每读一条新指令前，先把参数数组清空**（复用数组，不重新分配）。`start` 记录当前 token 在缓冲区里的起点，`start_line` 记录它起始的行号（用于报错定位）。

缓冲区不够时的续读逻辑（「半个 token 前移 + 续读文件」）：

[缓冲区耗尽时把未完成 token 前移并续读 — src/core/ngx_conf_file.c:533-611](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L533-L611)

第 535 行 `if (cf->conf_file->file.offset >= file_size)` 判断文件是否已读完，是则返回 `NGX_CONF_FILE_DONE`（第 552 行）。第 557 行 `if (len == NGX_CONF_BUFFER)` 是保护：如果一个 token 占满了 4KB 还没结束，且不在引号内，直接报「参数过长」。第 579-590 行的 `ngx_memmove` + `ngx_read_file` 正是「前移半个 token、把缓冲剩余空间填满新内容」的关键。

「空白间隙」分支（决定 token 边界与结构字符）：

[last_space 分支：识别 ; { } # 引号 变量 — src/core/ngx_conf_file.c:658-720](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L658-L720)

注意第 669-681 行：在 `last_space` 状态下直接遇到 `;` 或 `{`，意味着「上一条指令的参数已经齐了、现在该收尾」，于是返回 `NGX_OK`（`;`）或 `NGX_CONF_BLOCK_START`（`{`）。第 683-690 行：遇到 `}` 时要求 `cf->args->nelts == 0`（`}` 必须独占，前面不能有残留 token），返回 `NGX_CONF_BLOCK_DONE`。

「token 结束并入库」分支（含转义展开）：

[found 分支：把 token 压入 cf->args 并处理转义 — src/core/ngx_conf_file.c:760-814](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L760-L814)

第 761 行 `ngx_array_push(cf->args)` 把当前 token 追加到参数数组（第 0 个元素就是指令名）。第 766 行 `ngx_pnalloc(... b->pos - 1 - start + 1)` 为 token 分配内存（长度 = 当前位置 - 起点 + 末尾 `\0`）。第 771-801 行的 `for` 循环做转义处理：遇到 `\` 看下一个字符，`\"` `\'` `\\` 原样保留，`\t` `\r` `\n` 展开成真正的制表/回车/换行。

#### 4.1.4 代码实践

**实践目标**：用真实的默认配置文件，手工模拟一次词法器的输出，验证你对返回码的理解。

**操作步骤**：

1. 打开 [conf/nginx.conf](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/conf/nginx.conf)，定位第 3 行 `worker_processes  1;` 与第 12-14 行的 `events { worker_connections 1024; }`。
2. 把自己当成 `ngx_conf_read_token`，对第 3 行逐字符走一遍状态机：
   - 读到 `w`：`last_space==1`，进入 default，`last_space=0`，`start` 指向 `w`。
   - 一直读到 `worker_processes` 后的空格：在 token 中间支遇到空格 → `found=1`，把 `worker_processes` push 进 `cf->args[0]`，`last_space=1`。
   - 读到 `1`：再次进入新 token。
   - 读到 `;`：在 token 中间支遇到 `;` → `found=1`，把 `1` push 进 `cf->args[1]`，**同一次循环内**第 805 行 `if (ch == ';') return NGX_OK;`。
3. 对 `events {` 走一遍：词法器返回 `NGX_CONF_BLOCK_START`，此时 `cf->args[0] == "events"`，`nelts == 1`。

**需要观察的现象**：`worker_processes 1;` 这一行会让词法器**只返回一次** `NGX_OK`，而 `cf->args` 此时含 2 个元素（名字 + 1 个参数）。

**预期结果**：你应当能口述出「`;` 既触发 token 入库、又触发函数返回」这一关键点——这也是为什么第 805 行的 `return` 写在 `found` 分支内部。

> 待本地验证：若你想看真实输出，可用 `--with-debug` 编译 nginx，在 `ngx_conf_read_token` 的 `return NGX_OK;`（第 806 行）前临时加一行 `ngx_log_error(NGX_LOG_NOTICE, cf->log, 0, "token: %V nelts=%ui", &((ngx_str_t*)cf->args->elts)[0], cf->args->nelts);`，再 `nginx -t` 观察 error.log。**注意：本讲义不修改源码，这只是建议的观察手段，实践后请还原。**

#### 4.1.5 小练习与答案

**练习 1**：配置里写 `worker_processes 1`（漏掉分号）直接跟下一行 `events {`，会发生什么？走的是哪段代码？

**参考答案**：词法器读完 `1` 后遇到换行（空白）→ `last_space=1`，接着读 `events`，此时 `cf->args` 里已经有 `{worker_processes, 1}` 两个元素，又追加 `events` 成第三个。当读到 `{` 时返回 `NGX_CONF_BLOCK_START`。分发器 `ngx_conf_handler` 找到 `events` 指令（它是块指令），但 `cf->args` 里夹带了 `worker_processes 1` 这些脏数据——实际上 `worker_processes` 不会作为指令名被匹配（指令名是 `cf->args->elts` 的第 0 个 = `worker_processes`）。所以真实结果是：`worker_processes` 被当作指令名查找，参数是 `1` 和 `events`，因 `worker_processes` 只接受 1 个参数（`NGX_CONF_TAKE1`），参数个数校验失败，报 "invalid number of arguments"。报错点在 [ngx_conf_handler 的 invalid 标签 — src/core/ngx_conf_file.c:435-442](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L435-L442)。

**练习 2**：为什么 `ngx_conf_read_token` 要把 `cf->args->nelts = 0` 放在函数最开头（第 523 行）？如果去掉会怎样？

**参考答案**：因为 `cf->args` 数组是**跨多次调用复用**的（在 `ngx_init_cycle` 里只 `ngx_array_create` 一次）。每次读新指令前必须清零元素计数，否则上一条指令的参数会残留进本条指令，导致指令名错乱、参数个数判定错误。

---

### 4.2 指令分发 ngx_conf_handler

#### 4.2.1 概念说明

词法器只负责「切出一条指令的名字和参数」，但它**不知道**这个名字是不是合法指令、属于哪个模块、接受几个参数、能不能写在当前位置。这些判断全部由分发器 `ngx_conf_handler` 完成。

`ngx_conf_handler` 做的事可以概括为「**两轮匹配 + 三项校验 + 选 conf + 调 set**」：

1. **两轮匹配**：遍历所有已注册模块（`cf->cycle->modules[]`），对每个模块遍历它的指令表 `module->commands`，找到名字相同的那条 `ngx_command_t`。
2. **三项校验**：
   - 模块类型校验：这条指令所属模块的 `type` 必须等于 `cf->module_type`（当前所处块的模块类型）。
   - 作用域校验：指令的 `cmd->type` 必须与 `cf->cmd_type`（当前所处块的作用域）有交集。
   - 结尾符校验：非块指令必须以 `;` 结尾，块指令必须以 `{` 开头；参数个数必须符合 `cmd->type` 里的要求。
3. **选 conf**：根据 `cmd->type` 里的标志（`NGX_DIRECT_CONF` / `NGX_MAIN_CONF` / offset），算出应该把值写进哪个配置结构体的指针。
4. **调 set**：调用 `cmd->set(cf, cmd, conf)`。

理解它的关键在于：**nginx 没有「指令注册表」这种中心化的字典**。一个名字到底是不是合法指令，靠的是「暴力遍历所有模块的所有指令表」。这也是为什么 nginx 启动时配置解析相对慢、但运行时极快——解析只在启动/reload 时发生一次。

#### 4.2.2 核心流程

```
ngx_conf_handler(cf, last):          # last 是 NGX_OK 或 NGX_CONF_BLOCK_START
    name = cf->args[0]               # 指令名
    found = 0                        # 是否见到过同名指令（即使作用域不对）

    for 每个模块 modules[i]:
        for 模块指令表里的每条 cmd:
            if name 与 cmd->name 长度/内容不同: continue
            found = 1                # 见过这个名字了

            if 模块类型 != NGX_CONF_MODULE 且 != cf->module_type: continue
            if (cmd->type & cf->cmd_type) == 0:   continue   # 作用域不符
            if 非块指令但 last != NGX_OK:           报 "缺 ;"
            if 块指令但   last != NGX_CONF_BLOCK_START: 报 "缺 {"
            校验参数个数（见 4.3 节详述），不符报 "invalid number of arguments"

            # 算 conf 指针
            if NGX_DIRECT_CONF:  conf = ((void**)cf->ctx)[module.index]
            elif NGX_MAIN_CONF:  conf = &(((void**)cf->ctx)[module.index])
            elif cf->ctx:        conf = (*(void**)((char*)cf->ctx + cmd->conf))[module.ctx_index]

            rv = cmd->set(cf, cmd, conf)     # 真正执行
            if rv == NGX_CONF_OK:   return NGX_OK
            if rv == NGX_CONF_ERROR: return NGX_ERROR
            报错 rv

    # 遍历完所有模块都没能成功执行
    if found:  报 "directive is not allowed here"   # 见过名字但作用域/类型不符
    else:      报 "unknown directive"               # 根本没这个名字
```

`found` 这个标志很巧妙：它区分了两种失败——「名字根本不存在」与「名字存在但用错了地方」。前者报 `unknown directive`，后者报 `is not allowed here`，这对用户排错非常有帮助（比如把 `location` 写在了 `http {}` 里直接、而不是 `server {}` 里，就会得到后者的提示）。

#### 4.2.3 源码精读

分发器整体与「两轮匹配」：

[ngx_conf_handler 遍历模块与指令表 — src/core/ngx_conf_file.c:355-391](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L355-L391)

第 364 行 `name = cf->args->elts` 取指令名（数组第 0 个元素）。第 368 行 `for (i = 0; cf->cycle->modules[i]; i++)` 遍历所有模块（以 NULL 结尾）。第 375 行内层循环遍历当前模块的指令表，直到遇到 `ngx_null_command`（`name.len == 0`）。第 377-383 行做长度 + 内容比较。第 385 行置 `found = 1`。第 387-390 行做模块类型校验：允许 `NGX_CONF_MODULE`（解析器自身，如 `include` 指令）或与 `cf->module_type` 相同。

结尾符校验：

[块指令必须配 {、简单指令必须配 ; — src/core/ngx_conf_file.c:399-411](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L399-L411)

`NGX_CONF_BLOCK` 是块指令标志。第 399 行：若指令不是块指令，但词法器返回的是 `NGX_CONF_BLOCK_START`（即用户写了 `xxx {`），报「未用 `;` 结尾」。第 406 行反之：若是块指令但词法器返回 `NGX_OK`（用户写了 `xxx;`），报「缺少 `{`」。

作用域校验在第 395 行：

[作用域校验 cmd->type & cf->cmd_type — src/core/ngx_conf_file.c:393-397](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L393-L397)

`cf->cmd_type` 表示「当前正处在哪种块里」（最外层是 `NGX_MAIN_CONF`，进 `events {}` 是 `NGX_EVENT_CONF`，进 `http {}` 是 `NGX_HTTP_MAIN_CONF`，等等）。`cmd->type & cf->cmd_type` 为零说明这条指令不允许写在当前块——这就是 nginx 能强制「`worker_connections` 只能写在 `events {}` 里」「`listen` 只能写在 `server {}` 里」的根本机制。

最后是「选 conf + 调 set」：

[计算 conf 指针并调用 cmd->set — src/core/ngx_conf_file.c:445-476](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L445-L476)

这三种 conf 选取策略对应不同层级的模块，是本讲较难的一点，先建立印象即可（4.3 节与 u3-l2 会深化）：

- `NGX_DIRECT_CONF`（第 449 行）：核心模块用，conf 直接是 `cf->ctx` 数组里按 `module.index` 取出的一项。
- `NGX_MAIN_CONF`（第 452 行）：取 conf 的**地址**（因为 set 回调要往里写一个指针，如 `events {}` 要把构造好的 ctx 挂进去）。
- 其他（第 455 行）：协议层模块用，`cmd->conf` 是一个 offset（如 `NGX_HTTP_LOC_CONF_OFFSET`），用 `cf->ctx + offset` 定位到「这一层的指针数组」，再按 `module.ctx_index` 取出该模块在该层的 conf。

第 463 行 `rv = cmd->set(cf, cmd, conf)` 是真正的执行点。`NGX_CONF_OK`（即 `NULL`）表示成功，`NGX_CONF_ERROR`（即 `(void*)-1`）表示致命错误，其他字符串则是可读的错误描述。

失败时的两类提示：

[unknown directive / not allowed here — src/core/ngx_conf_file.c:480-490](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L480-L490)

#### 4.2.4 代码实践

**实践目标**：体验 `found` 标志带来的两种不同报错，加深对「作用域校验」的直觉。

**操作步骤**（找一个可运行的 nginx，或仅做静态推理）：

1. 准备一个最小坏配置 `bad.conf`：
   ```nginx
   events { }
   http {
       location / {            # 故意把 location 直接写在 http {} 里，而不是 server {} 里
           return 200;
       }
   }
   ```
2. 运行 `nginx -t -c bad.conf`（`-t` 只测试不启动，见 u1-l4）。
3. 观察报错信息。`location` 指令的 `cmd->type` 含 `NGX_HTTP_SRV_CONF`（只能在 server 块），而当前 `cf->cmd_type` 是 `NGX_HTTP_MAIN_CONF`（http 块），第 395 行 `cmd->type & cf->cmd_type` 为零，但 `found` 已被置 1，于是得到 `directive "location" is not allowed here`。

**需要观察的现象**：报错信息精确到**文件名和行号**——这得益于 [ngx_conf_log_error — src/core/ngx_conf_file.c:991-1022](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L991-L1022) 在第 1019-1021 行拼上了 `cf->conf_file->file.name` 和 `cf->conf_file->line`。

**预期结果**：你会看到形如 `"location" directive is not allowed here in /path/bad.conf:3` 的输出。把 `location` 那段挪进 `server { ... }` 后，`-t` 应当通过（或报别的小错，但不再是 "not allowed here"）。

> 待本地验证：若手头没有可运行 nginx，可根据上面的源码路径静态推断报错分支，标注出会命中的代码行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ngx_conf_handler` 在第 368 行用 `for (i = 0; cf->cycle->modules[i]; i++)` 而不是 `for (i = 0; i < module_count; i++)`？

**参考答案**：因为 `cf->cycle->modules` 是一个以 `NULL` 指针结尾的数组（哨兵结尾），遍历时只需判断当前元素是否为 NULL，不需要单独知道总数。这是 C 语言里常见的「无需计数器的数组遍历」写法，nginx 在多处使用。

**练习 2**：某条指令名同时出现在两个不同模块的指令表里（比如名字撞车），`ngx_conf_handler` 会怎么处理？

**参考答案**：以**模块在 `modules[]` 中的先后顺序**为准——先遍历到的那个会先通过类型/作用域校验并执行 `cmd->set`，执行成功（返回 `NGX_CONF_OK`）后第 465-466 行立即 `return NGX_OK`，不再看后面模块的同名指令。所以模块顺序（由 `auto/modules` 与 `ngx_modules.c` 决定，见 u1-l2、u3-l3）会影响同名指令的归属。实际上 nginx 在设计上避免了指令名冲突，但机制上是这样工作的。

---

### 4.3 ngx_command_t 与 conf slot 类型

#### 4.3.1 概念说明

`ngx_command_t` 是每条指令的「**说明书**」——它告诉解析器：这条指令叫什么、接受几种参数形式、能在哪些块里出现、解析时该调用谁。每个模块都会定义一个 `ngx_command_t` 数组（以 `ngx_null_command` 结尾），挂在自己的 `ngx_module_t.commands` 字段上。

`ngx_command_t.type` 是一个 `ngx_uint_t`，里面的位被分成三组（在头文件注释里写得很清楚）：

```
/*
 *        AAAA  number of arguments      低 8 位：参数个数标志
 *      FF      command flags            中间位：块/标志/任意等
 *    TT        command type             高位：作用域（HTTP 的 MAIN/SRV/LOC 等）
 */
```

「参数个数标志」就是 `NGX_CONF_TAKE1`、`NGX_CONF_TAKE2`、`NGX_CONF_TAKE12`（=1或2个）这一族，以及特殊的 `NGX_CONF_FLAG`（正好 on/off）、`NGX_CONF_1MORE`（至少1个）、`NGX_CONF_2MORE`（至少2个）、`NGX_CONF_ANY`（任意个）。

「作用域标志」对 HTTP 层就是 `NGX_HTTP_MAIN_CONF / SRV_CONF / LOC_CONF / UPS_CONF` 等，它们告诉分发器「这条指令可以写在 http{} / server{} / location{} / upstream{} 里」。注意这些是**位掩码**——一条指令可以同时标 `NGX_HTTP_SRV_CONF|NGX_HTTP_LOC_CONF`，表示它在 server 和 location 块里都能用。

「conf slot」则是 nginx 提供的一族**通用 set 回调**。绝大多数指令只是「把一个值写进结构体的某个字段」，逻辑完全一样，只是字段类型不同（数字、字符串、开关、时长、大小）。nginx 把这些公共逻辑抽成 `ngx_conf_set_num_slot`、`ngx_conf_set_str_slot`、`ngx_conf_set_flag_slot`、`ngx_conf_set_size_slot`、`ngx_conf_set_msec_slot` 等，模块只要在 `ngx_command_t` 里填好 `offset`（字段在结构体里的偏移），就能复用。这就是「**用 offset 反射式赋值**」——set 函数通过 `(char*)conf + cmd->offset` 定位到字段地址，再写入。

#### 4.3.2 核心流程

`ngx_command_t` 结构体（6 个字段）：

| 字段 | 含义 |
|------|------|
| `name` | 指令名（`ngx_str_t`） |
| `type` | 类型标志（参数个数 + flags + 作用域，位掩码） |
| `set` | 解析回调函数指针 `char *(*set)(cf, cmd, conf)` |
| `conf` | 用于协议层定位 conf 的 offset（如 `NGX_HTTP_LOC_CONF_OFFSET`） |
| `offset` | 字段在模块 conf 结构体里的偏移（供通用 slot 函数用 `offsetof`） |
| `post` | 后置校验/转换结构指针（可选，如范围检查） |

参数个数校验逻辑（`ngx_conf_handler` 内）用到一个巧妙的查表：

\[ \text{allowed}(n) = \text{cmd->type} \,\&\, \text{argument\_number}[n-1] \]

其中 `argument_number[]` 是一个静态数组，下标 \(n-1\) 对应「允许 n 个参数」的标志位。也就是说，nginx 把「0~7 个参数」预编码成了 `NGX_CONF_NOARGS` 到 `NGX_CONF_TAKE7` 八个位（分别是 \(2^0\) 到 \(2^7\)），再通过查表判断「当前实际参数数 n 是否被允许」。这也是为什么组合形式如 `NGX_CONF_TAKE12`（= `TAKE1|TAKE2`）能直接用按位或表达「接受 1 个或 2 个参数」。

通用 slot 函数的统一套路（以 `set_num_slot` 为例）：

```
ngx_conf_set_num_slot(cf, cmd, conf):
    np = (ngx_int_t*)(conf + cmd->offset)     # 用 offset 定位字段
    if *np != NGX_CONF_UNSET:                 # 已被设过 → "is duplicate"
        return "is duplicate"
    value = cf->args->elts
    *np = ngx_atoi(value[1].data, value[1].len)  # 字符串 → 数字（u2-l2）
    if *np == NGX_ERROR: return "invalid number"
    if cmd->post: return cmd->post->post_handler(cf, post, np)  # 可选后置校验
    return NGX_CONF_OK
```

注意「`is duplicate`」检查：nginx 规定大多数标量指令在同一个作用域里只能写一次，第二次会报 duplicate。这依赖字段被初始化成「未设置」哨兵值（`NGX_CONF_UNSET` 等），merge 阶段再据此决定是否继承上层默认值（u3-l2 详述）。

作用域标志（HTTP 层）：

| 标志 | 值 | 含义 |
|------|----|----|
| `NGX_HTTP_MAIN_CONF` | `0x02000000` | 只能写在 `http {}` 块顶层 |
| `NGX_HTTP_SRV_CONF` | `0x04000000` | 可写在 `server {}` 块 |
| `NGX_HTTP_LOC_CONF` | `0x08000000` | 可写在 `location {}` 块 |
| `NGX_HTTP_UPS_CONF` | `0x10000000` | 可写在 `upstream {}` 块 |
| `NGX_HTTP_SIF_CONF` | `0x20000000` | 可写在 `server` 的 `if` 里 |
| `NGX_HTTP_LIF_CONF` | `0x40000000` | 可写在 `location` 的 `if` 里 |
| `NGX_HTTP_LMT_CONF` | `0x80000000` | 用在 `limit_except` 块里 |

对应三个 conf offset：`NGX_HTTP_MAIN_CONF_OFFSET`、`NGX_HTTP_SRV_CONF_OFFSET`、`NGX_HTTP_LOC_CONF_OFFSET`，它们就是 `offsetof(ngx_http_conf_ctx_t, main_conf/srv_conf/loc_conf)`——即「HTTP 三层配置上下文」里三个指针字段的偏移。

#### 4.3.3 源码精读

`ngx_command_t` 结构体定义：

[ngx_command_s 六字段定义 — src/core/ngx_conf_file.h:77-84](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.h#L77-L84)

紧跟着第 86 行 `#define ngx_null_command { ngx_null_string, 0, NULL, 0, 0, NULL }` 是每个模块指令表都用来收尾的「空指令」。

类型标志宏（低 8 位参数个数 + 中间 flags + 高位作用域）：

[NGX_CONF_TAKE* / BLOCK / FLAG / ANY / 1MORE 等 — src/core/ngx_conf_file.h:22-52](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.h#L22-L52)

第 33-40 行的几个组合宏（`NGX_CONF_TAKE12`、`NGX_CONF_TAKE123` 等）正是前面讲的「按位或组合」。第 42 行 `NGX_CONF_ARGS_NUMBER 0x000000ff` 是「参数个数位」的掩码。第 49-52 行的 `NGX_DIRECT_CONF`、`NGX_MAIN_CONF`、`NGX_ANY_CONF` 是核心层用的作用域/取 conf 方式标志。「未设置」哨兵值在第 56-61 行。

`argument_number[]` 查表数组与参数个数校验：

[argument_number 查表 — src/core/ngx_conf_file.c:48-59](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L48-L59)

[参数个数校验主逻辑 FLAG/1MORE/2MORE/MAX_ARGS/查表 — src/core/ngx_conf_file.c:413-443](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L413-L443)

第 417 行 `NGX_CONF_FLAG`（正好 2 个元素：指令名 + on/off）；第 423 行 `NGX_CONF_1MORE`（至少 2 个）；第 429 行 `NGX_CONF_2MORE`（至少 3 个）；第 435 行防超过 `NGX_CONF_MAX_ARGS`（8）；第 439 行就是上面那个查表公式——`argument_number[cf->args->nelts - 1]` 取出「允许当前参数个数」的位，与 `cmd->type` 按位与，为零则不合法。

三个典型通用 slot 函数：

[ngx_conf_set_flag_slot — on/off → 0/1 — src/core/ngx_conf_file.c:1025-1062](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L1025-L1062)

第 1034 行 `fp = (ngx_flag_t*)(p + cmd->offset)` 正是「offset 反射式定位字段」。第 1042-1046 行用 `ngx_strcasecmp`（大小写不敏感）比较 `on`/`off`，转成 1/0。

[ngx_conf_set_str_slot — 直接拷贝 ngx_str_t — src/core/ngx_conf_file.c:1066-1089](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L1066-L1089)

[ngx_conf_set_size_slot — 调用 ngx_parse_size 支持 K/M 后缀 — src/core/ngx_conf_file.c:1197-1225](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L1197-L1225)

注意第 1214 行 `ngx_parse_size(&value[1])`——这就是 u2-l2 讲过的「带 K/M 单位的大小解析」。同理 [`ngx_conf_set_msec_slot` — src/core/ngx_conf_file.c:1259-1287](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L1259-L1287) 第 1276 行调用 `ngx_parse_time(&value[1], 0)`（0 表示返回毫秒），把 `keepalive_timeout 65;` 这样的写法解析成毫秒数。slot 函数把 u2-l2 的字符串解析器接到了配置系统上。

HTTP 作用域标志与三层上下文：

[NGX_HTTP_MAIN_CONF/SRV_CONF/LOC_CONF 等作用域位掩码 — src/http/ngx_http_config.h:41-47](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_config.h#L41-L47)

[三个 conf offset = offsetof(ngx_http_conf_ctx_t, ...) — src/http/ngx_http_config.h:50-52](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_config.h#L50-L52)

[ngx_http_conf_ctx_t 三层配置指针 — src/http/ngx_http_config.h:17-21](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_config.h#L17-L21)

HTTP 的配置是 main/srv/loc **三层并行**的指针数组（每层按模块的 `ctx_index` 索引）。一条指令通过 `cmd->conf` 选层、`cmd->offset` 选字段，从而精确地把值写到「某个模块在某一层的某个字段」。这是后续 u6（HTTP 核心）反复用到的结构。

`include` 指令的描述符（一个完整真实例子）：

[ngx_conf_commands：include 指令的 ngx_command_t — src/core/ngx_conf_file.c:19-29](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L19-L29)

`include` 的 type 是 `NGX_ANY_CONF|NGX_CONF_TAKE1`——`NGX_ANY_CONF`（`0xFF000000`）意味着它在任何作用域都能用，`TAKE1` 表示正好 1 个参数（文件名）。它的 set 回调是 `ngx_conf_include`，后者内部又调用 `ngx_conf_parse`：

[ngx_conf_include：支持 glob 通配，递归调用 ngx_conf_parse — src/core/ngx_conf_file.c:820-883](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L820-L883)

第 837 行 `strpbrk(file.data, "*?[")` 检测参数是否含通配符，没有就直接 `ngx_conf_parse(cf, &file)`（第 841 行），有则用 `ngx_open_glob`/`ngx_read_glob` 逐个匹配文件再解析（第 858-878 行）。这就是为什么 `include mime.types;`、`include servers/*.conf;` 都能工作。

#### 4.3.4 代码实践

**实践目标**：用一个最小坏配置，触发三种不同的 slot 校验报错，建立「type 标志 → 校验 → 报错」的对应直觉。

**操作步骤**（在 [conf/nginx.conf](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/conf/nginx.conf) 基础上做三组对照实验，每次只改一处后 `nginx -t`）：

1. **duplicate 实验**：把 `events {}` 块改成
   ```nginx
   events {
       worker_connections 1024;
       worker_connections 2048;   # 重复
   }
   ```
   预期：`set_num_slot` 第 1178-1180 行的 `if (*np != NGX_CONF_UNSET) return "is duplicate";` 命中，报 `"worker_connections" directive is duplicate`。

2. **invalid number 实验**：把 `worker_processes 1;` 改成 `worker_processes one;`（非数字）。
   预期：`set_num_slot` 第 1183 行 `ngx_atoi` 返回 `NGX_ERROR`，第 1185 行报 `invalid number`。

3. **参数个数实验**：把 `worker_processes 1;` 改成 `worker_processes 1 2;`（多给一个参数）。
   预期：`worker_processes` 的 type 含 `NGX_CONF_TAKE1`，第 439 行查表 `argument_number[2]`（= `NGX_CONF_TAKE3`）与 `TAKE1` 按位与为零，走 `invalid` 标签报 `invalid number of arguments`。

**需要观察的现象**：三种错误对应三种不同的报错文案，且都带文件名和行号。

**预期结果**：每次实验只触发一种报错；还原后 `nginx -t` 通过。

> 待本地验证：上述报错文案与触发行均依据当前 HEAD 源码推断；若手头有可编译的 nginx，请实际运行 `nginx -t -c <你的配置>` 核对。

#### 4.3.5 小练习与答案

**练习 1**：`keepalive_timeout 65;` 这条指令，从被分发到最终写入字段，经过了哪些函数？字段是什么类型？

**参考答案**：`ngx_conf_handler` 匹配到 `keepalive_timeout`（定义在 `ngx_http_core_module` 的指令表里），它的 set 回调是 `ngx_conf_set_sec_slot`（注意是 `sec` 不是 `msec`，因为该指令语义是秒）。`ngx_conf_set_sec_slot`（[src/core/ngx_conf_file.c:1290-1318](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L1290-L1318)）第 1307 行调用 `ngx_parse_time(&value[1], 1)`（`is_sec=1` 返回秒），通过 `(char*)conf + cmd->offset` 写入一个 `time_t` 字段。这里体现了「指令语义（秒 vs 毫秒）由选哪个 slot 决定」。

**练习 2**：为什么所有 slot 函数开头都要检查 `if (*field != NGX_CONF_UNSET) return "is duplicate";`？这个哨兵值是谁在什么时候设置的？

**参考答案**：nginx 规定标量指令在同一作用域不可重复设置。这条检查依赖字段在解析前被预置成「未设置」哨兵（如 `NGX_CONF_UNSET = -1`）。这个预置发生在模块的 `create_loc_conf`/`create_srv_conf`/`create_main_conf` 回调里——这些回调在进入对应块时被调用（如 `ngx_http_block` 里循环调 `create_*_conf`），把整个 conf 结构体先 `pcalloc` 清零，再把每个标量字段显式设成对应哨兵值。这样 merge 阶段才能区分「用户没设（继承默认）」与「用户设了具体值」。详见 u3-l2。

---

## 5. 综合实践

把三个模块串起来，完成本讲规格里要求的核心任务：**对照 `conf/nginx.conf`，在 `ngx_conf_file.c` 中追踪 `events {}` 和 `http {}` 块是如何被递归解析的，画出一次指令从文本到 set 回调的调用链。**

### 5.1 调用链总图

下面是默认配置里几个关键指令的完整调用链（数字是行号，便于你边读边对照）：

```
ngx_init_cycle (src/core/ngx_cycle.c)
  └─ 设置 conf.module_type = NGX_CORE_MODULE
     conf.cmd_type    = NGX_MAIN_CONF            # ngx_cycle.c:273-274
  └─ ngx_conf_parse(&conf, &cycle->conf_file)     # ngx_cycle.c:286  ← 总入口
       │
       │  打开 nginx.conf, type = parse_file      # ngx_conf_file.c:176-217
       │
       ├─[读 worker_processes 1;]
       │   ngx_conf_read_token → 返回 NGX_OK      # :502, args=[worker_processes,1]
       │   ngx_conf_handler(cf, NGX_OK)           # :319
       │   └─ 匹配 ngx_core_module 的 worker_processes
       │      type=NGX_MAIN_CONF|NGX_DIRECT_CONF|TAKE1，校验通过
       │      conf = conf_ctx[ngx_core_module.index]   # :449-450 (NGX_DIRECT_CONF)
       │      cmd->set = ngx_conf_set_num_slot    # :463
       │      └─ ngx_atoi("1") → 写入 *(ngx_int_t*)(conf+offset)
       │
       ├─[读 events {]
       │   ngx_conf_read_token → 返回 NGX_CONF_BLOCK_START  # args=[events]
       │   ngx_conf_handler(cf, NGX_CONF_BLOCK_START)        # :319
       │   └─ 匹配 events 指令 (定义在 ngx_event.c)
       │      type=NGX_MAIN_CONF|NGX_CONF_BLOCK，校验通过
       │      cmd->set = ngx_events_block        # :463
       │      └─ ngx_events_block (src/event/ngx_event.c:986)
       │           ├─ pcf = *cf                  # :1031  ← 保存当前上下文！
       │           ├─ 建 event 模块的 ctx
       │           ├─ cf->module_type = NGX_EVENT_MODULE    # :1033
       │           ├─ cf->cmd_type    = NGX_EVENT_CONF       # :1034
       │           ├─ ngx_conf_parse(cf, NULL)  # :1036  ← 递归进入块内！
       │           │    │  (type = parse_block, 因为 filename==NULL 且 fd 有效)
       │           │    │
       │           │    ├─[读 worker_connections 1024;]
       │           │    │   ngx_conf_read_token → NGX_OK
       │           │    │   ngx_conf_handler → 匹配 ngx_event_core_module 的指令
       │           │    │   (module_type==EVENT ✓, cmd_type==EVENT_CONF ✓)
       │           │    │   cmd->set = ngx_conf_set_num_slot → 写入
       │           │    │
       │           │    ├─[读 }] ngx_conf_read_token → NGX_CONF_BLOCK_DONE
       │           │    │   type==parse_block → goto done → 本层返回  # :259-266
       │           │    └─ 返回
       │           └─ *cf = pcf                   # :1038  ← 恢复外层上下文！
       │
       ├─[读 http { ... }]（结构同上，但 set 回调是 ngx_http_block）
       │   └─ ngx_http_block (src/http/ngx_http.c:140+)
       │      ├─ pcf = *cf                        # :219
       │      ├─ 建 main/srv/loc 三层 conf
       │      ├─ cf->ctx = ctx
       │      ├─ cf->module_type = NGX_HTTP_MODULE        # :238
       │      ├─ cf->cmd_type    = NGX_HTTP_MAIN_CONF     # :239
       │      └─ ngx_conf_parse(cf, NULL)         # :240  ← 递归解析 http 块内部
       │             │
       │             ├─[读 include mime.types;]
       │             │   └─ cmd->set = ngx_conf_include → 再次 ngx_conf_parse(cf, &file)
       │             │      （又一层递归，打开 mime.types 文件解析）
       │             │
       │             ├─[读 server { ... }]
       │             │   └─ cmd->set = ngx_http_core_server → 再递归 ngx_conf_parse
       │             │      （cmd_type 变 NGX_HTTP_SRV_CONF）
       │             │      └─[读 location { ... }]
       │             │         └─ cmd->set = ngx_http_core_location → 再递归
       │             │            （cmd_type 变 NGX_HTTP_LOC_CONF）
       │             │
       │             └─[读 }] 逐层 BLOCK_DONE 返回
       │
       └─[文件读完] ngx_conf_read_token → NGX_CONF_FILE_DONE → 返回  # :269-277
```

### 5.2 你要完成的任务

1. **画出递归栈**：用一张表，列出解析 `http { server { location / { root html; } } }` 这段时，`ngx_conf_parse` 的调用栈层次、每层的 `module_type` 与 `cmd_type`、每层的 `type`（`parse_file`/`parse_block`）。提示：从最外层 `NGX_CORE_MODULE/NGX_MAIN_CONF/parse_file` 开始，每进一个块深一层。
2. **解释上下文保存/恢复**：在 [ngx_events_block:1031-1038](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L1031-L1038) 与 [ngx_http_block:219-240](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L219-L240) 里都有 `pcf = *cf;` … 改 `cf` … `ngx_conf_parse` … `*cf = pcf;` 的模式。回答：如果不做这个保存/恢复，会发生什么？
3. **观察 `nginx -T`**：运行 `nginx -T`（dump 全量配置）。它能工作，依赖的是本讲源码里的 [`ngx_conf_add_dump` — src/core/ngx_conf_file.c:101-154](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L101-L154)：每打开一个配置文件（包括 `include` 进来的），就把内容 dump 到一个 `ngx_conf_dump_t`。结合 `ngx_conf_parse` 第 219-231 行对 `ngx_conf_add_dump` 的调用，说明 `nginx -T` 输出里为什么会包含被 `include` 的子文件内容。

### 5.3 参考答案要点

**任务 2 答案**：`pcf = *cf` 把整个解析上下文（`ctx`、`module_type`、`cmd_type` 等）压栈保存；递归返回后 `*cf = pcf` 恢复。如果不恢复，内层块（比如 `events {}`）设置的 `module_type=NGX_EVENT_MODULE`、`cmd_type=NGX_EVENT_CONF` 会「泄漏」到外层，导致 `events {}` 之后的顶层指令（如 `http {}`）用错误的作用域去匹配，从而报「unknown directive」或「not allowed here」。这正是 nginx 用「栈式上下文」管理块嵌套的核心机制——**块指令的 set 回调负责在进入/退出时切换 `module_type` 和 `cmd_type`，而 `ngx_conf_parse` 本身对「当前是什么块」一无所知**，它只机械地读 token、分发。

> 待本地验证：任务 3 的 `nginx -T` 行为，以及任务 1 中 `include` 与 `server`/`location` 的确切 cmd_type 切换，建议在有可运行 nginx 的环境下实际走读一遍。

---

## 6. 本讲小结

- nginx 配置解析由三个函数分工：`ngx_conf_parse` 是主驱动（可重入，靠同一个文件缓冲区 + 栈式保存上下文实现块嵌套）；`ngx_conf_read_token` 是逐字符词法状态机，返回 5 种状态码；`ngx_conf_handler` 是分发器，遍历所有模块的指令表做匹配和校验。
- 「块嵌套」不是语法树，而是「块指令的 set 回调再次调用 `ngx_conf_parse(cf, NULL)`」——`filename` 为 `NULL` 表示不重开文件、在当前流上读到匹配的 `}` 即返回本层。
- `ngx_command_t` 是每条指令的说明书，`type` 字段位掩码分三组：低 8 位是参数个数（`TAKE1`…`TAKE7`）、中间是 flags（`BLOCK`/`FLAG`/`ANY`/`1MORE`）、高位是作用域（核心层的 `MAIN_CONF`、HTTP 层的 `NGX_HTTP_MAIN/SRV/LOC_CONF` 等）。
- `cf->module_type` 与 `cf->cmd_type` 是当前块的「身份标签」，块指令进入时切换、退出时恢复；`cmd->type & cf->cmd_type` 为零就报「not allowed here」，这是 nginx 强制指令作用域的根本机制。
- 通用 `ngx_conf_set_*_slot` 函数用 `(char*)conf + cmd->offset` 反射式定位字段，复用了 u2-l2 的 `ngx_atoi`/`ngx_parse_size`/`ngx_parse_time`，并通过「未设置哨兵值」实现「不可重复」与「可继承默认值」。
- 解析只在启动/reload 时跑一次，所以「暴力遍历所有模块指令表」的简单分发策略对运行时性能无影响。

---

## 7. 下一步学习建议

- **u3-l2 cycle 生命周期**：本讲的 `ngx_conf_parse` 是在 `ngx_init_cycle` 里被调用的。下一讲会把 `ngx_init_cycle` 的「解析配置 → 初始化模块 → 打开监听端口」全流程串起来，并讲解 `create_conf`/`init_conf`/`merge_conf` 这些本讲反复提及但未展开的回调，解释「未设置哨兵值」如何在 merge 阶段变成「继承上层默认」。
- **u3-l3 模块系统**：本讲的 `cf->cycle->modules[]` 数组、`module.index`、`module.ctx_index` 从哪来？下一讲讲 `ngx_module_t` 结构、静态模块表 `ngx_modules` 与动态模块加载，把 u1-l2 里的 `ngx_modules.c` 与本讲的分发器接上。
- **u3-l4 指令与地址解析**：本讲的 slot 函数是「文本 → 字段」的桥梁，下一讲讲 `ngx_conf_set_*_slot` 的完整家族、`ngx_inet` 对 `listen` 地址/CIDR 的解析。
- **延伸阅读**：想看更多「块指令递归」的实例，可浏览 [src/http/ngx_http_core_module.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c) 里的 `ngx_http_core_server`（`server {}`）、`ngx_http_core_location`（`location {}`）、`ngx_http_core_types`（`types {}`），它们都遵循本讲总结的「保存 `*cf` → 切换 `module_type`/`cmd_type` → 递归 `ngx_conf_parse` → 恢复 `*cf`」四步范式。
