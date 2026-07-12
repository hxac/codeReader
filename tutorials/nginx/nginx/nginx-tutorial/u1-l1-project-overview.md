# nginx 是什么：项目定位与核心概念

> 本讲是《nginx 源码学习手册》的第一篇。我们从最基础的问题开始：nginx 到底是什么、它能做什么、它的程序大致长什么样。本篇几乎不涉及 C 代码细节，目标是为后续阅读源码建立一个清晰的整体印象。

## 1. 本讲目标

学完本讲后，你应当能够：

- 用一句话说清 nginx 的产品定位，并列举它的典型使用场景；
- 说明 nginx 为什么不使用「单进程大循环」而采用 **master + worker** 的多进程模型；
- 理解 nginx 的 **模块化（modules）** 思想，以及静态模块与动态模块的区别；
- 知道 nginx 是 **配置驱动（configuration-driven）** 的软件，并且掌握用 `nginx -V` 查看编译进来的模块；
- 对 nginx 的源码目录分层有一个鸟瞰式的认识，为第二单元（core 核心基础设施）做好铺垫。

## 2. 前置知识

阅读本讲之前，你只需要具备以下基础：

- **会用命令行**：能执行 `nginx`、`curl` 这类命令即可。
- **大概知道什么是 Web 服务器**：一个接收 HTTP 请求、返回网页或数据的程序。
- **大概知道什么是进程**：操作系统里正在运行的程序实例。你不需要理解 fork、信号、epoll 的细节——这些会在后续讲义中从零讲解。

几个本讲会出现、但后续会深入展开的术语，先建立直觉即可：

| 术语 | 直觉解释 |
| --- | --- |
| 反向代理（Reverse Proxy） | 代替真正的后端服务器接收客户端请求，再把请求转发给后端，最后把后端的响应回给客户端。客户端「以为」自己在和 nginx 对话。 |
| 负载均衡（Load Balancer） | 把大量请求按照某种策略分发到多台后端服务器，避免某一台被压垮。 |
| 模块（Module） | 一段可插拔的功能单元，编译进 nginx 后提供一组配置指令（directive）和能力。 |
| 共享内存（Shared Memory） | 多个进程都能读写的同一块内存，nginx 用它在 worker 之间同步状态。 |

## 3. 本讲源码地图

本讲的依据主要是项目根目录的 README，它用结构化语言介绍了 nginx 的工作原理。源码目录只做鸟瞰，不深入。

| 路径 | 作用 | 本讲如何使用 |
| --- | --- | --- |
| [README.md](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md) | 项目自述文件，包含 *How it works / Modules / Configurations / Runtime* 等章节 | 本讲核心依据，逐段精读 |
| [src/core/nginx.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.h) | 版本号等编译期常量定义 | 用它确认当前源码版本 |
| `src/` 下的子目录 | 源码分层目录 | 仅做目录鸟瞰，为后续讲义铺垫 |

> 说明：讲义规格中提到的 `docs/xml/index.xml` 在当前仓库中**并不存在**（`docs/xml/nginx/` 下只有 `changes.xml` 变更日志）。为保证准确，本讲不引用该文件，全部依据真实存在的 README.md。

## 4. 核心概念与源码讲解

本讲覆盖 README 中的三个最小模块：**Modules（模块化）**、**Runtime（进程模型）**、以及作为总纲的 **How it works**；并补充 *Configurations（配置驱动）* 作为通向后续讲义的桥梁。

### 4.1 产品定位：nginx 是什么

#### 4.1.1 概念说明

很多人以为 nginx「只是一个 Web 服务器」，这只说对了一半。README 在第一句就给出了完整定位——nginx 同时是：

- Web 服务器（直接把磁盘上的网页返回给客户端）；
- 高性能负载均衡器（把请求分发到多台后端）；
- 反向代理（替后端接收并转发请求）；
- API 网关（在请求进入后端前做鉴权、限流、改写等）；
- 内容缓存（把后端的响应缓存到磁盘/内存，下次直接返回）。

