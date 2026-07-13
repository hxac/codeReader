# cycle 生命周期：配置加载与初始化

## 1. 本讲目标

上一讲（u3-l1）我们弄清了「一段纯文本 `nginx.conf` 是怎么被切成 token、分发到模块、最终调用 `cmd->set` 的」。但那只是整个启动故事的一半——`ngx_conf_parse` 是被谁调用的？解析出来的配置又存到了哪里？谁来负责真正打开监听端口、创建共享内存、初始化模块？本讲要回答这些问题。

本讲的核心对象是 **`ngx_cycle_t`**——nginx 运行时的「全局上下文容器」，以及构造它的函数 **`ngx_init_cycle`**。你可以把 cycle 想象成一台组装到一半的服务器：从一块空主板（内存池）开始，逐个装上 CPU（模块）、刷入固件（配置）、插上硬盘（共享内存/文件）、接上网线（监听端口），最终通电开机。

学完本讲，你应当能够：

1. 说清楚 `ngx_cycle_t` 结构体里几大类字段（配置、连接、文件、共享内存、模块）各自的用途，理解它为什么是「整个进程的全局变量」。
2. 画出 `ngx_init_cycle` 的多阶段流程，标注出「创建池与容器 → 调模块 `create_conf` → 解析配置 → 调模块 `init_conf` → 打开文件/共享内存 → 打开监听端口 → 调 `init_module`」这几大阶段的边界函数。
3. 区分核心模块的两类配置回调（`create_conf` / `init_conf`）与 `ngx_module_t` 上的进程级回调（`init_module` / `init_process`）的调用时机。
4. 解释 `nginx -t` 为什么能在「不真正启动」的情况下检查配置——它的本质是跑完 `ngx_init_cycle` 后立即返回，并理解哪一步失败会让 `-t` 报错。
5. 理解 reload 与二进制升级如何复用旧 cycle 的资源（监听套接字、共享内存）。

本讲是 u3-l1 的下游、u3-l3（模块系统）与第四单元（进程模型）的上游。

---

## 2. 前置知识

### 2.1 为什么需要 cycle 这个容器

nginx 是一个多模块、多进程的程序。启动时，几十个模块各自需要一块「配置结构体」；运行时，所有 worker 又需要共享同一批「监听端口」「共享内存」「打开的文件」。如果这些数据散落在各模块的全局变量里，配置的 reload（重新加载）就成了噩梦——你没法把「旧配置」和「新配置」同时握在手里做切换。

`ngx_cycle_t` 把所有这些「进程级共享状态」收拢进一个结构体。进程里有一个全局指针 `ngx_cycle` 指向当前生效的 cycle。reload 时，master 进程会**构造一个全新的 cycle**，构造成功后才把 `ngx_cycle` 切过去、销毁旧 cycle；构造失败则丢弃新 cycle、继续用旧的——这就是 nginx「reload 失败不影响线上服务」的实现根基。

### 2.2 三种调用 ngx_init_cycle 的场景

`ngx_init_cycle` 的参数是 `ngx_cycle_t *old_cycle`——一个**旧 cycle**。理解它的行为，必须分清三种调用场景：

| 场景 | `old_cycle` 是什么 | 特点 |
|------|--------------------|------|
| 首次启动 | 一个「初始化 cycle」（`init_cycle`，其 `conf_ctx == NULL`） | 宏 `ngx_is_init_cycle(old_cycle)` 为真，没有可继承的资源 |
| `nginx -s reload`（master 收到 SIGHUP） | 当前正在运行的 cycle | 新 cycle 要继承旧的监听 fd、共享内存，实现「平滑切换」 |
| worker 自身不会调 | —— | `ngx_init_cycle` 只在 master（或单进程模式的主进程）里跑 |

二进制升级（`SIGUSR2`）是另一条路径——新进程通过环境变量 `NGINX` 继承监听 fd（u1-l4 讲过 `ngx_add_inherited_sockets`），不在本讲的 `old_cycle` 复用机制内，但两者都服务于同一个目标：**切换配置/二进制时不丢连接**。

### 2.3 与前序讲义的衔接

- **u1-l4 main() 全流程**：讲过 `ngx_init_cycle(&init_cycle)` 是启动中段的核心一步，`-t`/`-v`/`-s` 在它之前或之后短路。本讲展开它的内部。
- **u3-l1 配置解析器**：讲过 `ngx_conf_parse` 的词法/分发/执行三层。本讲你会看到 `ngx_init_cycle` 是**谁**设置了 `conf.module_type = NGX_CORE_MODULE`、`conf.cmd_type = NGX_MAIN_CONF`，又是**谁**调用了 `ngx_conf_parse`。
- **u2-l1 内存池**：`ngx_init_cycle` 第一步就是 `ngx_create_pool`，整个 cycle 的所有结构体都分配在这个池上，销毁 cycle = 销毁池。本讲会反复见到「分配失败 → `ngx_destroy_pool(pool); return NULL;`」这个模式。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/core/ngx_cycle.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.h) | `ngx_cycle_t`（cycle 容器）、`ngx_core_conf_t`（核心模块配置）、`ngx_shm_zone_t`（共享内存区）的结构体定义，以及 `ngx_init_cycle` 等函数声明。 |
| [src/core/ngx_cycle.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c) | `ngx_init_cycle` 全部实现、共享内存区注册 `ngx_shared_memory_add`、pid 文件管理、旧 cycle 清理。本讲主战场。 |
| [src/core/ngx_module.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.c) | `ngx_cycle_modules`（把静态模块表拷进 cycle）、`ngx_init_modules`（调用每个模块的 `init_module`）。 |
| [src/core/nginx.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c) | `ngx_core_module` 的定义、它的 `create_conf` / `init_conf` 回调实现，以及 `main()` 里对 `ngx_init_cycle` 的调用点。 |
| [src/core/ngx_connection.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_connection.c) | `ngx_open_listening_sockets`——真正 `socket()`+`bind()`+`listen()` 打开监听端口的函数。 |
| [src/core/ngx_module.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.h) | `ngx_module_t`（模块结构，含 `init_module`/`init_process` 等回调）与 `ngx_core_module_t`（核心模块上下文，含 `create_conf`/`init_conf`）。 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 `ngx_cycle_t`：全局上下文容器**——cycle 里装了什么，字段怎么分类。
- **4.2 `ngx_init_cycle` 主流程**——从 `ngx_create_pool` 到 `return cycle` 的多阶段装配线。
- **4.3 模块生命周期回调**——`create_conf` / `init_conf`（核心模块配置回调）与 `init_module`（进程级回调）的分工与时机。
- **4.4 打开监听端口与新旧 cycle 复用**——`ngx_open_listening_sockets`，以及 reload 时如何继承旧 fd。

### 4.1 ngx_cycle_t：全局上下文容器

#### 4.1.1 概念说明

`ngx_cycle_t` 是 nginx 里最重要的结构体之一。它承载了进程运行所需的几乎所有「共享状态」。理解它的字段组成，就理解了 nginx 运行时需要管理哪几类资源。

它有以下几个特点：

