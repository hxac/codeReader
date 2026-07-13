# 从源码构建与运行 nginx

> 这是《nginx 源码学习手册》的第二篇。上一篇（u1-l1）我们建立了 nginx 的整体印象，并提到一个关键事实：**nginx 的能力由「编译进来的模块」决定，行为由「配置文件」决定**。本篇就回答紧接着的问题——这些模块到底是怎么被「编译进来」的？我们从源码亲手走一遍 `configure → make → 运行` 的全过程，并把每一步对应到 `auto/` 目录下的真实脚本。

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 nginx 的构建系统为什么是**一堆 shell 脚本**而不是 CMake/Makefile 模板，以及 `auto/configure` 这个「总指挥」依次做了哪些事；
- 看懂 `auto/options` 里 `--with-XXX` / `--without-XXX` 开关是怎么被解析的，并能以 `--with-http_v2_module` 为例讲清「一条命令行参数 → 一个 shell 变量」的映射；
- 理解 `auto/sources` 里的 `CORE_DEPS` / `CORE_SRCS` / `EVENT_MODULES` / `EPOLL_MODULE` 等清单是如何与 `auto/modules`、`auto/make` 配合，最终生成 `objs/ngx_modules.c` 和 `objs/Makefile` 的；
- 在本机从源码编译出一个 `objs/nginx` 二进制，并用 `nginx -V` 验证你指定的模块（例如 HTTP/2）确实被编译进来了。

## 2. 前置知识

阅读本讲前，建议你：

- 读完上一篇 u1-l1，了解「静态模块 / 动态模块」「`nginx -V` 查看 configure arguments」等概念；
- 会基本的 shell 操作（`cd`、`./script`、`make`）；
- 大致知道「编译一个 C 程序」需要经历 *预处理 → 编译 → 链接*，并且通常有一个 *配置（configure）* 阶段来探测系统环境。

几个本讲反复出现、但值得先建立直觉的术语：

| 术语 | 直觉解释 |
| --- | --- |
| configure（配置阶段） | 在真正编译前，先探测系统（编译器、操作系统、可选库），并按用户指定的开关决定「编哪些模块」，最后生成一份 Makefile。 |
| `auto/` 目录 | nginx 仓库里全部构建脚本的存放地；`auto/configure` 是入口，其余脚本被它「点 sourcing」进来协同工作。 |
| `objs/` 目录 | 构建的「产物目录」，存放生成的 Makefile、`ngx_modules.c`、自动探测头文件，以及最终的 `nginx` 二进制。 |
| ngx_modules.c | configure 阶段**动态生成**的一个 C 文件，里面有一张「本次编译进来的所有模块」的指针数组——它是「模块系统」在运行时的入口表。 |

> 提示：如果你曾用过 `./configure && make && make install` 三段式安装过别的开源软件，nginx 的流程几乎一样，只是它的 `configure` 用 shell 脚本手写，而不是用 autoconf 工具生成。

## 3. 本讲源码地图

本讲的依据集中在仓库根目录的 `auto/` 脚本与 README 的「Building from source」章节。

| 路径 | 作用 | 本讲如何使用 |
| --- | --- | --- |
| [auto/configure](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/configure) | 构建总入口脚本，按固定顺序调用各子脚本 | 逐段讲解它的执行顺序（4.1） |
| [auto/options](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/options) | 定义所有编译选项的默认值，并解析命令行参数 | 精读选项解析循环与 `--with-http_v2_module`（4.2） |
| [auto/sources](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources) | 列出核心、事件、各操作系统下的源文件与模块清单 | 拆解 CORE/EVENT/HTTP 三类清单（4.3） |
| [auto/modules](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/modules) | 把「选项开关」翻译成「实际编译的模块与源文件」，并生成 `ngx_modules.c` | 串联从开关到产物的关键一环（4.3） |
| [auto/module](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/module) | 单个模块的「登记」原语：决定静态链接 / 动态链接 / 附加模块 | 解释 `ngx_module_link` 三种取值（4.3） |
| [auto/make](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/make) | 生成 `objs/Makefile`（含编译/链接规则） | 讲清 `objs/nginx` 是怎么被链出来的（4.3） |
| [auto/init](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/init) | 定义 `objs/` 下各产物文件的路径名 | 提供「产物放在哪」的对照（4.1） |
| [README.md](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md) | 「Building from source」章节给出官方操作步骤 | 实践步骤的权威依据（第 4 节与综合实践） |

## 4. 核心概念与源码讲解

本讲覆盖三个最小模块：**`auto/configure`（构建总指挥）**、**`auto/options` 中的 `--with/--without-http_*` 开关**、以及 **`auto/sources` 中的 CORE/EVENT/HTTP 模块清单（及其如何变成可编译产物）**。

### 4.1 `auto/configure`：构建的总指挥

#### 4.1.1 概念说明