这一句话很重要：它解释了为什么 nginx 的源码里会同时存在「静态文件处理」「upstream 反向代理」「proxy_cache 缓存」「limit_req 限流」等看似不相关的子系统——它们都服务于上面这五个角色。

#### 4.1.2 核心流程

从「客户端发起请求」到「拿到响应」，nginx 的高层流程可以概括为：

```text
客户端
  │  HTTP/TCP/UDP 请求
  ▼
nginx worker 进程（事件驱动接收请求）
  │
  ├── 角色 A：Web 服务器 → 读本地文件，返回
  ├── 角色 B：反向代理  → 转发给后端，再把后端响应回传
  ├── 角色 C：负载均衡  → 在多台后端中挑一台
  ├── 角色 D：API 网关  → 限流 / 鉴权 / 改写头部
  └── 角色 E：内容缓存  → 命中缓存则直接返回，否则回源
  │
  ▼
客户端拿到响应
```

注意：这五个角色并不互斥，同一个 nginx 实例常常同时承担好几个角色。具体承担哪些，完全由配置文件决定（见 4.4 节）。

#### 4.1.3 源码精读

README 开篇第一句给出了 nginx 的官方定位（多角色合一）：

[README.md:12](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L12) —— 这一行明确写出 nginx 是 Web Server、Load Balancer、Reverse Proxy、API Gateway、Content Cache 的集合体，是理解整个项目范围的「总纲」。

当前源码对应的版本号定义在：