1. **一切都在一个池上**：`cycle->pool` 是这个 cycle 专属的内存池（u2-l1），下面所有字段指向的结构体都分配在这个池里。销毁 cycle 就是 `ngx_destroy_pool(cycle->pool)`，一次回收所有内存。
2. **配置按模块分格存放**：`cycle->conf_ctx` 是一个「按模块 `index` 索引」的指针数组，每个核心模块在数组里有一格，存它自己的配置结构体。
3. **资源清单是「预分配容器」**：`listening`（监听端口）、`paths`（缓存目录等路径）、`open_files`（要打开的文件）、`shared_memory`（共享内存区）这几个容器在 cycle 初始化时就被 `ngx_array_init` / `ngx_list_init` 预先建好（容量参考旧 cycle），随后在配置解析阶段由各模块往里填，最后在 cycle 提交阶段被「物化」（真正打开/分配）。

#### 4.1.2 核心流程

`ngx_cycle_t` 的字段可以分成五类来记：

| 类别 | 代表字段 | 说明 |
|------|----------|------|
| 基础设施 | `pool`、`log`、`new_log`、`old_cycle` | 内存池、日志、指向旧 cycle 的指针（reload 用） |
| 配置 | `conf_ctx`、`conf_file`、`conf_param`、`prefix`、`hostname` | 配置上下文数组、配置文件名、命令行 `-g` 参数、工作目录、主机名 |
| 模块 | `modules`、`modules_n` | 本 cycle 使用的模块指针数组（以 NULL 结尾）及其数量 |
| 资源清单（解析阶段填充） | `listening`、`paths`、`open_files`、`shared_memory` | 监听端口、路径、文件、共享内存区——先登记后物化 |
| 连接运行时（worker 使用） | `connections`、`read_events`、`write_events`、`free_connections`、`connection_n` | 连接池与读写事件数组，worker 事件循环的支柱 |
| 调试/管理 | `config_dump`、`config_dump_rbtree` | 支持 `nginx -T` dump 全量配置 |

注意 `conf_ctx` 的类型是 `void ****`——四个星号。这是因为：它本身是「按模块 index 索引的指针数组」（去掉一星 `void***`）；对于核心模块，每一格直接指向该模块的配置结构体；而对于像 HTTP 这样的「框架型」核心模块，那一格指向的是 `ngx_http_conf_ctx_t`——它本身又是一个含三个指针（main/srv/loc）的结构体。所以最内层有多个层次的指针。本讲只用到最外层「核心模块配置」这一层。

#### 4.1.3 源码精读

`ngx_cycle_s` 结构体定义：