很多 C 项目用 CMake 或 autotools 生成构建脚本，nginx 却选择了**纯手写的 shell 脚本**来管理整个构建。入口就是 `auto/configure`（README 里让你执行的正是它）。它本身不做太多「具体工作」，而是像一个总指挥，按固定顺序把 `auto/` 下的一堆子脚本「source」（用 `. 路径` 的方式在**同一个 shell 进程**里执行）进来，让这些子脚本共享同一组 shell 变量，协同完成：

1. 解析命令行参数（哪些模块要、哪些不要）；
2. 探测操作系统、编译器、系统头文件、第三方库；
3. 把「要编的模块」汇总成一张模块表，生成 `objs/ngx_modules.c`；
4. 生成 `objs/Makefile`；
5. 打印一份「Configuration summary」。

> 为什么用同一个 shell 进程 source 子脚本？因为 shell 变量默认只在当前进程有效。如果在子进程里执行（比如 `sh auto/options`），它设置的 `HTTP_V2=YES` 之类的变量就回不到 `configure` 主进程。所以这里一律用 `. auto/xxx`（等价于 `source`），保证所有子脚本读写的都是同一份变量。

#### 4.1.2 核心流程

`auto/configure` 的执行顺序可以概括为下面这条流水线（左列是脚本里的 `. auto/xxx` 行，右列是它做的事）：

```text
auto/configure 执行顺序
─────────────────────────────────────────────────────────────────
. auto/options        ← 读默认值 + 解析命令行参数（HTTP_V2=YES 等）
. auto/init           ← 定义 objs/ 下各产物路径（Makefile、ngx_modules.c …）
. auto/sources        ← 载入核心/事件/各 OS 的源文件清单
检测操作系统（uname）
. auto/cc/conf        ← 选定 C 编译器、探测编译特性
. auto/headers        ← 探测系统头文件
. auto/os/conf        ← 按当前 OS 选 config（Linux/FreeBSD/Darwin/Solaris…）
. auto/unix           ← 一堆 POSIX 特性探测（sendfile/kqueue/epoll …）
. auto/threads        ← 线程池支持探测
. auto/modules        ← ★ 把开关翻译成模块表，生成 objs/ngx_modules.c
. auto/lib/conf       ← 第三方库（PCRE/OpenSSL/zlib）探测
处理 --prefix 等安装路径
. auto/make           ← ★ 生成 objs/Makefile
. auto/install        ← 往 Makefile 追加 install 目标
. auto/stubs
. auto/summary        ← 打印 Configuration summary
─────────────────────────────────────────────────────────────────
```

带 ★ 的两步（`auto/modules` 与 `auto/make`）是「从选项到产物」的核心，我们放到 4.3 节细讲。本节先抓住这条主线：**`configure` 本质上是一次顺序的 shell 脚本编排，每一步都往 `objs/` 里写一点东西**。

#### 4.1.3 源码精读