[src/core/nginx.h:12-L14](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.h#L12-L14) —— 这里用宏定义了 `nginx_version`、`NGINX_VERSION`（字符串 `"1.31.3"`）和 `NGINX_VER`。这个版本号最终会出现在 `nginx -V` 的输出里。后续读源码时，遇到「这是哪个版本的实现」之类的问题，都可以回到这个文件核对。

#### 4.1.4 代码实践

1. **实践目标**：确认你手头研究的是哪个版本的 nginx。
2. **操作步骤**：
   - 打开 [src/core/nginx.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.h)，读出 `NGINX_VERSION` 的值；
   - 在命令行执行 `nginx -v`（小写 v，只输出版本）。
3. **需要观察的现象**：`nginx -v` 的输出形如 `nginx version: nginx/1.31.3`。
4. **预期结果**：命令行输出的版本号应与 `nginx.h` 中 `NGINX_VERSION` 宏的值一致。如果你装的是发行版自带的 nginx，版本号可能不同——那是正常的，因为打包版本与本仓库 HEAD 不一定相同。
5. 若你暂时没有可运行的 nginx 二进制，可只做源码阅读部分；运行结果**待本地验证**。

#### 4.1.5 小练习与答案

- **练习 1**：README 把 nginx 描述成哪几个角色的集合？
  - **答案**：Web Server、Load Balancer、Reverse Proxy、API Gateway、Content Cache（共五个，见 [README.md:12](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L12)）。
- **练习 2**：当前源码仓库的版本号定义在哪个文件的哪一行？
  - **答案**：[src/core/nginx.h:13](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.h#L13)，`NGINX_VERSION` 宏，当前值为 `"1.31.3"`。

---

### 4.2 Modules：模块化架构

#### 4.2.1 概念说明

nginx 不是一个「所有功能都写死在一起」的庞然大物，而是由一个个 **模块（module）** 拼装而成。README 对模块的定义是：每个模块通过提供「额外的、可配置的特性」来扩展核心功能。

这一点至关重要，因为它直接决定了 nginx 源码的组织方式：

- 你在配置文件里写的每一条指令（directive，例如 `proxy_pass`、`limit_req`、`gzip`），几乎都来自某个具体模块；
- 模块决定 nginx **能做什么**，配置决定 nginx **具体怎么做**；
- 模块可以静态编译进二进制，也可以动态加载。

#### 4.2.2 核心流程

按「分发方式」划分，nginx 模块有两类：

```text
                 nginx 模块
                 ┌───┴───┐
            静态模块        动态模块
             │                │
   编译时（./configure）   编译成独立的 .so 文件
   打包进同一个 nginx 二进制   运行时用 load_module 加载
   不能卸载                  可单独安装/升级/卸载
```

- **静态模块（static modules）**：在执行 `auto/configure` 时被选定，与 nginx 核心一起编译进同一个 `nginx` 二进制文件，分发时随二进制一起走。是否包含某个静态模块，由编译选项（如 `--with-http_v2_module`、`--without-http_gzip_module`）决定。
- **动态模块（dynamic modules）**：自 1.9.11 起支持。它们被编译成独立的共享库（`.so`），在运行时通过配置文件里的 `load_module` 指令加载，可以在不重新编译 nginx 本体的前提下增删功能。

> 提示：动态模块也可以选择在编译时静态地打包进 nginx。也就是说「静态/动态」是**分发与加载方式**的区别，而不是「功能强弱」的区别。

#### 4.2.3 源码精读

README 的 *Modules* 章节给出了模块化的定义：

[README.md:56-L57](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L56-L57) —— 说明 nginx 由若干模块组成，每个模块通过「额外的、可配置的特性」扩展核心功能；要查看完整的官方模块清单，应参考 nginx 文档底部的「Modules reference」。

紧接着它说明了静态与动态两种分发方式：

[README.md:59](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L59) —— 明确「nginx 模块既可以作为静态模块构建和分发，也可以作为动态模块」，并指向 *Dynamic Modules* 小节了解动态模块的获取与配置。

查看「当前这个 nginx 二进制到底编译进来了哪些**静态**模块」，README 给出了最实用的命令：

[README.md:62-L66](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L62-L66) —— 提示用 `nginx -V`（大写 V）查看编译时启用的静态模块，并列出了 `configure` 参数。

#### 4.2.4 代码实践

1. **实践目标**：列出你本机 nginx 启用的静态模块，理解「同一个 nginx，不同模块组合 = 不同能力」。
2. **操作步骤**：
   - 在命令行执行 `nginx -V`（**大写 V**）；
   - 观察输出末尾的 `configure arguments:` 那一行，里面会列出形如 `--with-http_ssl_module`、`--with-http_v2_module`、`--without-http_gzip_module` 等开关。
3. **需要观察的现象**：`nginx -V` 会把编译时的 configure 参数完整打印出来，每个 `--with-XXX` 表示「额外启用」，每个 `--without-XXX` 表示「特意禁用」。没有出现在列表里的「默认启用」模块也会随二进制分发，只是不在参数里显式列出。
4. **预期结果**：你能据此回答「我的这个 nginx 支持 HTTP/2 吗？支持 SSL 吗？」——只要看到对应的 `--with-http_v2_module` / `--with-http_ssl_module` 即可确认。
5. 若没有可运行的 nginx，可对照第二讲（构建）先用 `auto/configure` 编译一个再验证；当前**待本地验证**。

#### 4.2.5 小练习与答案

- **练习 1**：静态模块和动态模块最本质的区别是什么？
  - **答案**：在于**分发与加载方式**。静态模块在编译期（`auto/configure`）就被决定，编进同一个 nginx 二进制；动态模块编译成独立的 `.so`，运行时用 `load_module` 加载，可以单独安装和升级（见 [README.md:59](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L59)）。
- **练习 2**：想确认某个模块是否被编译进 nginx，应该用哪个命令？
  - **答案**：`nginx -V`（大写 V），它会打印编译时的 `configure arguments`（见 [README.md:64](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L64)）。

---

### 4.3 Runtime：master/worker 进程模型

#### 4.3.1 概念说明

这是 nginx **高性能**的关键设计之一。README 在 *Runtime* 章节明确指出：nginx 不像很多传统程序那样跑在「单个、庞大的进程」里，而是以**一组进程**的形式运行。具体包含：

- 一个 **master 进程**：负责管理（维护）worker 进程，并读取、评估配置文件；
- 一个或多个 **worker 进程**：真正处理数据（例如 HTTP 请求）。

为什么要这样分？直觉上有两个好处：

1. **分工清晰**：master 负责「指挥和配置」，worker 负责「干活」，互不干扰。
2. **横向扩展**：worker 数量可以根据 CPU 核数自动调整，让 nginx 能跨核高效分发工作，从而突破「单进程」在并发上的天花板。

#### 4.3.2 核心流程

nginx 启动后的进程拓扑大致如下：

```text
        nginx（你启动的命令）
              │ fork
              ▼
   ┌─────────────────────┐
   │   master 进程       │   读配置、监控 worker、处理信号（reload/升级）
   └──────────┬──────────┘
              │ fork 出多个
   ┌──────────┼──────────┐
   ▼          ▼          ▼
 worker1   worker2   workerN      每个都用事件驱动处理成千上万的并发连接
   │          │          │
   └──────────┴──────────┴─── 共享内存 ──┐
                              （worker 之间通过共享内存同步状态）
```

几个要点（后续讲义会展开）：

- worker 数量由配置里的 [`worker_processes`](https://nginx.org/en/docs/ngx_core_module.html#worker_processes) 指令控制，既可以写死，也可以设为 `auto` 自动匹配 CPU 核数；
- 多个 worker 之间通过 **共享内存（shared memory）** 同步数据，因此很多指令需要显式申请一块共享内存区域（例如限流要用 `limit_req_zone` 定义一个共享区）；
- 真正的「并发能力」来自每个 worker 内部的**事件驱动**循环（epoll 等），而不是靠多线程——这一点是第五单元的主题。

#### 4.3.3 源码精读

README 的 *Runtime* 章节给出了进程模型的权威描述：

[README.md:74-L77](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L74-L77) —— 明确 nginx 不是单进程巨兽，而是一组进程：一个 master 进程负责维护 worker 并读取/评估配置；一个或多个 worker 进程负责处理实际数据（如 HTTP 请求）。

关于 worker 数量的可配置性：

[README.md:79](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L79) —— 说明 worker 数量在配置文件中定义，可以写死，也可以自动按可用 CPU 核数调整；nginx 被设计成能高效地把工作分摊到所有 worker 上。

关于进程间通过共享内存同步：

[README.md:82](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L82) —— 解释了为什么很多指令需要分配共享内存区，并以限流（rate limiting）为例：要把客户端访问记录在一个公共内存区里，所有 worker 才能读到「某客户端在一段时间内访问了多少次」。

> 鸟瞰提示：上面这套进程模型的 C 代码实现位于 `src/os/unix/ngx_process_cycle.c`（如 `ngx_master_process_cycle`、`ngx_worker_process_cycle`），共享内存在 `src/core/ngx_slab.c`、`src/os/unix/ngx_shmem.c`。这些只是给你一个「将来去哪儿看」的索引，本讲不展开。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看到 nginx 运行时的 master + worker 进程结构。
2. **操作步骤**：
   - 启动 nginx（如 `nginx` 或 `sudo /usr/local/nginx/sbin/nginx`）；
   - 用 `ps -ef | grep nginx` 或 `pgrep -a nginx` 查看进程列表；
   - 在配置里把 `worker_processes` 分别改成 `1` 和 `auto`，每次改完执行 `nginx -s reload`，再观察进程数量变化。
3. **需要观察的现象**：你会看到一个 master 进程（通常以 root 启动）和若干 worker 进程（通常以 `nobody` 或配置指定的用户运行）；worker 数量随 `worker_processes` 变化。
4. **预期结果**：`worker_processes 1` 时只有 1 个 worker；`worker_processes auto` 时 worker 数量通常等于 CPU 逻辑核数。
5. 若暂时无法启动 nginx，可先做源码阅读：在 `src/os/unix/ngx_process_cycle.c` 中搜索 `ngx_master_process_cycle` 和 `ngx_worker_process_cycle` 两个函数名，确认它们的存在即为达成目标；运行验证部分**待本地验证**。

#### 4.3.5 小练习与答案

- **练习 1**：master 进程和 worker 进程各自的职责是什么？
  - **答案**：master 负责**维护 worker 进程**并**读取/评估配置文件**；worker 负责**处理实际数据**（如 HTTP 请求）。见 [README.md:76-L77](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L76-L77)。
- **练习 2**：为什么像「限流」这样的功能需要共享内存？
  - **答案**：因为有多个 worker 各自独立处理请求，要让所有 worker 都知道「某个客户端访问了多少次」，就必须把计数放在一块**所有 worker 都能读写**的公共内存区里。见 [README.md:82](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L82)。
- **练习 3**：把 `worker_processes` 设为 `auto` 意味着什么？
  - **答案**：让 nginx 自动按可用 CPU 核数决定 worker 数量，以在多数情况下最优地分摊负载。见 [README.md:79](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L79)。

---

### 4.4 Configurations：配置驱动机制（通向后续讲义的桥梁）

#### 4.4.1 概念说明

nginx 是高度可配置的：它通过**基于文本的配置文件**接受一组称为「指令（directive）」的参数来驱动行为。换句话说，nginx 的「能力」由编译进来的模块决定，而「怎么用这些能力」则完全由配置文件决定。

理解这一点之后，源码阅读的两条主线就清晰了：

1. **模块系统主线**：模块如何被注册、如何提供指令（第三单元）；
2. **配置解析主线**：nginx 如何把文本配置文件解析成内存里的结构（第三单元的 `ngx_conf_parse`）。

#### 4.4.2 核心流程

```text
文本配置文件（nginx.conf）
   │  由配置解析器逐条读取
   ▼
每一条指令 ──► 找到所属模块 ──► 调用模块提供的 set 回调
   │
   ▼
内存里的配置结构体（main/srv/loc 三层）   ← 第六单元详解
   │
   ▼
运行时各模块读取这些结构体来决定行为
```

一个关键推论：**你的发行版里能用哪些指令，取决于它编译进来了哪些模块**。这也是为什么 README 反复强调「能用的指令集合依赖于可用模块」。

#### 4.4.3 源码精读

[README.md:68-L69](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L68-L69) —— 说明 nginx 通过基于文本的配置文件、以「指令（directive）」为单位来配置自身，并指向官方文档了解配置文件的结构。

[README.md:71-L72](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L71-L72) —— 强调「你能用哪些指令，取决于你的 nginx 编译/加载了哪些模块」，把「模块」和「配置」这两个概念正式联系起来。

> 鸟瞰提示：配置文件解析的主入口是 `src/core/ngx_conf_file.c` 里的 `ngx_conf_parse`；一份默认配置示例在仓库的 `conf/nginx.conf`。这些都属于第三单元，本讲只需建立「文本 → 解析 → 内存结构 → 驱动行为」这条直觉。

#### 4.4.4 代码实践

1. **实践目标**：感受「同一份 nginx 二进制，不同配置 = 不同行为」。
2. **操作步骤**：
   - 找到配置文件（默认常在 `/usr/local/nginx/conf/nginx.conf` 或 `/etc/nginx/nginx.conf`；仓库里也有一份示例 `conf/nginx.conf`）；
   - 用 `nginx -t` 测试配置语法是否正确（不真正启动）；
   - 修改 `worker_processes` 的值后再次 `nginx -t`，体会「改文本就能改行为」。
3. **需要观察的现象**：`nginx -t` 输出 `the configuration file ... syntax is ok` 与 `test is successful`。
4. **预期结果**：你会直观看到 nginx 是「配置优先」的——行为不写在代码里，而写在配置里。
5. 运行结果**待本地验证**（需要本机有 nginx 与配置文件）。

#### 4.4.5 小练习与答案

- **练习 1**：「你能用哪些指令」由什么决定？
  - **答案**：由你的 nginx 编译/加载了哪些**模块**决定。见 [README.md:71-L72](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L71-L72)。
- **练习 2**：用哪个命令只校验配置而不真正启动 nginx？
  - **答案**：`nginx -t`（test configuration）。

## 5. 综合实践

把本讲的三条主线（产品定位、模块化、进程模型）串起来，完成下面这个贯穿任务：

> **任务**：在本机启动一个 nginx，确认它「是谁、能做什么、怎么跑的」。

步骤建议：

1. **它是谁**：执行 `nginx -v`，记录版本号；回到源码 [src/core/nginx.h:13](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.h#L13) 核对版本是否一致（若你用的是发行版自带版本，可能不一致，记下差异）。
2. **它能做什么**：执行 `nginx -V`（大写 V），把 `configure arguments` 那一行整段抄下来；逐个标注每个 `--with-XXX` / `--without-XXX` 是「额外启用」还是「特意禁用」，并据此判断它是否支持 SSL、HTTP/2 等。
3. **它怎么跑的**：用 `ps -ef | grep nginx` 查看 master 与 worker 进程；记录 worker 数量，并对照配置文件里的 `worker_processes` 解释这个数量是怎么来的。
4. **它返回什么**：按 README 的示例执行 `curl localhost`，确认能拿到欢迎页（输出以 `<!DOCTYPE html>` 开头，标题为 `Welcome to nginx!`，见 [README.md:202-L213](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L202-L213)）。

把以上四步的观察结果整理成一张小表，你就拥有了「这台 nginx 的能力画像」——这正是后续源码阅读的现实参照物。若本机暂无 nginx，可先用第二讲的方法从源码编译一个，再回头完成本任务；运行部分**待本地验证**。

## 6. 本讲小结

- nginx 不仅是 Web 服务器，同时是负载均衡器、反向代理、API 网关和内容缓存（[README.md:12](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L12)）。
- nginx 由若干**模块**拼装而成，模块分静态（编译时打进二进制）和动态（运行时 `load_module` 加载）两种（[README.md:56-L66](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L56-L66)）。
- `nginx -V`（大写 V）能列出编译时启用的静态模块与 configure 参数。
- nginx 以**一组进程**运行：一个 master 管配置与 worker，多个 worker 干活（[README.md:74-L77](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L74-L77)）。
- worker 之间通过**共享内存**同步状态，限流等跨 worker 功能依赖它（[README.md:82](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L82)）。
- nginx 是**配置驱动**的：能力由模块决定，行为由配置文件决定（[README.md:68-L72](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/README.md#L68-L72)）。

## 7. 下一步学习建议

本讲只建立了「nginx 是什么」的整体印象，几乎没有碰 C 代码。建议按下面的顺序继续：

- **紧接着读第二篇（u1-l2《从源码构建与运行 nginx》）**：学习 `auto/configure`、`auto/options`、`auto/sources`，亲手从源码编译出一个 nginx 二进制——这是后续所有「运行验证」类实践的前提。
- **再读第三篇（u1-l3《源码目录结构与分层总览》）**：把 `src/core`、`src/event`、`src/http`、`src/stream`、`src/mail`、`src/os` 的职责理清，建立源码导航地图。
- **然后读第四篇（u1-l4《程序启动入口 main() 全流程》）**：进入 `src/core/nginx.c` 的 `main()`，第一次真正逐段读 C 代码。
- 完成第一单元后，第二单元将深入 `src/core` 的内存池、字符串、容器、buf、时间与日志——那是读懂一切后续源码的地基。

> 推荐长期参考资料：官方文档首页 <https://nginx.org/en/docs/>、[Beginner's Guide](https://nginx.org/en/docs/beginners_guide.html)、以及本仓库 README 中反复指向的 [Configuration / Building](https://nginx.org/en/docs/configure.html) 页面。