[ngx_cycle_s 全字段定义 — src/core/ngx_cycle.h:39-86](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.h#L39-L86)

对照阅读时注意几个关键字段：第 40 行 `conf_ctx`（配置数组）、第 41 行 `pool`（内存池）、第 52 行 `modules`（模块数组）、第 60 行 `listening`（监听端口数组）、第 67-68 行 `open_files` / `shared_memory`、第 73-75 行 `connections` / `read_events` / `write_events`（连接运行时）、第 77 行 `old_cycle`（指向旧 cycle）。

紧跟着的 `ngx_core_conf_t` 是「核心模块」`ngx_core_module` 自己的配置结构体，存的都是 `worker_processes`、`daemon`、`master`、`pid`、`user` 这类进程级参数：

[ngx_core_conf_t：核心模块配置 — src/core/ngx_cycle.h:89-122](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.h#L89-L122)

`ngx_core_conf_t` 的实例就挂在 `cycle->conf_ctx[ngx_core_module.index]` 上。后面你会看到 `ngx_init_cycle` 通过 `ngx_get_conf(cycle->conf_ctx, ngx_core_module)` 把它取出来。

「是否为初始化 cycle」的判断宏：

[ngx_is_init_cycle 宏 — src/core/ngx_cycle.h:125](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.h#L125)

`conf_ctx == NULL` 即说明这个 cycle 还没有装载任何配置——它就是 `main()` 里那个用来启动装配的「空壳」`init_cycle`。`ngx_init_cycle` 内部多处用这个宏区分「首次启动」（无可继承资源）与「reload」（有旧资源可复用）。

`ngx_get_conf` 宏——从 conf_ctx 里按模块取出配置：

[ngx_get_conf 宏 — src/core/ngx_conf_file.h:176](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.h#L176)

它就是 `conf_ctx[module.index]`。`module.index` 是模块在全局模块表里的下标（u3-l3 详述），所以 `conf_ctx` 必须按 `index` 而不是 `ctx_index` 索引。

#### 4.1.4 代码实践

**实践目标**：建立对 cycle 五类字段的直觉，能在源码里快速定位「某项运行时数据存在 cycle 的哪个字段」。

**操作步骤**：

1. 打开 [ngx_cycle.h:39-86](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.h#L39-L86)，按本节 4.1.2 的五类分类，给每个字段标注它属于哪一类。
2. 自测三个问题：
   - 「当前进程打开了哪些监听端口」存在哪个字段？（答：`listening`，它是个 `ngx_array_t`，元素是 `ngx_listening_t`。）
   - 「worker 处理连接时用到的连接池」存在哪？（答：`connections` / `free_connections`。）
   - 「限流模块要跨 worker 共享的那块内存」登记在哪？（答：`shared_memory`，元素是 `ngx_shm_zone_t`。）

**需要观察的现象**：你会发现 cycle 几乎不存「单个值」，而是存「容器」（array/list）。这是因为配置解析阶段还不知道最终要开多少端口、多少共享区，所以先建空容器，解析时往里 push。

**预期结果**：你能口述「nginx 运行时的全部共享状态都被 `ngx_cycle_t` 收拢」这一设计，并理解为什么 reload 必须构造一个全新的 cycle（这样旧 cycle 仍完整可用，新 cycle 失败可整体丢弃）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `conf_ctx` 要预分配 `ngx_max_module` 个格子（见 4.2.3 的 `ngx_pcalloc(pool, ngx_max_module * sizeof(void *))`），而不是按实际模块数 `modules_n` 分配？

**参考答案**：`ngx_max_module = ngx_modules_n + NGX_MAX_DYNAMIC_MODULES`（u3-l3 讲过），它预留了动态模块的索引空间。配置解析过程中可能通过 `load_module` 指令加载动态模块（`ngx_add_module` 会把新模块追加进 `cycle->modules` 并占用新的 `index`），其配置也要存进 `conf_ctx[index]`。如果只按静态模块数分配，加载动态模块时就会越界。预分配上限保证索引空间一次性到位。

**练习 2**：`cycle->log` 和 `cycle->new_log` 有什么区别？

**参考答案**：装配阶段 `cycle->log` 指向旧 cycle 的日志（因为新日志还没建好，先用旧的打日志）；配置解析过程中，`error_log` 指令会把新日志的描述写进 `new_log`；装配接近尾声时（4.2.3 第 411 行）执行 `cycle->log = &cycle->new_log;`，完成日志切换。这样新 cycle 启用配置里指定的新日志目标，而装配过程中的报错仍能用旧日志输出。

---

### 4.2 ngx_init_cycle 主流程：从配置文本到可运行 cycle

#### 4.2.1 概念说明

`ngx_init_cycle(old_cycle)` 是一条**线性装配线**：它从 `old_cycle` 借来少量信息（日志、路径前缀、配置文件名），然后在全新的内存池上一点点搭出一个完整可用的 `cycle`。整条线大致分七个阶段，任何一个阶段失败都会回滚（`goto failed` 或直接 `return NULL`），保证不留半成品。

这条线最关键的认知是：**配置解析（阶段 3）只负责「填登记表」，真正的「物化」（打开文件、分配共享内存、绑定端口）发生在后面的阶段 5、6**。这就是为什么 u3-l1 讲的 `cmd->set` 回调里那些 `ngx_conf_set_num_slot` 只是「把值写进结构体」，并不会去真正 `bind` 端口——物化要等所有配置都解析完、确认无冲突后再统一做。

另一个关键认知是阶段 2 和阶段 4 的「核心模块配置回调」对称结构：**先 `create_conf` 建空壳（含哨兵值）→ 解析配置往里填值 → 再 `init_conf` 校验并补默认值**。这套「未设置哨兵 + init 阶段填默认」的机制，正是 u3-l1 反复提到却未展开的内容。

#### 4.2.2 核心流程

`ngx_init_cycle` 的七阶段伪代码：

```
ngx_init_cycle(old_cycle):
    ── 阶段 0：时间更新（刷新时区/缓存时间）
    pool = ngx_create_pool(NGX_CYCLE_POOL_SIZE, log)         # 建新池
    cycle = ngx_pcalloc(pool, sizeof(ngx_cycle_t))           # 建空 cycle
    cycle->pool = pool; cycle->old_cycle = old_cycle
    从 old_cycle 复制 conf_prefix/prefix/error_log/conf_file/conf_param
    预建空容器：paths / config_dump / open_files / shared_memory / listening
    conf_ctx = ngx_pcalloc(pool, ngx_max_module * sizeof(void*))   # 配置数组
    取 hostname

    ── 阶段 1：装载模块表
    ngx_cycle_modules(cycle)            # 把静态 ngx_modules[] 拷进 cycle->modules

    ── 阶段 2：核心模块 create_conf（建配置空壳，填哨兵值）
    for 每个 CORE 类型模块:
        if module->create_conf:  conf_ctx[module.index] = create_conf(cycle)

    ── 阶段 3：解析配置
    初始化 ngx_conf_t（args/temp_pool/ctx/cycle/module_type=CORE/cmd_type=MAIN_CONF）
    ngx_conf_param(&conf)               # 先解析命令行 -g 参数
    ngx_conf_parse(&conf, &conf_file)   # 再解析主配置文件  ← u3-l1 的入口
    （若 ngx_test_config）打印 "syntax is ok"

    ── 阶段 4：核心模块 init_conf（校验 + 补默认值）
    for 每个 CORE 类型模块:
        if module->init_conf:  init_conf(cycle, conf_ctx[module.index])

    if 进程身份 == SIGNALLER:  return cycle     # nginx -s 只需配置，不开端口

    ── 阶段 5：物化文件资源
    （若 -t 或非首次启动）创建/切换 pid 文件
    ngx_test_lockfile / ngx_create_paths / ngx_log_open_default
    打开 open_files 里登记的所有文件
    cycle->log = &cycle->new_log

    ── 阶段 6：物化共享内存 + 监听端口
    为每个 shared_memory 分配 shm（能与旧 cycle 同名同 size 同 tag 的则复用）
    与 old_cycle 比对 listening：同地址同类型的 fd 直接继承（remain=1）
    ngx_open_listening_sockets(cycle)   # 真正 socket()+bind()+listen()
    （非 -t）ngx_configure_listening_sockets(cycle)   # 设各种 setsockopt

    ── 阶段 7：提交
    ngx_init_modules(cycle)             # 调每个模块的 init_module；失败 exit(1)
    清理 old_cycle 里不再需要的 shm/端口/文件
    return cycle

failed:                                 # 阶段 5/6/7 失败的统一回滚
    关闭新开的文件、释放新分配的 shm、关闭新开的端口
    （若 -t）直接销毁池返回 NULL；否则也销毁返回 NULL
```

几个贯穿全程的设计要点：

- **失败处理分两种风格**：阶段 0-4 的失败多为「分配/解析失败」，直接 `ngx_destroy_cycle_pools(&conf); return NULL;`（连临时池一起销毁）；阶段 5-7 的失败用 `goto failed;`，因为此时已经打开了一些资源，需要先回滚再销毁。
- **临时池 `conf.temp_pool`**：配置解析过程中的临时分配（如 `ngx_palloc` 给 token 用）走临时池，装配完成后第 781 行 `ngx_destroy_pool(conf.temp_pool)` 单独销毁——不污染主池。
- **`environ` 的保存/恢复**：第 251 行 `senv = environ;` 保存环境，解析阶段某些指令可能改 `environ`，失败时第 281/309 行 `environ = senv;` 恢复，避免污染。

#### 4.2.3 源码精读

函数签名与开头的时间刷新、池/cycle 创建：

[ngx_init_cycle：建池、建 cycle、从 old_cycle 复制路径信息 — src/core/ngx_cycle.c:38-124](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L38-L124)

注意第 64 行 `ngx_time_update()`——先刷新时间缓存（u2-l5），因为后面日志要用到准确时间。第 69 行 `ngx_create_pool(NGX_CYCLE_POOL_SIZE, log)` 建新池，第 75 行在池上 `pcalloc` 出 cycle 主体。第 85-124 行从 `old_cycle` 把 `conf_prefix`、`prefix`、`error_log`、`conf_file`、`conf_param` 这些字符串复制过来（用 `ngx_pstrdup` / `ngx_cpystrn` 在新池上重新存一份，因为新 cycle 不能引用旧池的内存）。

预建空容器与配置数组：

[预建 paths/config_dump/open_files/shared_memory/listening 容器 + conf_ctx 数组 — src/core/ngx_cycle.c:127-204](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L127-L204)

注意容器的初始容量都参考旧 cycle（如第 127 行 `n = old_cycle->paths.nelts ? ... : 10`）——reload 时新 cycle 大概率开差不多的资源，预分配合理容量避免频繁扩容。第 200 行 `conf_ctx = ngx_pcalloc(pool, ngx_max_module * sizeof(void *))` 是配置数组（4.1.5 练习 1 解释了为何按 `ngx_max_module`）。

**阶段 1-2：装载模块 + create_conf**

[ngx_cycle_modules + 遍历核心模块调 create_conf — src/core/ngx_cycle.c:227-248](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L227-L248)

第 227 行 `ngx_cycle_modules(cycle)` 把静态 `ngx_modules[]` 拷贝到 `cycle->modules`（详见 4.3.3）。第 233-248 行的循环只关心 `type == NGX_CORE_MODULE` 的模块——这类模块才有「配置上下文」`ngx_core_module_t`（含 `create_conf`/`init_conf`）。第 240 行判断有 `create_conf` 才调，第 246 行把返回的配置结构体指针挂进 `conf_ctx[module->index]`。以核心模块 `ngx_core_module` 为例，这里调用的就是 `ngx_core_module_create_conf`（4.3.3 详述），返回一个填好哨兵值的 `ngx_core_conf_t`。

**阶段 3：解析配置**

[组装 ngx_conf_t + ngx_conf_param + ngx_conf_parse — src/core/ngx_cycle.c:251-295](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L251-L295)

第 254-274 行组装 `ngx_conf_t`：`conf.ctx = cycle->conf_ctx`、`conf.module_type = NGX_CORE_MODULE`、`conf.cmd_type = NGX_MAIN_CONF`——**这正是 u3-l1 反复提到的「最外层块的身份标签」的设置点**。第 280 行 `ngx_conf_param(&conf)` 先解析命令行 `-g` 传入的指令，第 286 行 `ngx_conf_parse(&conf, &cycle->conf_file)` 才解析主配置文件（u3-l1 的总入口就在这里）。第 292-295 行：若处于 `-t` 模式，打印 `the configuration file ... syntax is ok`——注意它发生在**解析成功之后、`init_conf` 与打开端口之前**，这意味着 `-t` 即使打印了 "syntax is ok"，后续阶段仍可能失败。

**阶段 4：init_conf + signaller 早退**

[遍历核心模块调 init_conf + SIGNALLER 早退 — src/core/ngx_cycle.c:297-318](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L297-L318)

第 297-314 行的循环与阶段 2 对称，只是这次调 `init_conf`，传入第 306 行 `cycle->conf_ctx[module->index]`（即阶段 2 建好、阶段 3 填好的配置）。`init_conf` 负责校验 + 把仍是哨兵值的字段补上默认值。第 316-318 行很关键：若进程身份是 `NGX_PROCESS_SIGNALLER`（即 `nginx -s xxx` 发信号），直接 `return cycle`——发信号只需要解析出 pid 文件路径等配置，不需要真正打开端口。

**阶段 5-6 交界：物化共享内存 + 监听端口比对 + 打开端口**

[创建共享内存 + 比对 listening + ngx_open_listening_sockets — src/core/ngx_cycle.c:415-638](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L415-L638)

这段较长，抓三个要点：第 420-508 行为每个登记的共享内存区分配内存，并与 `old_cycle` 比对——同名同 size 同 tag 的直接复用旧地址（第 476 行 `shm_zone[i].shm.addr = oshm_zone[n].shm.addr;`），这是 reload 时不丢失限流计数等共享状态的关键。第 513-630 行把新 cycle 的 `listening` 与旧 cycle 的逐一比对，同地址同类型的把旧 fd 直接挪给新 cycle（第 539 行 `nls[n].fd = ls[i].fd;`，并标记 `ls[i].remain = 1` 表示「旧的需要保留」），这样 reload 后端口不重新绑定。第 632 行 `ngx_open_listening_sockets(cycle)` 才真正为「新增/变更」的端口做 `socket/bind/listen`（详见 4.4）。第 636 行 `if (!ngx_test_config) ngx_configure_listening_sockets(cycle)`——`-t` 模式跳过 `setsockopt` 配置。

**阶段 7：提交 + init_module**

[提交：ngx_init_modules（失败 exit(1)） — src/core/ngx_cycle.c:649-652](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L649-L652)

第 649 行 `ngx_init_modules(cycle)` 调用每个模块的 `init_module` 回调。注意它的失败处理是 `exit(1)` 而非 `goto failed`——注释明确写 `/* fatal */`，因为到了这一步 cycle 已经几乎装配完成、资源已大量物化，回滚代价太大且意义不大。成功后第 655-781 行清理 `old_cycle` 里不再使用的共享内存/端口/文件，最后返回 cycle。

`failed` 标签与 `main()` 对返回值的处理：

[failed 标签：回滚新开的文件/shm/端口 — src/core/ngx_cycle.c:833-953](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L833-L953)

第 933-936 行：若处于 `-t` 模式，回滚后直接销毁池返回 NULL。回到 [main() — src/core/nginx.c:293-301](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L293-L301)，`ngx_init_cycle` 返回 NULL 时，`main()` 第 295-298 行打印 `configuration file ... test failed` 并 `return 1`；返回非空时第 305-306 行打印 `test is successful` 并 `return 0`。这就是 `nginx -t` 的完整行为：**它跑完整个 `ngx_init_cycle`（含打开端口），成功才报 successful**。

#### 4.2.4 代码实践

**实践目标**：完成规格要求的核心任务——在 `ngx_init_cycle` 中标注「解析配置 → 初始化模块 → 打开监听端口」三大阶段的边界函数，并说明哪一步失败会让 `nginx -t` 报错。

**操作步骤**：

1. 打开 [src/core/ngx_cycle.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c)，按下表在源码里标注三个阶段的起止边界：

   | 阶段 | 起始边界函数（行号） | 结束边界 / 下一阶段起点 | 失败时 `-t` 表现 |
   |------|----------------------|--------------------------|------------------|
   | ① 解析配置 | `ngx_conf_parse`（[L286](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L286)） | 打印 "syntax is ok"（L292） | 报语法错，**不会**打印 "syntax is ok" |
   | ② 初始化模块（配置层） | `create_conf` 循环（[L233](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L233)）→ `init_conf` 循环（[L297](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L297)） | `init_conf` 循环结束（L314） | 打印 "syntax is ok"，随后报 init_conf 错误 |
   | ③ 打开监听端口 | `ngx_open_listening_sockets`（[L632](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L632)） | 之后 `ngx_init_modules`（L649） | 打印 "syntax is ok"，随后报 `bind()` 错（如端口被占） |

2. **关键观察**：阶段 ②③ 的失败，`-t` 会**先**在第 292 行打印 "syntax is ok"，**再**在 `failed` 回滚后让 `main()` 打印 "test failed"。也就是说，「syntax is ok」只代表「文本解析通过」，不代表「配置完全可用」。

3. （可选，待本地验证）若手头有可编译的 nginx，构造两种坏配置分别验证：
   - 语法坏配置：把 `events {}` 改成 `events {`（少右花括号）→ `-t` 应**不**出现 "syntax is ok"，直接报错。
   - 端口冲突配置：写两个 server 都 `listen 80;` 已被占用的情况，或故意 `listen 1;`（无权限的特权端口）→ `-t` 会先打印 "syntax is ok"，再报 `bind() failed`。

**需要观察的现象**：阶段 ① 失败与阶段 ②③ 失败，`-t` 的输出差异点在于「是否先打印了 syntax is ok」。

**预期结果**：你能口述「`-t` = 跑完整 `ngx_init_cycle` 且含 `bind`，但 `init_module` 之后立即返回不开 worker」，并指出 `main()` 第 295 行的 `if (cycle == NULL)` 是判断 `-t` 成败的总开关。

> 待本地验证：上述 `-t` 输出顺序基于当前 HEAD 源码静态推断；不同端口/权限环境下 `bind` 是否真的失败需实际运行核对。

#### 4.2.5 小练习与答案

**练习 1**：`ngx_init_cycle` 里有两种失败处理——阶段 0-4 用 `ngx_destroy_cycle_pools(&conf); return NULL;`，阶段 5-7 用 `goto failed;`。为什么不能用同一种？

**参考答案**：阶段 0-4 还没打开任何系统资源（文件 fd、共享内存、监听端口都还没物化），失败时只需销毁内存池即可干净回收。阶段 5-7 已经真正打开了一些资源（第 385 行 `ngx_open_file`、第 493 行 `ngx_shm_alloc`、`ngx_open_listening_sockets` 里的 `socket`），失败时必须先逐个关闭/释放这些已物化的资源，否则会造成 fd 泄漏、共享内存残留、端口占用。`failed` 标签（L833-953）就是这套「先回滚物化资源、再销毁池」的逻辑。

**练习 2**：为什么 `ngx_init_modules`（阶段 7）失败直接 `exit(1)`，而不是 `goto failed` 优雅回滚？

**参考答案**：到阶段 7 时 cycle 已基本装配完成，`ngx_init_modules` 失败属于「模块初始化的致命错误」（如某模块自检不过）。注释明确标 `/* fatal */`。一方面此时回滚代价大、且旧 cycle 可能已经因为 reload 流程而处于半退休状态；另一方面这种错误通常意味着环境或配置有根本性问题，直接退出让 master 进程的调用方（reload 路径里）感知失败、保留旧 cycle 继续服务，比留一个半坏的新 cycle 更安全。

---

### 4.3 模块生命周期回调：create_conf / init_conf / init_module

#### 4.3.1 概念说明

nginx 的「模块回调」分布在**两个不同结构体**上，初学者很容易混淆。本节把它们彻底分清。

**第一类：核心模块的配置回调，挂在 `ngx_core_module_t` 上。** 只有 `type == NGX_CORE_MODULE` 的模块才有这个上下文（`ngx_module_t.ctx` 指向一个 `ngx_core_module_t`）。它有两个方法：

- `create_conf(cycle)`：在配置解析**之前**调用，分配本模块的配置结构体，并把所有标量字段初始化成「未设置」哨兵值（如 `NGX_CONF_UNSET`）。
- `init_conf(cycle, conf)`：在配置解析**之后**调用，做最终校验，并把仍是哨兵值的字段补上默认值。

`ngx_core_module`（核心模块，管理 `worker_processes` 等进程参数）是最典型的核心模块。HTTP 框架模块 `ngx_http_module` 也是核心模块，它的 `create_conf`/`init_conf` 负责建/校 HTTP 三层配置（u6-l1 详述）。

**第二类：所有模块都有的进程级回调，挂在 `ngx_module_t` 上。** 与模块类型无关，每个模块都可定义：

- `init_module(cycle)`：cycle 装配的**最后**（`ngx_init_cycle` 阶段 7）调用一次，用于模块级的全局初始化。
- `init_process(cycle)`：每个 worker 进程启动时调用一次（u4-l1 详述）。
- `exit_process` / `exit_master`：退出时调用。

本讲的焦点是第一类的 `create_conf` / `init_conf`，以及第二类的 `init_module`（因为它也在 `ngx_init_cycle` 里被调）。

#### 4.3.2 核心流程

核心模块配置的三步流水（以 `ngx_core_module` 为例）：

```
1. create_conf（解析前）：
   ccf = ngx_pcalloc(pool, sizeof(ngx_core_conf_t))    # 清零
   ccf->daemon = NGX_CONF_UNSET                        # 标量全部设成哨兵
   ccf->worker_processes = NGX_CONF_UNSET
   ...
   return ccf                                          # 挂到 conf_ctx[index]

2. 解析阶段（u3-l1）：
   "worker_processes 4;" → set_num_slot 把 4 写进 ccf->worker_processes
   （此时若用户没写某指令，对应字段仍是 NGX_CONF_UNSET）

3. init_conf（解析后）：
   ngx_conf_init_value(ccf->worker_processes, 1)       # 若仍是 UNSET 则补默认 1
   ngx_conf_init_value(ccf->daemon, 1)
   ... 校验 pid 路径、生成 oldpid 名字
   return NGX_CONF_OK
```

`ngx_conf_init_value` 宏就是 `if (conf == NGX_CONF_UNSET) { conf = default; }`（见 [ngx_conf_file.h:180-183](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.h#L180-L183)）。这个「哨兵 → 默认值」的替换，正是 u3-l1 里「未设置哨兵值如何在 merge 阶段变成继承上层默认」的机制在核心层的体现（HTTP 层的 `merge_loc_conf` 思路完全一致，u6-l1 详述）。

#### 4.3.3 源码精读

两个结构体——`ngx_core_module_t`（配置回调）与 `ngx_module_t` 的回调字段：

[ngx_core_module_t：核心模块上下文（create_conf/init_conf） — src/core/ngx_module.h:265-269](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.h#L265-L269)

[ngx_module_s：进程级回调 init_module/init_process/exit_* — src/core/ngx_module.h:243-252](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.h#L243-L252)

对照两张表就能记住：`ngx_core_module_t` 只有 `name`/`create_conf`/`init_conf` 三字段，是「配置生命周期」；`ngx_module_t` 上的 `init_module`/`init_process`/`init_thread`/`exit_*` 是「进程生命周期」。两套回调互不替代。

`ngx_core_module` 的实例化——把上下文挂到模块上：

[ngx_core_module_ctx（核心模块上下文） + ngx_core_module（模块本体） — src/core/nginx.c:160-180](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L160-L180)

第 160-164 行 `ngx_core_module_ctx` 把 `create_conf` 绑成 `ngx_core_module_create_conf`、`init_conf` 绑成 `ngx_core_module_init_conf`。第 167-180 行 `ngx_core_module` 是模块本体，第 169 行 `ctx` 字段指向上面那个上下文，第 171 行 `type = NGX_CORE_MODULE`（这正是 `ngx_init_cycle` 阶段 2/4 循环里 `type == NGX_CORE_MODULE` 判断能选中它的原因）。注意第 173-174 行 `init_module`/`init_process` 都是 `NULL`——核心模块不需要进程级初始化。

`create_conf` 实现——建结构体 + 填哨兵：

[ngx_core_module_create_conf：pcalloc + 设哨兵 — src/core/nginx.c:1093-1135](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L1093-L1135)

第 1098 行 `ngx_pcalloc` 把结构体清零，注释（1103-1112 行）列出「靠 pcalloc 已经为 0/NULL 的字段」。第 1114-1126 行把**标量**字段显式设成哨兵（`NGX_CONF_UNSET`、`NGX_CONF_UNSET_MSEC`、`NGX_CONF_UNSET_UINT`）——这些就是 u3-l1 里 slot 函数「`is duplicate` 检查」与「补默认值」所依赖的标记。字符串类字段（`pid` 等）保持 `pcalloc` 的 `NULL`。

`init_conf` 实现——补默认值 + 路径处理：

[ngx_core_module_init_conf：ngx_conf_init_value 补默认 + pid 路径 — src/core/nginx.c:1138-1180](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L1138-L1180)

第 1143-1149 行连续 `ngx_conf_init_value`：`daemon` 默认 1、`master` 默认 1、`worker_processes` 默认 1。所以你在 `nginx.conf` 里不写 `worker_processes`，最终生效值是 1——根源就在这里。第 1167-1179 行处理 `pid` 路径：未设置则用编译期 `NGX_PID_PATH`，再用 `ngx_conf_full_name` 拼成绝对路径，并派生出 `oldpid`（升级时用，u4-l2）。

进程级回调的调用点——`ngx_cycle_modules` 与 `ngx_init_modules`：

[ngx_cycle_modules：把静态模块表拷进 cycle — src/core/ngx_module.c:42-62](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.c#L42-L62)

第 50 行为新 cycle 分配 `ngx_max_module + 1` 个指针的数组，第 56 行 `ngx_memcpy` 把全局静态 `ngx_modules[]`（u1-l2 讲过的那个 `objs/ngx_modules.c` 生成的数组）拷贝进来。从此 `cycle->modules` 与 `ngx_modules` 内容一致但相互独立——reload 时新 cycle 的模块表修改不会污染全局表。

[ngx_init_modules：调每个模块的 init_module — src/core/ngx_module.c:65-79](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.c#L65-L79)

第 70-76 行遍历 `cycle->modules`，对定义了 `init_module` 的模块调用它。这就是 `ngx_init_cycle` 阶段 7 的第 649 行所做的事。注意它与 `create_conf`/`init_conf` 的层级差异：后两者只对 `NGX_CORE_MODULE` 调，而 `init_module` 对**所有**模块（HTTP、EVENT、MAIL、STREAM 都算）调。

#### 4.3.4 代码实践

**实践目标**：用源码追踪回答一个具体问题——「我在 `nginx.conf` 里没写 `worker_processes`，最终 worker 数到底是几？」把「哨兵 → 默认值」的链条走通。

**操作步骤**：

1. 起点：[nginx.c:1098](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L1098) `create_conf` 把 `ccf->worker_processes = NGX_CONF_UNSET`（第 1119 行）。
2. 中间：因为你没写该指令，u3-l1 的 `set_num_slot` 根本没被调用，字段保持 `NGX_CONF_UNSET`。
3. 终点：[nginx.c:1148](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L1148) `init_conf` 里 `ngx_conf_init_value(ccf->worker_processes, 1)`——字段是 `UNSET`，于是赋成 1。
4. 取用：master 进程里 [nginx.c:337](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L337) `ccf = ngx_get_conf(cycle->conf_ctx, ngx_core_module)` 取出这份配置，按 `ccf->worker_processes` fork worker（u4-l1）。

**需要观察的现象**：整条链上，值「1」是在 `init_conf` 里被填入的，而非 `create_conf`，也非解析阶段。

**预期结果**：你能说清「默认值来自 `init_conf` 的 `ngx_conf_init_value` 第二个参数」，并据此推断：若把第 1148 行的 `1` 改成 `4`（**仅作思维实验，本讲义不修改源码**），不写 `worker_processes` 时默认 worker 数就会变成 4。

> 待本地验证：用 `worker_processes` 注释掉后启动 nginx，`ps` 观察 worker 数是否为 1，与上面的推断核对。

#### 4.3.5 小练习与答案

**练习 1**：一个 HTTP 模块（`type == NGX_HTTP_MODULE`）的 `create_loc_conf` 会在 `ngx_init_cycle` 阶段 2 的 `create_conf` 循环里被调用吗？

**参考答案**：不会。阶段 2 的循环（[ngx_cycle.c:233-248](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L233-L248)）只挑 `type == NGX_CORE_MODULE` 的模块。HTTP 模块的 `create_loc_conf`/`create_srv_conf`/`create_main_conf` 是在 `http {}` 块的 set 回调 `ngx_http_block` 内部被循环调用的（解析到 `http {` 时才进入）。所以「HTTP 三层配置」的创建发生在阶段 3（配置解析）内部，而非阶段 2。这也是为什么 HTTP 模块是「框架型核心模块」——`ngx_http_module` 本身是 CORE 模块（参与阶段 2/4），但它内部又自建了一套针对 HTTP 子模块的配置管理。

**练习 2**：`init_module`（进程级，阶段 7）和 `init_conf`（核心模块配置，阶段 4）都会在 `ngx_init_cycle` 里被调，它们的本质区别是什么？

**参考答案**：`init_conf` 是「配置层」回调，只对核心模块调，时机早（阶段 4，端口还没开），目的是校验配置结构体并补默认值，失败走 `return NULL` 优雅回滚。`init_module` 是「模块运行时层」回调，对所有模块调，时机晚（阶段 7，端口已开、资源已物化），目的是做模块级的运行时初始化（如注册 phase handler 之外的内部数据结构），失败走 `exit(1)` 致命退出。一个管「配置对不对」，一个管「模块准备好运行没有」。

---

### 4.4 打开监听端口与新旧 cycle 复用：ngx_open_listening_sockets

#### 4.4.1 概念说明

配置解析阶段，每当遇到 `listen` 指令，HTTP/stream/mail 模块就会往 `cycle->listening` 数组里 push 一个 `ngx_listening_t`（登记「我要监听这个地址」）。但此时**并没有真正调用 `socket/bind/listen`**——只是登记。真正「物化」这些监听端口的，是阶段 6 的 `ngx_open_listening_sockets(cycle)`。

为什么要把「登记」和「物化」分开？两个原因：

1. **校验整体一致性**：等所有 `listen` 都解析完，确认没有冲突（如两个 server 监听同地址但不同选项），再统一打开。
2. **支持 reload 平滑切换**：reload 时新 cycle 的 `listening` 与旧 cycle 的逐一比对，能复用的 fd 直接继承（`remain=1`），只有真正新增/变更的才需要重新 `socket/bind/listen`。这保证了 reload 期间端口始终在监听、不丢连接。

#### 4.4.2 核心流程

`ngx_open_listening_sockets` 的主结构是一个「5 次重试」的外层循环套一个「遍历所有监听端口」的内层循环：

```
ngx_open_listening_sockets(cycle):
    for tries = 5 down to 1:                  # 最多重试 5 次
        failed = 0
        for 每个 ls in cycle->listening:
            if ls.ignore:                     continue
            if ls.fd != -1 且无需 change_protocol:   continue   # 已有 fd（继承来的），跳过
            if ls.inherited:                  continue   # 继承自升级前进程，跳过

            s = ngx_socket(family, type, protocol)        # socket()
            setsockopt(SO_REUSEADDR)                      # 允许地址重用
            flags = ls.flags (NONBLOCK|REUSEPORT...)
            if ls.sndbuf/rcvbuf/keepalive...:  setsockopt(...)
            if bind(s, sockaddr) 失败:  failed++; close(s); continue   # 端口占用先记下
            if ls.type != UDP:  listen(s, ls.backlog)                 # TCP 才 listen
            ls.fd = s; ls.listen = 1; ls.open = 1

        if !failed:  break                   # 全部成功，跳出重试
        ngx_msleep(500ms)                    # 否则睡半秒再试（等旧端口释放）

    if failed:  return NGX_ERROR
```

重试机制（`tries = 5`、失败睡 500ms）是为了应对 reload 场景：旧 worker 还在优雅退出、端口还没完全释放，新 cycle 的 `bind` 可能暂时失败，睡一下再试就能成功。`SO_REUSEADDR` 则允许绑定处于 `TIME_WAIT` 状态的地址。

新旧 cycle 的比对复用逻辑发生在 `ngx_open_listening_sockets` **之前**（[ngx_cycle.c:513-630](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L513-L630)）：对每个新 `listening` 项，在旧 `old_cycle->listening` 里找「同地址同类型」的，把旧 fd 直接赋给新项（`nls[n].fd = ls[i].fd`），并标记旧的 `remain = 1`（保留）。这样到 `ngx_open_listening_sockets` 时，这些项 `ls.fd != -1`，第 500 行直接 `continue` 跳过，根本不重新 `socket/bind`。

#### 4.4.3 源码精读

外层重试循环与「已有 fd 就跳过」：

[ngx_open_listening_sockets：5 次重试 + 跳过已继承 fd — src/core/ngx_connection.c:425-514](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_connection.c#L425-L514)

第 444 行 `for (tries = 5; tries; tries--)` 是重试外循环。第 452 行 `if (ls[i].ignore) continue` 跳过被标记忽略的端口。第 500 行 `if (ls[i].fd != (ngx_socket_t) -1 && !ls[i].change_protocol) continue` 是「复用 fd」的核心——reload 时这些 fd 已被前面的比对逻辑（ngx_cycle.c:539）从旧 cycle 挪过来，这里直接跳过。第 504 行 `if (ls[i].inherited) continue` 跳过二进制升级继承来的 fd。第 513 行才真正 `ngx_socket()` 创建新套接字。

`SO_REUSEADDR` 与 bind：

[setsockopt SO_REUSEADDR + bind — src/core/ngx_connection.c:522-620](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_connection.c#L522-L620)

第 522 行 `if (ls[i].type != SOCK_DGRAM || !ngx_test_config)`——注意对 UDP 且 `-t` 模式不设 `SO_REUSEADDR`（避免测试时干扰）。第 524 行设 `SO_REUSEADDR`，第 568 行起设各种自定义 socket 选项（`sndbuf`/`rcvbuf`/`keepalive`/`reuseport`/`fastopen` 等，来自 `listen` 指令的参数）。`bind` 失败不立即返回，而是 `failed++` 记下、`close(s)` 后 `continue`，让本轮把其他端口先开完，再由外层重试。

新旧 listening 比对（复用入口，在 cycle.c 里）：

[reload 时把旧 fd 赋给新 listening 并标 remain — src/core/ngx_cycle.c:513-612](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L513-L612)

第 516 行先把所有旧端口的 `remain = 0`。第 520 行遍历新 `listening`，第 522 行内层遍历旧 `listening`，第 535 行 `ngx_cmp_sockaddr` 比较新旧地址是否相同。相同则第 539 行 `nls[n].fd = ls[i].fd`（fd 移交）、第 547 行 `ls[i].remain = 1`（旧端口标记保留）。之后第 600 行 `if (nls[n].fd == -1)` 表示这是全新地址（旧 cycle 没有），需要 `ngx_open_listening_sockets` 真正创建。reload 结束后，第 721-751 行关闭旧 cycle 里 `remain == 0` 的端口（新配置不再需要的）。

提交后的善后——关闭旧 cycle 不再需要的端口：

[关闭旧 cycle 中 remain==0 的监听端口 — src/core/ngx_cycle.c:719-751](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L719-L751)

第 724 行 `if (ls[i].remain || ls[i].fd == -1) continue`——`remain==1` 的已被新 cycle 接管（不能关），`fd==-1` 的本就没开。其余的（旧配置有、新配置没有的）第 728 行 `ngx_close_socket` 关掉。这就是 reload 后「删掉某个 `listen` 端口就真的不再监听」的实现。

#### 4.4.4 代码实践

**实践目标**：跟踪一个 reload 场景，理解「监听端口如何在旧新 cycle 间移交而不丢连接」。

**操作步骤**（源码阅读型，无需真实 reload）：

1. 假设当前 nginx 监听 `80` 端口，你执行 `nginx -s reload`。master 进程进入 `ngx_init_cycle(当前cycle)`。
2. 在 [ngx_cycle.c:513-612](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L513-L612) 里定位：新 cycle 解析出的 `listen 80` 项，在内层循环找到旧 cycle 同地址的项，第 539 行 `nls[n].fd = ls[i].fd` 把旧的 fd（假设是 fd 6）挪给新 cycle，第 547 行 `ls[i].remain = 1`。
3. 进入 [ngx_open_listening_sockets](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_connection.c#L425)，第 500 行发现 `ls[i].fd == 6 != -1`，直接 `continue`——**全程没有重新 `socket/bind/listen`，80 端口的 fd 自始至终是同一个，正在被处理的连接不受影响**。
4. 新 cycle 提交后，第 724 行检查旧 cycle 的 80 端口 `remain==1`，跳过不关。

**需要观察的现象**：整个 reload 过程中，监听 fd 从未关闭、从未重新绑定，只是「所有权」从旧 cycle 转到新 cycle。

**预期结果**：你能解释「为什么 `nginx -s reload` 不会导致正在传输的请求被断开」——因为监听 socket 的 fd 被直接继承，内核侧的 listen backlog 和已 accept 的连接完全不受 cycle 切换影响。

> 待本地验证：在有可运行 nginx 的环境，`nginx -s reload` 期间持续 `curl` 一个大文件下载，观察连接是否中断；并用 `ss -ltnp` 确认 reload 前后监听 80 的进程 pid 变了但 fd 没断。

#### 4.4.5 小练习与答案

**练习 1**：`ngx_open_listening_sockets` 为什么要有 `for (tries = 5; ...)` 这个重试循环？什么情况下第一次 `bind` 会失败但重试能成功？

**参考答案**：reload 场景下，旧 worker 可能仍在优雅退出（处理完已有请求才关连接），此时旧 cycle 持有的某些监听端口（尤其是配置变更、不能复用 fd 的那些）刚被关闭、可能处于 `TIME_WAIT` 或尚未完全释放，新 cycle 的 `bind` 会暂时失败。睡 500ms 再试，等旧端口释放或 `TIME_WAIT` 过去，就能成功。配合 `SO_REUSEADDR`，多数情况下第一次就能成功，重试是兜底。首次冷启动则几乎不会触发重试。

**练习 2**：如果你在 `nginx.conf` 里删掉了一个 `listen 8080;` 然后 reload，8080 端口会被关闭吗？走的是哪段代码？

**参考答案**：会。新 cycle 的 `listening` 里不再有 8080，所以比对逻辑（ngx_cycle.c:520-612）不会把 8080 的 fd 标记 `remain`（它仍是初始的 `remain=0`，第 516 行设置）。新 cycle 提交后，第 719-751 行的清理循环遍历旧 cycle 的 listening，发现 8080 的 `remain==0` 且 `fd != -1`，第 728 行 `ngx_close_socket(ls[i].fd)` 关闭它。此后 nginx 不再监听 8080。

---

## 5. 综合实践

把四个最小模块串起来，完成规格要求的核心任务，并加上一条 reload 追踪。

### 5.1 任务

下面是一个贯穿本讲的综合练习。准备一张纸，画出 `nginx -t`（测试配置）从命令行到退出的完整数据流，标注每一步在源码的哪个函数、哪一行：

1. **命令行解析**：`main()` 里 `ngx_get_options` 把 `-t` 翻成 `ngx_test_config = 1`（[nginx.c:836](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L836)，u1-l4 讲过）。
2. **进入装配**：[nginx.c:293](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L293) `cycle = ngx_init_cycle(&init_cycle)`。
3. **在 `ngx_init_cycle` 内部**，按本讲 4.2.2 的七阶段，标出每阶段的边界函数与对应行号（create_conf 循环 L233、ngx_conf_parse L286、init_conf 循环 L297、ngx_open_listening_sockets L632、ngx_init_modules L649）。
4. **`-t` 特殊路径**：标出三个受 `ngx_test_config` 影响的点——「syntax is ok」打印（L292）、pid 文件创建分支（L322）、跳过 `ngx_configure_listening_sockets`（L636）。
5. **返回 `main()`**：[nginx.c:294-327](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L294-L327) 根据 `cycle` 是否为 NULL 打印 "test failed" 或 "test is successful"，然后 `return 0` 或 `return 1`。

### 5.2 进阶问题

完成上图后，回答：

- **Q1**：`nginx -t` 报 `bind() to 0.0.0.0:80 failed (98: Address already in use)`，但前面却打印了 `syntax is ok`。为什么「语法 ok」却仍然失败？（提示：回顾 4.2.4 的阶段表——语法检查只是阶段 ①，bind 在阶段 ③。）
- **Q2**：`nginx -t` 与真实启动，在 `ngx_init_cycle` 内部的执行路径**唯一差别**在哪几行？（提示：L292 的打印、L322 的 pid 分支、L636 的 `configure_listening_sockets` 跳过；其余完全一样——`-t` 也会真正 `bind` 端口、也会调 `init_module`。）
- **Q3**：reload 场景下，假设配置里新增了一个 `listen 8080;`（旧配置没有）。画出 8080 的生命周期：它在哪个函数被登记进 `cycle->listening`？在 [ngx_cycle.c:513-612](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L513-L612) 的比对中命运如何（能否找到旧项）？最终由谁真正 `socket/bind/listen`？

### 5.3 参考答案要点

**Q1**：`syntax is ok`（L292）只代表阶段 ① 文本解析通过。`bind` 失败发生在阶段 ③ 的 `ngx_open_listening_sockets`（L632），此时已晚于 L292 的打印。`bind` 失败走 `goto failed`（L633），最终让 `ngx_init_cycle` 返回 NULL，`main()` 第 295-298 行据此打印 "test failed"。所以输出里两行都出现并不矛盾。

**Q2**：核心差别就三处。其余路径（含真正的 `bind`、`init_module`）`-t` 与真实启动完全一致——这也是为什么 `nginx -t` 能检测出端口冲突、权限不足等「非语法」问题。

**Q3**：8080 在 HTTP 模块解析 `listen 8080;` 指令时被 `ngx_http_add_listening`（u6-l1 详述）登记进 `cycle->listening`。在比对循环（[ngx_cycle.c:522](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L522)）里，旧 cycle 没有 8080，所以找不到匹配项，`nls[n].fd` 保持 -1（第 600 行进入该分支）。最终由 `ngx_open_listening_sockets`（L632，内部 [ngx_connection.c:513](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_connection.c#L513) `ngx_socket`、后续 `bind`/`listen`）真正创建。

> 待本地验证：Q1 的 `bind` 失败输出、Q3 的 reload 新增端口行为，建议在可运行环境用 `nginx -t` 与 `nginx -s reload` 实测核对。

---

## 6. 本讲小结

- `ngx_cycle_t` 是 nginx 的「全局上下文容器」，把进程级共享状态（配置数组 `conf_ctx`、模块表 `modules`、资源清单 `listening`/`paths`/`open_files`/`shared_memory`、连接运行时 `connections` 等）收拢进一个结构体，全部挂在专属内存池 `pool` 上；`ngx_cycle` 全局指针指向当前生效的 cycle，reload 时构造全新 cycle 再切换。
- `ngx_init_cycle` 是一条七阶段线性装配线：建池与容器（0）→ 装载模块 `ngx_cycle_modules`（1）→ 核心模块 `create_conf`（2）→ 解析配置 `ngx_conf_parse`（3）→ 核心模块 `init_conf`（4）→ 物化文件/共享内存（5）→ 打开监听端口 `ngx_open_listening_sockets`（6）→ `ngx_init_modules` 提交（7）；阶段 0-4 失败直接销毁池返回，阶段 5-7 失败 `goto failed` 先回滚物化资源。
- 核心模块配置的三步流水是「`create_conf` 建空壳填哨兵 → 解析阶段 slot 函数填值 → `init_conf` 用 `ngx_conf_init_value` 把仍是哨兵的字段补默认」，这就是 u3-l1 里「未设置哨兵 → 继承默认」机制的真正落地；`worker_processes` 默认 1 即来自 `ngx_core_module_init_conf`。
- 模块回调分两套，别混淆：`ngx_core_module_t.create_conf/init_conf` 是「配置层」回调，只对核心模块、在阶段 2/4 调；`ngx_module_t.init_module/init_process` 是「进程运行时层」回调，对所有模块，`init_module` 在阶段 7 调、`init_process` 在 worker 启动时调（下一单元）。
- `nginx -t` 的本质是跑完**整个** `ngx_init_cycle`（含真正 `bind` 端口、含 `init_module`），仅跳过 `ngx_configure_listening_sockets` 的 setsockopt 与后续 fork worker；「syntax is ok」只代表阶段 ① 通过，bind/init_module 失败仍会让 `-t` 报 "test failed"。
- reload 与升级的「不丢连接」根基是资源复用：新 cycle 在 [ngx_cycle.c:513-612](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L513-L612) 把旧 cycle 同地址的监听 fd 直接继承（`remain=1`），`ngx_open_listening_sockets` 见到 `fd != -1` 即跳过，监听 socket 全程不关闭、不重新绑定；共享内存同理按「同名同 size 同 tag」复用旧地址。

---

## 7. 下一步学习建议

- **u3-l3 模块系统与动态模块加载**：本讲的 `ngx_cycle_modules`（拷贝静态 `ngx_modules[]`）、`ngx_init_modules`（调 `init_module`）、`conf_ctx[module.index]` 里的 `index` 从哪来？下一讲讲 `ngx_module_t` 结构、`ngx_preinit_modules`/`ngx_count_modules` 如何分配 `index`/`ctx_index`，以及 `load_module` 指令如何 `dlopen` 动态 `.so` 并经 `ngx_add_module` 插入 `cycle->modules`。
- **u3-l4 指令类型与地址解析**：本讲提到 `listen` 指令会往 `cycle->listening` 里登记端口，下一讲讲 `ngx_inet` 如何解析 `listen` 后面的地址/CIDR，以及 `ngx_conf_set_*_slot` 的完整家族。
- **第四单元 进程模型**：本讲的 `init_module`（阶段 7）之后，cycle 就装配完成了。下一单元讲 master 如何基于 `ngx_core_conf_t` 里的 `worker_processes` fork worker、worker 如何调 `init_process` 并进入事件循环——届时你会看到 `cycle->connections`/`read_events`/`write_events` 这些本讲只列了字段、还没被填充的连接运行时是如何在 worker 启动时初始化的。
- **延伸阅读**：想看共享内存区的完整生命周期，可阅读 [ngx_shared_memory_add — src/core/ngx_cycle.c:1305-1376](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L1305-L1376)（登记）配合 `ngx_init_zone_pool`（初始化 slab 分配器），它是 u4-l3（共享内存与 slab）的前置。