[auto/configure:10-L12](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/configure#L10-L12) —— 这三行依次 source 了 `auto/options`（解析参数）、`auto/init`（定义产物路径）、`auto/sources`（载入源文件清单）。这就是整条流水线的「前置加载」。

[auto/configure:19](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/configure#L19) —— 把你执行 configure 时用的完整命令行（`$NGX_CONFIGURE`）写进 `ngx_auto_config.h` 作为一个宏 `NGX_CONFIGURE`。这正是为什么后来 `nginx -V` 能原样打印出 configure 参数——这些参数在编译期就被「刻」进了二进制。

[auto/configure:27-L48](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/configure#L27-L48) —— 操作系统探测：用 `uname -s/-r/-m` 拼出形如 `Linux:6.x.x:x86_64` 的 `NGX_PLATFORM`，并对 MinGW/MSYS 做了 win32 特判。后续 `auto/os/conf` 会据此选择对应的 `ngx_linux_config.h`、`ngx_freebsd_config.h` 等。

[auto/configure:62-L64](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/configure#L62-L64) —— `. auto/threads`、`. auto/modules`、`. auto/lib/conf` 三连。其中 `. auto/modules`（第 63 行）是「把开关变成模块表」的关键，下文 4.3 详解。

[auto/configure:107-L109](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/configure#L107-L109) —— `. auto/make`、`. auto/lib/make`、`. auto/install`：生成 Makefile 的主体、第三方库的 make 片段、以及 install 目标。完成后 `objs/Makefile` 就齐了。

[auto/configure:121](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/configure#L121) —— `. auto/summary` 打印「Configuration summary」，告诉你最终选了哪些库、二进制/配置/pid 文件分别装到哪里。

至于「产物放在哪」，路径名都在 `auto/init` 里定义：

[auto/init:6-L10](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/init#L6-L10) —— 明确 `NGX_MAKEFILE=objs/Makefile`、`NGX_MODULES_C=objs/ngx_modules.c`、`NGX_AUTO_CONFIG_H=objs/ngx_auto_config.h`。所以你构建时盯住 `objs/` 目录就对了。

[auto/init:45-L53](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/init#L45-L53) —— `configure` 在**仓库根目录**直接写一个顶层 `Makefile`，里面只有 `default: build` 和 `clean` 两个伪目标。你执行 `make` 时，它实际会进入 `objs/Makefile` 去做真正的编译（见 4.3）。

#### 4.1.4 代码实践

1. **实践目标**：在不实际编译的前提下，用源码确认 `auto/configure` 的执行顺序，并验证它会生成顶层 `Makefile`。
2. **操作步骤**：
   - 打开 [auto/configure](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/configure)，把所有 `. auto/xxx` 的行按出现顺序抄下来，与本节 4.1.2 的流水线图对照；
   - 打开 [auto/init:45-L53](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/init#L45-L53)，看清顶层 `Makefile` 只含 `default`/`clean` 两个目标。
3. **需要观察的现象**：你会确认 `configure` 不直接编译任何东西，它的全部产出是「脚本 + 文本文件」（`objs/Makefile`、`objs/ngx_modules.c`、若干 `ngx_auto_*.h`）。
4. **预期结果**：理解「configure 阶段零编译、只生成构建描述」，这是 shell 脚本式构建系统的典型特征。
5. 这一步是纯源码阅读，无运行结果需要验证。

#### 4.1.5 小练习与答案

- **练习 1**：`auto/configure` 为什么用 `. auto/options` 而不是 `sh auto/options` 来调用子脚本？
  - **答案**：因为 `. `（source）在**当前 shell 进程**里执行，子脚本设置的变量（如 `HTTP_V2=YES`）能保留给后续脚本使用；`sh auto/options` 会开一个子进程，变量一退出就丢失。
- **练习 2**：`nginx -V` 打印的 configure 参数是从哪里来的？
  - **答案**：configure 把命令行写进了 `ngx_auto_config.h` 的 `NGX_CONFIGURE` 宏（[auto/configure:19](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/configure#L19)），编译期被刻进二进制，运行时由 `-V` 输出。

---

### 4.2 `auto/options`：`--with` / `--without` 编译开关

#### 4.2.1 概念说明

上一篇我们说过：每个 `--with-XXX` 表示「额外启用」，每个 `--without-XXX` 表示「特意禁用」。这些开关的全部逻辑就集中在 `auto/options` 这一个文件里。它做两件事：

1. **给每一个可选模块一个默认值**（`YES` 默认开 / `NO` 默认关）；
2. **遍历命令行参数**，遇到 `--with-XXX` 就把对应变量设成 `YES`，遇到 `--without-XXX` 就设成 `NO`。

关键设计：**nginx 用一个普通的 shell 变量来表达「这个模块编不编」**。例如 `HTTP_V2` 这个变量——默认是 `NO`（HTTP/2 默认不编），一旦你传 `--with-http_v2_module`，它就被改成 `YES`。后续 `auto/modules` 只需判断 `if [ $HTTP_V2 = YES ]` 就能决定要不要把这个模块加进编译列表。整条链路完全靠 shell 变量串起来，没有任何「配置描述文件」。

> 命名规律（方便你速查）：
> - `--with-http_<name>_module` → 变量 `HTTP_<NAME>`，默认多为 `NO`（需显式开启，如 SSL、HTTP/2）；
> - `--without-http_<name>_module` → 变量 `HTTP_<NAME>`，默认多为 `YES`（默认就编，可关掉，如 gzip、proxy）；
> - 部分模块支持 `=dynamic`，如 `--with-http_xslt_module=dynamic` → 变量设为 `DYNAMIC`，编成独立 `.so`。

#### 4.2.2 核心流程

一条命令行参数（以 `--with-http_v2_module` 为例）的「命运」如下：

```text
命令行:  auto/configure --with-http_v2_module
                 │
                 ▼  auto/options 的 for option 循环逐个处理
case "$option" in
    --with-http_v2_module)  HTTP_V2=YES ;;        ← 把 shell 变量 HTTP_V2 改成 YES
    ...
esac
                 │
                 ▼  其他脚本共享这个变量
auto/modules 里： if [ $HTTP_V2 = YES ]; then
                      把 ngx_http_v2_module + 它的源文件加入编译列表
                  fi
                 │
                 ▼
 objs/ngx_modules.c 里出现 &ngx_http_v2_module
 objs/Makefile   里编译 src/http/v2/ngx_http_v2.c 等源文件
                 │
                 ▼
 nginx -V 的 configure arguments 里回显 --with-http_v2_module
```

注意最后一步「回显」：`auto/options` 会把原始命令行存进 `NGX_CONFIGURE`，所以你后来用 `-V` 看到的参数，和你当初敲的完全一致。

#### 4.2.3 源码精读

先看默认值。`auto/options` 一开头就给所有可选模块赋了默认值：

[auto/options:50-L64](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/options#L50-L64) —— 这里能看到 `HTTP=YES`（HTTP 核心默认开）、`HTTP_SSL=NO`（第 62 行，SSL 默认关）、**`HTTP_V2=NO`（第 63 行，HTTP/2 默认关）**、`HTTP_V3=NO`（第 64 行）。所以「默认编译」的 nginx 并不支持 HTTP/2，必须显式加 `--with-http_v2_module`。

紧接着第 65 行往后是一长串默认 `YES` 的模块（`HTTP_SSI`、`HTTP_GZIP`、`HTTP_PROXY`、`HTTP_FASTCGI`、`HTTP_LIMIT_REQ` 等），它们默认就编，可用 `--without-XXX` 关掉。

然后是参数解析循环：

[auto/options:188](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/options#L188) —— `for option` 开始遍历所有命令行参数（`for option do ... done`，省略了 `in` 列表表示遍历位置参数 `$@`）。

[auto/options:192-L195](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/options#L192-L195) —— 对形如 `--xxx=value` 的参数，用 `sed` 把 `value` 抽出来，方便后面赋值（用于路径类、`=dynamic` 类参数）。

我们要找的 HTTP/2 开关在这里：

[auto/options:242-L244](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/options#L242-L244) —— 这就是本讲的核心三行：`--with-http_ssl_module) HTTP_SSL=YES`、`--with-http_v2_module) HTTP_V2=YES`、`--with-http_v3_module) HTTP_V3=YES`。一个 case 分支把 shell 变量从 `NO` 翻成 `YES`，仅此而已——但这一翻，就决定了后面整个编译。

动态模块的写法可以看 XSLT：

[auto/options:248-L251](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/options#L248-L251) —— `--with-http_xslt_module=dynamic)` 把 `HTTP_XSLT` 设成 `DYNAMIC`（而不是 `YES`）。这个 `DYNAMIC` 取值会被 `auto/module` 识别，从而编成独立 `.so`（见 4.3.3）。

非法参数会被拦下：

[auto/options:431-L434](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/options#L431-L434) —— `case` 的 `*)` 默认分支：遇到不认识的选项就报 `invalid option` 并 `exit 1`。所以拼错选项名会直接失败，不会「悄悄忽略」。

循环结束后，命令行被原样存档：

[auto/options:439](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/options#L439) —— `NGX_CONFIGURE="$opt"`，把归整后的命令行存起来，供前面 [auto/configure:19](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/configure#L19) 写进 `ngx_auto_config.h`。

`--help` 的输出也由本文件维护：

[auto/options:442-L435](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/options#L442) —— `if [ $help = yes ]` 时打印整段帮助并退出。例如 `--with-http_v2_module` 的帮助行在 [auto/options:476](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/options#L476)。想确认某个选项的确切名字和默认行为，`./auto/configure --help` 是最权威的查询方式。

最后，安装路径的默认值也在这里兜底：

[auto/options:643-L661](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/options#L643-L661) —— 若用户没指定，则 `NGX_SBIN_PATH=sbin/nginx`、`NGX_CONF_PATH=conf/nginx.conf`、`NGX_PID_PATH=logs/nginx.pid` 等（都是相对于 `--prefix` 的路径）。

#### 4.2.4 代码实践

1. **实践目标**：确认「`--with-http_v2_module` 唯一的作用就是把 `HTTP_V2` 翻成 `YES`」。
2. **操作步骤**：
   - 在 [auto/options](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/options) 里搜索字符串 `--with-http_v2_module`，定位到第 243 行，确认它对应的动作只有 `HTTP_V2=YES`；
   - 再向上看第 63 行，确认 `HTTP_V2` 的默认值是 `NO`；
   - 运行 `./auto/configure --help | grep v2`，对照帮助行（第 476 行）。
3. **需要观察的现象**：源码中该 case 分支只改一个 shell 变量，没有任何「立即编译」的动作；`--help` 输出里 `--with-http_v2_module` 的描述与源码一致。
4. **预期结果**：你会直观理解 nginx 的开关是「声明式」的——解析阶段只改变量，真正的编译决策推迟到 `auto/modules`。
5. `--help` 部分可立即在本机验证；若暂未安装构建依赖，则**待本地验证**。

#### 4.2.5 小练习与答案

- **练习 1**：不加任何参数直接 `auto/configure`，最终编出来的 nginx 支持 HTTP/2 吗？为什么？
  - **答案**：不支持。因为 `HTTP_V2` 默认是 `NO`（[auto/options:63](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/options#L63)），只有显式传 `--with-http_v2_module` 才会变成 `YES`（[auto/options:243](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/options#L243)）。
- **练习 2**：`--with-http_xslt_module` 和 `--with-http_xslt_module=dynamic` 在 `auto/options` 里分别把变量设成什么？
  - **答案**：前者 `HTTP_XSLT=YES`（静态编进二进制），后者 `HTTP_XSLT=DYNAMIC`（编成独立 `.so`）。见 [auto/options:248-L251](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/options#L248-L251)。
- **练习 3**：如果拼错了选项名，比如 `--with-http_vv2_module`，会发生什么？
  - **答案**：命中 `case` 的默认分支，报 `invalid option` 并 `exit 1`，configure 直接失败（[auto/options:431-L434](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/options#L431-L434)）。

---

### 4.3 `auto/sources` 的模块清单：从源文件到 `objs/nginx`

#### 4.3.1 概念说明

光有「开关变量」还不够，还得知道**每个模块对应哪些 `.c`/`.h` 文件**。这份「文件清单」就写在 `auto/sources` 里。它把源码按层次整理成几组 shell 变量：

- **核心层（CORE）**：内存池、字符串、配置解析、日志等基础设施，无论你开什么模块都要编；
- **事件层（EVENT）**：事件驱动框架 + 各操作系统的后端（epoll/kqueue/poll/select…）；
- **HTTP/MAIL/STREAM 层**：协议相关代码，由各自开关决定是否编；
- **OS 层（Linux/FreeBSD/Darwin/Solaris/Win32）**：按探测到的操作系统挑一组。

但要特别注意：**`auto/sources` 只列「总是要编」的核心/事件清单，以及各事件后端的「定义」**。那些「可选模块」（HTTP/2、SSL、proxy…）的源文件并不直接写在 `auto/sources` 里，而是在 `auto/modules` 中用 `if [ $HTTP_V2 = YES ]` 这种条件**动态追加**的。也就是说：

- `auto/sources` = **静态基础清单**（核心、事件框架、各 OS）；
- `auto/modules` = **按开关动态拼装**（可选 HTTP/MAIL/STREAM 模块），并最终生成 `objs/ngx_modules.c`；
- `auto/module`（单数）= 单个模块的「登记」原语，由 `auto/modules` 反复调用；
- `auto/make` = 读取上面汇总好的源文件清单，生成 `objs/Makefile`。

#### 4.3.2 核心流程

把 4.2 的 `HTTP_V2=YES` 接上，完整的「开关 → 产物」链路是：

```text
HTTP_V2=YES (来自 auto/options)
        │
        ▼  auto/modules 判断
if [ $HTTP_V2 = YES ]; then
    ngx_module_name=ngx_http_v2_module
    ngx_module_srcs="src/http/v2/ngx_http_v2.c ... "
    ngx_module_link=$HTTP_V2            # = YES → 静态链接
    . auto/module                        # 登记这个模块
fi
        │
        ▼  auto/module（单数）按 ngx_module_link 分派
link=YES  → 把模块名追加到 HTTP_MODULES，源文件追加到 HTTP_SRCS   （静态）
link=DYNAMIC → 把模块名加入 DYNAMIC_MODULES，单独编成 .so          （动态）
link=ADDON  → 作为第三方附加模块处理
        │
        ▼  auto/modules 汇总所有模块
modules="$CORE_MODULES $EVENT_MODULES ... $HTTP_MODULES ..."
生成 objs/ngx_modules.c（含 ngx_modules[] 指针数组）
        │
        ▼  auto/make 读取 CORE_SRCS/HTTP_SRCS/...
为每个 .c 写一条编译规则，再写一条链接规则 → 产出 objs/nginx
```

最终你拿到的 `objs/nginx` 二进制里，静态模块的代码都被链接进去了；而 `objs/ngx_modules.c` 里的 `ngx_modules[]` 数组，就是运行时「这个 nginx 一共装了哪些模块」的权威清单（第六单元会用到它）。

#### 4.3.3 源码精读

**① 静态基础清单（`auto/sources`）**

核心模块只有三个，且永远编进去：

[auto/sources:6](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources#L6) —— `CORE_MODULES="ngx_core_module ngx_errlog_module ngx_conf_module"`。这三个是「最底层」的模块（核心、错误日志、配置指令系统），任何 nginx 都离不开。

核心源文件清单分「头文件依赖」和「源文件」两组：

[auto/sources:10-L45](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources#L10-L45) —— `CORE_DEPS` 列出核心层的所有头文件（`ngx_palloc.h`、`ngx_buf.h`、`ngx_cycle.h`、`ngx_conf_file.h`、`ngx_module.h` 等），它们既是编译依赖，也勾勒出 `src/core` 的全貌——这些正是第二单元要逐个精读的文件。

[auto/sources:48-L83](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources#L48-L83) —— `CORE_SRCS` 列出核心层的所有 `.c` 文件（`nginx.c`、`ngx_palloc.c`、`ngx_buf.c`、`ngx_cycle.c`、`ngx_conf_file.c`…）。`auto/make` 会为这里的每个文件生成一条编译规则。

事件层同样有清单，并且把「各操作系统的事件后端」预先定义好：

[auto/sources:86-L103](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources#L86-L103) —— `EVENT_MODULES` 默认含 `ngx_events_module` 和 `ngx_event_core_module`（事件框架本身），`EVENT_SRCS` 含 `ngx_event.c`、`ngx_event_accept.c`、`ngx_event_timer.c` 等。这是第五单元的主角。

[auto/sources:106-L127](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources#L106-L127) —— 这里为每种事件后端定义了「模块名 + 源文件」一对，例如 `EPOLL_MODULE=ngx_epoll_module` / `EPOLL_SRCS=src/event/modules/ngx_epoll_module.c`（第 123–124 行）。具体编哪个后端，由 `auto/os/conf` 在探测到 Linux 后把 `EVENT_MODULES` 追加上 `ngx_epoll_module`（在 `auto/modules` 开头处理）。

**② 按开关动态拼装可选模块（`auto/modules`）**

可选模块的源文件不在 `auto/sources` 里，而是按开关追加。以 HTTP/2 为例：

[auto/modules:59](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/modules#L59) —— `if [ $HTTP = YES ]; then` 开启整个 HTTP 子系统的拼装。`HTTP` 默认是 `YES`（见 [auto/options:50](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/options#L50)），除非你传 `--without-http`。

[auto/modules:105-L107](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/modules#L105-L107) —— 只要 `HTTP_V2` 或 `HTTP_V3` 任一为 `YES`，就把 HPACK/QPACK 用的 Huffman 编解码源文件（`HTTP_HUFF_SRCS`）追加进 `HTTP_SRCS`。这是「模块间共享源文件」的一个例子。

真正登记 HTTP/2 主体模块（含它的全部源文件）的地方：

[auto/modules:424-L439](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/modules#L424-L439) —— `if [ $HTTP_V2 = YES ]; then` 块里：先 `have=NGX_HTTP_V2 . auto/have`（生成一个编译宏，让 C 代码能用 `#ifdef NGX_HTTP_V2` 做条件编译），然后设置 `ngx_module_name=ngx_http_v2_module`、`ngx_module_srcs="src/http/v2/ngx_http_v2.c ngx_http_v2_table.c ngx_http_v2_encode.c ngx_http_v2_module.c"`、`ngx_module_link=$HTTP_V2`（即 `YES`），最后 `. auto/module` 登记它。注意这里把 HTTP/2 的**四个源文件**一次性列了出来——它们只有在你传了 `--with-http_v2_module` 时才会被编译。

此外 HTTP/2 还有一个专门负责「把响应编码成 HTTP/2 帧」的过滤器模块，在更前面登记：

[auto/modules:211-L220](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/modules#L211-L220) —— `ngx_http_v2_filter_module`，同样受 `HTTP_V2` 控制。这就是为什么「开 HTTP/2」会同时编进 `ngx_http_v2_module` 和 `ngx_http_v2_filter_module` 两个模块。

**③ 登记原语的分派（`auto/module`，单数）**

[auto/module:12](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/module#L12) —— `if [ "$ngx_module_link" = DYNAMIC ]; then` 处理动态模块：把模块名加入 `DYNAMIC_MODULES`，准备单独编成 `.so`。

[auto/module:94-L107](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/module#L94-L107) —— `elif [ "$ngx_module_link" = YES ]; then` 处理**静态**模块：把模块名追加到 `${ngx_module_type}_MODULES`（例如 `HTTP_MODULES`），把源文件追加到 `${ngx_var}_SRCS`（例如 `HTTP_SRCS`）。`ngx_http_v2_module` 走的就是这条分支。

[auto/module:128](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/module#L128) —— `elif [ "$ngx_module_link" = ADDON ]; then` 处理 `--add-module=` 引入的第三方附加模块。

**④ 汇总模块表并生成 `ngx_modules.c`（`auto/modules` 末尾）**

[auto/modules:1456](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/modules#L1456) —— `modules="$CORE_MODULES $EVENT_MODULES"`，开始拼装最终的「按顺序排列」的模块列表。

[auto/modules:1465-L1470](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/modules#L1465-L1470) —— 如果开了 HTTP，就把 `$HTTP_MODULES $HTTP_FILTER_MODULES ...` 全部追加进 `modules`。注意这里的**顺序很重要**——HTTP 过滤器链的执行顺序就由它决定（第六单元详解）。

[auto/modules:1553-L1579](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/modules#L1553-L1579) —— 用 `cat << END > $NGX_MODULES_C` 生成 `objs/ngx_modules.c` 的开头，然后循环为每个模块输出 `extern ngx_module_t $mod;`，再输出 `ngx_module_t *ngx_modules[] = { ... };` 指针数组（第 1568 行）。这张数组表就是运行时模块系统的入口——nginx 启动时会遍历它来初始化所有模块（第三单元 u1-l4 / u3-l3 会用到）。

**⑤ 生成 Makefile 并链接出 `objs/nginx`（`auto/make`）**

[auto/make:6](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/make#L6) —— 打印 `creating objs/Makefile`，开始生成 Makefile。

[auto/make:259-L275](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/make#L259-L275) —— 为 `$CORE_SRCS` 里的每个 `.c` 生成一条 `xxx.o: xxx.c` 编译规则。HTTP 的源文件同理在 [auto/make:290-L317](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/make#L290-L317) 处理——所以 `HTTP_V2=YES` 时，`src/http/v2/ngx_http_v2.c` 等就会被这几行生成的规则编译成 `.o`。

最关键的链接规则：

[auto/make:228-L235](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/make#L228-L235) —— `build: binary modules manpage`；`binary: objs/nginx`；而 `objs/nginx` 的生成规则是用 `$(LINK)` 把前面所有 `.o`（含 `ngx_modules.o`）和库链接到一起。**这就是 `objs/nginx` 二进制的诞生地**。动态模块则在 [auto/make:503-L674](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/make#L503-L674) 单独循环，每个编成一个 `.so`。

> 串联小结：`auto/options`（开关）→ `auto/sources`（基础清单）+ `auto/modules`（按开关追加 + 生成 `ngx_modules.c`）→ `auto/make`（按清单生成 Makefile）→ `make` 真正编译链接 → `objs/nginx`。

#### 4.3.4 代码实践

1. **实践目标**：亲手从源码编译出一个带 HTTP/2 的 nginx，并验证它确实被编进二进制。
2. **操作步骤**（依据 README「Building from source」章节）：
   - 安装构建依赖（README [README.md:129-L156](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L129-L156)）：`sudo apt install gcc make libpcre3-dev zlib1g-dev`（若要 SSL 再加 `libssl-dev`）；
   - 在仓库根目录执行配置（README [README.md:164-L171](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L164-L171)）：
     ```bash
     ./auto/configure --with-http_v2_module --with-http_ssl_module
     ```
   - 编译（README [README.md:176-L181](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L176-L181)）：
     ```bash
     make
     ```
   - 定位二进制并验证（README [README.md:183-L188](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L183-L188)）：
     ```bash
     objs/nginx -V 2>&1 | tr ' ' '\n' | grep -E 'http_v2|http_ssl|configure'
     ```
3. **需要观察的现象**：
   - `configure` 末尾打印的「Configuration summary」会列出 PCRE/zlib/OpenSSL 的来源，以及 binary/conf/pid 的安装路径；
   - `objs/` 目录下生成了 `Makefile`、`ngx_modules.c`、`nginx`（二进制）；
   - 用 `grep ngx_http_v2_module objs/ngx_modules.c` 能在生成的 C 文件里看到 `&ngx_http_v2_module,`，证明它已被登记；
   - `objs/nginx -V` 的 `configure arguments` 里能看到 `--with-http_v2_module`。
4. **预期结果**：`objs/nginx -V` 输出的 configure 参数回显了你传入的 `--with-http_v2_module`，且 `objs/ngx_modules.c` 含 `ngx_http_v2_module`——两条证据共同确认 HTTP/2 已被编译进来。若想进一步安装到 `/usr/local/nginx/`，可执行 `sudo make install`（README [README.md:183-L191](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L183-L191)），但本实践不强求安装，直接用 `objs/nginx` 即可。
5. 因本环境未必具备完整构建依赖与权限，上述编译运行的输出**待本地验证**；源码阅读部分（确认 `ngx_modules.c` 的生成逻辑）可立即完成。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `src/http/v2/ngx_http_v2.c` 没有出现在 `auto/sources` 的 `CORE_SRCS` 里？
  - **答案**：因为它是**可选模块**的源文件，由 `auto/modules` 在 `HTTP_V2=YES` 时动态追加到 `HTTP_SRCS`（[auto/modules:424-L439](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/modules#L424-L439)）。`auto/sources` 只负责「总是要编」的核心/事件基础清单。
- **练习 2**：`auto/module`（单数）和 `auto/modules`（复数）职责有什么不同？
  - **答案**：`auto/module` 是「登记一个模块」的原语，根据 `ngx_module_link`（`YES`/`DYNAMIC`/`ADDON`）决定静态/动态/附加（[auto/module:12-L178](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/module#L12-L178)）；`auto/modules` 是编排者，反复调用 `auto/module` 来登记所有模块，最后汇总生成 `objs/ngx_modules.c`。
- **练习 3**：`objs/ngx_modules.c` 里的 `ngx_modules[]` 数组有什么用？
  - **答案**：它是「本次编译进来的所有模块」的指针数组，是运行时模块系统的入口表；nginx 启动时会遍历它来执行各模块的初始化回调（见 [auto/modules:1568](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/modules#L1568)，后续 u3-l3 详解）。

## 5. 综合实践

把本讲三个最小模块（`auto/configure` 总指挥、`auto/options` 开关、`auto/sources`+`auto/modules`+`auto/make` 清单与产物）串起来，完成下面这个贯穿任务：

> **任务**：跟踪一条 `--with-http_v2_module` 参数，从命令行一直追到 `objs/nginx` 二进制，说清「它在每一层留下了什么痕迹」。

建议步骤：

1. **在 `auto/options` 找入口**：确认 `--with-http_v2_module` 把变量 `HTTP_V2` 从默认 `NO`（[auto/options:63](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/options#L63)）翻成 `YES`（[auto/options:243](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/options#L243)），并被存进 `NGX_CONFIGURE`（[auto/options:439](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/options#L439)）。
2. **在 `auto/modules` 看拼装**：找到 `if [ $HTTP_V2 = YES ]` 块（[auto/modules:424](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/modules#L424)），记下它登记的模块名和源文件清单；再确认这些模块最终进了 `modules` 总列表（[auto/modules:1465-L1470](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/modules#L1465-L1470)）。
3. **生成产物并验证**：执行 `./auto/configure --with-http_v2_module && make`，然后：
   - `grep ngx_http_v2_module objs/ngx_modules.c` —— 应看到 `&ngx_http_v2_module,` 出现在指针数组里；
   - `ls objs/src/http/v2/` —— 应看到编译产物 `ngx_http_v2.o` 等；
   - `objs/nginx -V 2>&1 | grep http_v2` —— 应在 configure arguments 里回显 `--with-http_v2_module`。
4. **对照总结**：把上面三步的「痕迹」整理成一张表（参数层 / 构建脚本层 / 生成文件层 / 二进制层），你就完整还原了「一条编译开关的全生命周期」。

> 若本机缺少构建依赖或权限，步骤 3 的实际编译输出**待本地验证**；步骤 1、2 的源码追踪可立即完成，已是本任务的核心收获。

## 6. 本讲小结

- nginx 的构建系统是**纯 shell 脚本**：`auto/configure` 是总指挥，按固定顺序 source 各子脚本，全程共享同一组 shell 变量（[auto/configure:10-L121](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/configure#L10-L121)）。
- 每个编译开关就是一个 shell 变量：`auto/options` 先设默认值（如 `HTTP_V2=NO`），再用 `for option` 循环把 `--with-XXX` 翻成 `YES`、`--without-XXX` 翻成 `NO`、`=dynamic` 翻成 `DYNAMIC`（[auto/options:188-L436](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/options#L188-L436)）。
- `auto/sources` 提供「总是要编」的核心/事件基础清单（`CORE_MODULES`/`CORE_SRCS`/`EVENT_MODULES`/各 OS 后端），而可选模块的源文件由 `auto/modules` 按开关动态追加（[auto/sources:6-L127](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources#L6-L127)、[auto/modules:424-L439](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/modules#L424-L439)）。
- `auto/modules` 汇总所有模块并生成 `objs/ngx_modules.c`（含 `ngx_modules[]` 指针数组），`auto/make` 据此生成 `objs/Makefile`，最终链接出 `objs/nginx`（[auto/modules:1553-L1579](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/modules#L1553-L1579)、[auto/make:228-L235](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/make#L228-L235)）。
- 验证某模块是否被编译进来有两条证据：`objs/ngx_modules.c` 里有它的指针、`nginx -V` 的 configure arguments 回显了对应开关。
- 官方推荐的从源码构建流程就是 `auto/configure [选项] && make && make install`，二进制默认产出在 `objs/nginx`、安装到 `/usr/local/nginx/`（README [README.md:164-L205](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L164-L205)）。

## 7. 下一步学习建议

本篇让你具备了「从源码造一个 nginx」的能力，并理解了模块如何被编译进来。接下来：

- **读第三篇（u1-l3《源码目录结构与分层总览》）**：对照 `auto/sources` 里的 `CORE_DEPS`/`EVENT_SRCS` 清单，把 `src/core`、`src/event`、`src/http`、`src/stream`、`src/mail`、`src/os` 各目录的职责理清，建立一张「源码导航地图」。
- **读第四篇（u1-l4《程序启动入口 main() 全流程》）**：进入 `src/core/nginx.c` 的 `main()`，第一次逐段读 C 代码——你会发现 `objs/ngx_modules.c` 生成的 `ngx_modules[]` 数组正是启动时被遍历初始化的对象。
- **第三单元（配置解析与模块系统）**：本篇你看到了模块在**编译期**如何被登记；第三单元会讲它们在**运行期**如何被加载、如何提供配置指令（`ngx_module_t` 结构、`create_conf`/`init_module` 回调）。
- 长期参考：官方 [Building nginx from Source](https://nginx.org/en/docs/configure.html) 列出了全部 configure 选项，可作为 `auto/options --help` 的补充手册。
