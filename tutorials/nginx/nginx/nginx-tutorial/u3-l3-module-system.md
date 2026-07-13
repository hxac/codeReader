# 模块系统与动态模块加载

## 1. 本讲目标

nginx 的「一切皆模块」并不是一句口号，而是一套用 C 结构体 + 索引数组实现的严格工程机制。本讲学完后，你应该能够：

- 说清 `ngx_module_t` 这个「万能模块描述符」有哪些字段，以及 `index` 与 `ctx_index` 这两套索引各自的用途。
- 说清静态模块从 `ngx_modules[]` 表到 `init_module` 回调被调用的完整注册链路。
- 理解 `ngx_preinit_modules`、`ngx_count_modules`、`ngx_load_module` 三个关键函数分别在什么时机、为谁分配索引。
- 理解 `load_module` 指令如何用 `dlopen` 运行时加载一个 `.so`，以及它为什么要做版本号与「二进制兼容签名」双重校验。

本讲是 u3-l2「cycle 生命周期」的直接续篇：u3-l2 讲的是 `ngx_cycle_t` 这个容器如何被装配，本讲专门放大其中「模块」这一维度。

## 2. 前置知识

阅读本讲前，你需要已经掌握以下概念（在前序讲义中已建立）：

- **模块（module）**：nginx 能力的基本单元，分为静态模块（编译进二进制）和动态模块（独立 `.so`，运行时加载）。见 u1-l1。
- **指令（directive）**：配置文件里由模块提供的参数，如 `worker_processes`、`load_module`。见 u3-l1。
- **`ngx_cycle_t` 与 `ngx_init_cycle`**：全局上下文容器及其七阶段装配线；`cycle->modules` 与 `cycle->conf_ctx` 都是本讲要细讲的对象。见 u3-l2。
- **两套模块回调的区分**：`ngx_core_module_t` 的 `create_conf`/`init_conf` 是「配置层回调」，`ngx_module_t` 的 `init_module`/`init_process` 是「进程运行时层回调」。见 u3-l2。

本讲会反复用到 `offsetof` 反射式赋值（u3-l1）与内存池（u2-l1）的概念，但不会重复讲解。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/core/ngx_module.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.h) | 定义 `ngx_module_t` 结构体、`NGX_MODULE_V1` 初始化宏、二进制兼容签名 `NGX_MODULE_SIGNATURE`、以及模块系统函数原型。 |
| [src/core/ngx_module.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.c) | 实现 `ngx_preinit_modules`、`ngx_cycle_modules`、`ngx_init_modules`、`ngx_count_modules`、`ngx_add_module` 等。 |
| [src/core/nginx.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c) | `main()` 中调用 `ngx_preinit_modules`；定义 `load_module` 指令并实现 `ngx_load_module`（动态加载入口）。 |
| [src/core/ngx_cycle.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c) | `ngx_init_cycle` 末尾调用 `ngx_init_modules`，触发每个模块的 `init_module` 回调。 |
| [src/os/unix/ngx_process_cycle.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c) | worker 进程启动时遍历 `cycle->modules` 调用 `init_process` 回调。 |
| [auto/modules](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/modules) | 构建期脚本，把静态模块「拍平」生成 `objs/ngx_modules.c`（含 `ngx_modules[]` 与 `ngx_module_names[]`）。 |
| [src/http/modules/ngx_http_static_module.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_static_module.c) | 一个典型的静态 HTTP 模块定义，用作本讲的「样本模块」。 |

## 4. 核心概念与源码讲解

### 4.1 模块结构体 ngx_module_t 与双索引

#### 4.1.1 概念说明

nginx 里有五类模块：核心（CORE）、事件（EVENT）、HTTP、邮件（MAIL）、流（STREAM）。无论哪一类，在 C 层面都是**同一个结构体 `ngx_module_t`**，只是 `type` 字段取值不同。这是一个典型的「标签联合（tagged union）」设计：用同一个外壳装下所有模块，靠 `type` 区分种类，靠 `ctx`（context，上下文指针）携带该种类专属的回调表。

`ngx_module_t` 里最容易被初学者混淆的是**两套索引**：

- `index`：**全局索引**。在整个进程的所有模块（不分种类）中唯一，从 0 开始递增。用来在 `cycle->modules[index]` 这个「全局模块表」里定位自己，也用来在 `cycle->conf_ctx[index]` 里存放核心模块的配置。
- `ctx_index`：**类内索引**。只在「同 type 的模块」之间唯一，从 0 开始递增。HTTP 模块用它去 `main_conf[ctx_index]`、`srv_conf[ctx_index]`、`loc_conf[ctx_index]` 里取自己的三层配置。

可以这样记：`index` 是「全校学号」，`ctx_index` 是「班内序号」。HTTP 那一堆配置数组是按「班内序号」开的，所以必须用 `ctx_index` 去 index。

#### 4.1.2 核心流程

一个模块从源码到运行，其结构体字段被填写的顺序大致是：

```text
编译期  : auto/modules 把模块符号收集进 ngx_modules[]
         NGX_MODULE_V1 宏把 index/ctx_index 预填为 NGX_MODULE_UNSET_INDEX
启动期  : ngx_preinit_modules  → 给每个静态模块赋 index（0,1,2,...）
解析期  : 进入 http{} 块 → ngx_count_modules(type=HTTP) → 给每个 HTTP 模块赋 ctx_index
运行期  : init_module / init_process 回调被调用
```

关键点：`index` 在进程一启动就定好（且基本不变），`ctx_index` 要等到解析到对应配置块（`http{}`/`events{}`/`stream{}`/`mail{}`）时才分配——因为只有那时才知道这一类模块总共有多少个、数组该开多大。

#### 4.1.3 源码精读

先看结构体本体（注意 `ctx_index` 与 `index` 排在最前面，因为它们被高频访问）：

[src/core/ngx_module.h:227-262](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.h#L227-L262) 定义了 `struct ngx_module_s`，其中：

- `ctx_index` / `index`（第 228–229 行）：本讲的主角，两套索引。
- `name`（第 231 行）：模块名字符串，如 `"ngx_http_static_module"`，动态加载时用于查重与排序。
- `version` / `signature`（第 236–237 行）：动态模块的版本与二进制兼容签名（见 4.4）。
- `ctx`（第 239 行）：void 指针，指向该类模块的「专属回调表」。对核心模块指向 `ngx_core_module_t`，对 HTTP 模块指向 `ngx_http_module_t`。
- `commands`（第 240 行）：该模块提供的指令表 `ngx_command_t[]`。
- `type`（第 241 行）：模块种类标签，取值如 `NGX_CORE_MODULE`、`NGX_HTTP_MODULE`。
- 一组函数指针回调（第 243–252 行）：`init_master`、`init_module`、`init_process`、`init_thread`、`exit_thread`、`exit_process`、`exit_master`。

`type` 标签本身是四字符码（FourCC）转成的 32 位整数，便于阅读：

[src/core/ngx_conf_file.h:70-71](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.h#L70-L71) 给出 `NGX_CORE_MODULE = 0x45524F43`（即 `"CORE"`）；其余四类散落在各自的头里，例如 [src/http/ngx_http_config.h:39](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_config.h#L39) 的 `NGX_HTTP_MODULE = 0x50545448`（即 `"HTTP"`）。

再看模块如何「声明」。每个 `.c` 文件用 `NGX_MODULE_V1` 宏把前几个字段填上默认值：

[src/core/ngx_module.h:220-222](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.h#L220-L222) 的 `NGX_MODULE_V1` 宏，把 `ctx_index` 与 `index` 都预填成 `NGX_MODULE_UNSET_INDEX`（即 `(ngx_uint_t) -1`，定义在 [src/core/ngx_module.h:18](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.h#L18)），同时写入当前 `nginx_version` 与 `NGX_MODULE_SIGNATURE`。这个「-1」就是「索引待分配」的哨兵，`ngx_count_modules` 会靠它识别「还没分配 ctx_index 的模块」。

一个完整的静态模块定义样例（核心模块 `ngx_core_module`）：

[src/core/nginx.c:167-180](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L167-L180) 中，`NGX_MODULE_V1` 占位前两个字段，随后依次填 `ctx = &ngx_core_module_ctx`、`commands = ngx_core_commands`、`type = NGX_CORE_MODULE`，最后是一串回调指针（这里全是 `NULL`）和 `NGX_MODULE_V1_PADDING`（8 个 spare_hook 占位，给未来扩展留空间）。

HTTP 模块的写法完全同构，只是 `type` 换成 `NGX_HTTP_MODULE`、`ctx` 指向 `ngx_http_module_t`：

[src/http/modules/ngx_http_static_module.c:32-45](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_static_module.c#L32-L45) 定义了 `ngx_http_static_module`。这就是本讲「综合实践」要追踪的目标模块。

#### 4.1.4 代码实践

**实践目标**：用一个真实存在的静态模块，看清「同一个 `ngx_module_t` 外壳 + 不同 `type`/`ctx`」的设计。

**操作步骤**：

1. 打开 [src/core/nginx.c:167-180](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L167-L180)（核心模块）。
2. 打开 [src/http/modules/ngx_http_static_module.c:32-45](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_static_module.c#L32-L45)（HTTP 模块）。
3. 逐字段对照两张表，找出 `ctx` 与 `type` 的差异。

**需要观察的现象**：两个模块的结构体字段顺序、数量完全一致；差异只在 `ctx`（`ngx_core_module_t` vs `ngx_http_module_t`）和 `type`（`NGX_CORE_MODULE` vs `NGX_HTTP_MODULE`）。

**预期结果**：你会确认「nginx 用一个统一结构体承载所有模块」这一论断，这解释了为什么 `cycle->modules[]` 能用一个 `ngx_module_t *` 数组同时装下五类模块。

#### 4.1.5 小练习与答案

**练习 1**：`index` 和 `ctx_index` 分别用来索引哪两个数组？
**答案**：`index` 索引全局模块表 `cycle->modules[index]`（以及核心模块配置 `cycle->conf_ctx[index]`）；`ctx_index` 索引该类模块的专属配置数组，例如 HTTP 的 `main_conf[ctx_index]` / `srv_conf[ctx_index]` / `loc_conf[ctx_index]`。

**练习 2**：为什么 `NGX_MODULE_V1` 要把两个索引都预填成 `-1`？
**答案**：`-1`（`NGX_MODULE_UNSET_INDEX`）表示「尚未分配」。后续 `ngx_preinit_modules` 和 `ngx_count_modules` 靠判断 `ctx_index == NGX_MODULE_UNSET_INDEX` 来区分「需要分配索引的新模块」与「已经分配过的老模块」（reload 场景下需要保留旧 ctx_index）。

### 4.2 静态模块表与索引空间：ngx_preinit_modules 与 ngx_count_modules

#### 4.2.1 概念说明

静态模块的「登记表」并不是手写的，而是**构建期由 shell 脚本生成**的。`auto/modules` 在 `./configure` 阶段把所有要编进来的模块符号拼成两个数组，写进 `objs/ngx_modules.c`：

```c
ngx_module_t *ngx_modules[] = {
    &ngx_core_module,
    &ngx_errlog_module,
    /* ... 一长串 ... */
    &ngx_http_static_module,
    NULL              /* 哨兵结尾 */
};

char *ngx_module_names[] = {
    "ngx_core_module",
    "ngx_errlog_module",
    /* ... 同序 ... */
    NULL
};
```

这两个全局数组就是「静态模块表」。运行时有两个函数围绕它工作：

- **`ngx_preinit_modules`**：进程启动极早期调用一次，给静态模块分配**全局 `index`**，并预留动态模块的索引空间。
- **`ngx_count_modules`**：解析到某个配置块（如 `http{}`）时调用，统计该类模块数量、分配**类内 `ctx_index`**，返回值用来给配置数组开空间。

#### 4.2.2 核心流程

```text
main()
  └─ ngx_preinit_modules()                 # 给静态模块赋 index = 0..N-1
       └─ ngx_max_module = N + 128         # 预留 128 个动态模块槽位
  └─ ngx_init_cycle()
       └─ ngx_cycle_modules()              # 把静态 ngx_modules[] 拷进 cycle->modules
       └─ ngx_conf_parse()
            ├─ 解析到 events{} → ngx_count_modules(EVENT) → ngx_event_max_module
            └─ 解析到 http{}    → ngx_count_modules(HTTP)  → ngx_http_max_module
                                   └─ 给每个 HTTP 模块赋 ctx_index
                                   └─ main_conf/srv_conf/loc_conf 数组按此大小开辟
```

注意 `ngx_max_module` 的含义：它**不是当前模块数，而是「索引空间总容量」**——静态模块数加上 128 个预留给动态模块的空位。`cycle->modules` 数组就是按这个容量分配的，这样运行时 `load_module` 把动态模块塞进来时不会越界。

#### 4.2.3 源码精读

先看构建期生成器，理解 `ngx_modules[]` 从哪来：

[auto/modules:1568-1590](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/modules#L1568-L1590) 用 `echo` 把每个模块符号 `&$mod,` 追加进 `ngx_modules[]`，并以 `NULL` 收尾；`ngx_module_names[]` 同序生成名字数组。这两个数组在 [src/core/ngx_module.h:282-285](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.h#L282-L285) 以 `extern` 声明，供 C 代码使用。

接着是启动期的索引分配：

[src/core/ngx_module.c:25-39](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.c#L25-L39) 的 `ngx_preinit_modules`：遍历 `ngx_modules[]` 直到 `NULL`，给每个模块 `index = i`（即数组下标），并把 `ngx_module_names[i]` 赋给 `name`；循环结束后 `ngx_max_module = ngx_modules_n + NGX_MAX_DYNAMIC_MODULES`（`NGX_MAX_DYNAMIC_MODULES` 定义为 128，见 [src/core/ngx_module.c:13](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.c#L13)）。这一步在 `main()` 中很早执行：[src/core/nginx.c:289-291](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L289-L291)。

注意它**只赋 `index`，不碰 `ctx_index`**——`ctx_index` 此时仍是 `-1`。

每个 cycle 会复制一份独立的模块表，避免 reload 时改坏全局表：

[src/core/ngx_module.c:42-62](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.c#L42-L62) 的 `ngx_cycle_modules`：在 cycle 的内存池上按 `ngx_max_module + 1` 大小分配 `cycle->modules`，然后把全局 `ngx_modules[]` 的前 `ngx_modules_n` 项 `memcpy` 过去。这就是动态模块能被「加进当前 cycle」而不污染全局表的原因。

再看类内索引分配：

[src/core/ngx_module.c:82-153](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.c#L82-L153) 的 `ngx_count_modules(cycle, type)`：

- 遍历 `cycle->modules`，跳过 `type` 不符的模块（第 96–98 行）。
- 对 `ctx_index` 仍是 `NGX_MODULE_UNSET_INDEX` 的模块，调用 `ngx_module_ctx_index` 找一个该类型内未占用的最小值赋上（第 117 行）；对已经赋过值的（reload 场景），保留旧值（第 100–113 行）。
- 末尾还会扫一遍 `old_cycle->modules`（第 133–146 行），确保返回的 `max` 足够大——这是为了 reload 回滚时不至于让全局变量（如 `ngx_http_max_module`）变小而越界，注释在第 126–131 行解释得很清楚。
- 设置 `cycle->modules_used = 1`（第 150 行）「锁门」：此后不再允许加载新模块（见 4.4）。
- 返回 `max + 1`，即该类型模块的总数。

谁调用它？每个协议块入口各调一次：

[src/http/ngx_http.c:150](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L150) 在 `ngx_http_block` 里 `ngx_http_max_module = ngx_count_modules(cf->cycle, NGX_HTTP_MODULE)`；紧接着 [src/http/ngx_http.c:155-156](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L155-L156) 就用这个数给 `main_conf` 数组 `ngx_pcalloc`。事件、stream、mail 同理（`ngx_event.c:1001`、`ngx_stream.c:117`、`ngx_mail.c:94`）。

> 小结一条索引分配规则：**`index` 全局唯一、启动期定；`ctx_index` 类内唯一、解析期定**。

#### 4.2.4 代码实践

**实践目标**：亲眼看到构建期生成的 `ngx_modules[]`，并验证 `index` 与数组下标一一对应。

**操作步骤**：

1. 在源码根目录执行 `./auto/configure`（或 `auto/configure --with-http_v2_module`），完成配置。
2. 打开生成的 `objs/ngx_modules.c`（这个文件由 `auto/modules` 生成，本仓库当前未构建所以不存在，配置后才会出现）。
3. 找到 `ngx_modules[]`，数一共有多少项；再在其中定位 `&ngx_http_static_module` 的下标 `i`。
4. 对照 [src/core/ngx_module.c:25-39](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.c#L25-L39)，确认它的 `index` 就等于这个下标。

**需要观察的现象**：`ngx_modules[]` 是一个以 `NULL` 结尾的指针数组；`ngx_http_static_module` 的 `index` 等于它在数组中的位置。

**预期结果**：你会看到 `ngx_max_module = (数组长度) + 128`（待本地验证：精确数值取决于 configure 选项）。这印证了「静态模块 index = 数组下标」「动态模块 index 从 N 开始往后排」。

> 如果无法在本地构建，可改为纯源码阅读：阅读 [auto/modules:1568-1590](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/modules#L1568-L1590)，在脑中模拟 `for mod in $modules` 循环，写出它生成的 `ngx_modules[]` 模样。

#### 4.2.5 小练习与答案

**练习 1**：`ngx_max_module` 为什么要在静态模块数基础上加 128？
**答案**：给动态模块预留索引空间。`cycle->modules` 数组按 `ngx_max_module + 1` 分配（见 `ngx_cycle_modules`），这样运行时 `load_module` 加载的动态模块可以从 `index = N` 开始往后放，不必重新分配数组。

**练习 2**：`ngx_count_modules` 末尾为什么要扫一遍 `old_cycle->modules`？
**答案**：reload 时构造的是全新 cycle，但若失败要回滚到旧 cycle。某些 `*_max_module`（如 `ngx_http_max_module`）是全局变量，如果新 cycle 算出的值比旧的小并被写回全局，旧 cycle 的配置数组按旧的大尺寸访问就会越界。所以取「新+旧」两者中的较大值兜底。

### 4.3 模块生命周期回调与 ngx_init_modules

#### 4.3.1 概念说明

`ngx_module_t` 里有七个回调钩子，按调用时机分两组：

| 回调 | 调用时机 | 调用频率 | 典型用途 |
| --- | --- | --- | --- |
| `init_module` | `ngx_init_cycle` 末尾 | 每次构造/重载 cycle 一次 | 初始化模块全局状态（如注册 phase handler） |
| `init_process` | worker 进程启动 | 每个 worker 一次 | 初始化进程私有状态（如打开共享资源） |
| `init_thread` | 线程启动（少用） | 每线程一次 | 线程私有初始化 |
| `exit_thread` / `exit_process` / `exit_master` | 对应退出时 | — | 资源清理 |
| `init_master` | 历史遗留，实际不调用 | — | 未使用 |

最常用的是 `init_module` 和 `init_process`。务必把它们和 u3-l2 讲过的「配置层回调」`create_conf`/`init_conf` 区分开：

- `create_conf` / `init_conf`：**配置层**，仅核心模块有，在配置解析阶段调用，用于建立/填充配置结构体。
- `init_module` / `init_process`：**运行时层**，所有模块都可有，在配置解析**完成之后**调用，用于把模块「接入」运行时（注册处理函数、初始化连接池等）。

#### 4.3.2 核心流程

```text
ngx_init_cycle()
  ├─ ... 解析配置、init_conf ...
  └─ ngx_init_modules(cycle)          # 遍历 cycle->modules，逐个调 init_module
        └─ module->init_module(cycle) # 失败则 return NGX_ERROR

# 之后进入进程循环
ngx_worker_process_cycle()
  └─ 启动时遍历 cycle->modules，逐个调 init_process
        └─ module->init_process(cycle)
```

`init_module` 在配置通过校验、所有模块都已就位后统一触发；`init_process` 在 worker 进程真正开始干活前触发，因此每个 worker 各调一遍。

#### 4.3.3 源码精读

`init_module` 的触发器：

[src/core/ngx_module.c:65-79](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.c#L65-L79) 的 `ngx_init_modules`：遍历 `cycle->modules`，对每个非空的 `init_module` 回调，调用之；任一返回非 `NGX_OK` 即整体返回 `NGX_ERROR`。注意它遍历的是 `cycle->modules`（当前 cycle 的拷贝），所以动态加载的模块也会被一并初始化。

这个函数在 cycle 装配线的最后一环被调：

[src/core/ngx_cycle.c:649-652](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L649-L652) 在 `ngx_init_cycle` 中 `if (ngx_init_modules(cycle) != NGX_OK) { exit(1); }`。这正是 u3-l2 所说的「阶段 7：`ngx_init_modules` 调 `init_module` 提交」。

`init_process` 的触发器在 worker 进程启动流程里：

[src/os/unix/ngx_process_cycle.c:288-295](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L288-L295) 遍历 `cycle->modules`，对每个 `init_process` 回调逐个调用，失败则 `exit(2)`。这在 `ngx_worker_process_cycle` 进入事件主循环 `for ( ;; )` 之前。

把这两处和 [src/core/ngx_module.h:243-252](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.h#L243-L252) 的回调字段声明对照看，就能把「字段定义 → 触发点」整条链路串起来。

> 一个易错点：`init_module` 是「每个 cycle 一次」，而 reload 会构造新 cycle，所以 reload 也会重新调用所有模块的 `init_module`——这正是很多模块（如 `ngx_http_static_init` 注册 content handler）能在 reload 后生效的原因。

#### 4.3.4 代码实践

**实践目标**：确认 `init_module` 在 cycle 装配线中的确切位置，以及它与配置层回调的先后关系。

**操作步骤**：

1. 在 [src/core/ngx_cycle.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c) 的 `ngx_init_cycle` 中定位三处调用：核心模块的 `create_conf`（阶段 2）、`ngx_conf_parse`（阶段 3）、`ngx_init_modules`（阶段 7，第 649 行）。
2. 画出它们的先后顺序。

**需要观察的现象**：`create_conf` 在解析配置之前建立空壳；`ngx_conf_parse` 填充配置；`init_module` 在最后才被调用。

**预期结果**：你会清楚看到「配置层回调（create_conf/init_conf）」与「运行时层回调（init_module）」被严格分在装配线的不同阶段，这正是 u3-l2 强调的两套回调分层的落点。

#### 4.3.5 小练习与答案

**练习 1**：`init_module` 和 `init_process` 的调用频率有何不同？
**答案**：`init_module` 在每次构造 cycle（含 reload）时调用一次，由 master 触发；`init_process` 在每个 worker 进程启动时调用一次，所以有 N 个 worker 就会被调用 N 遍。

**练习 2**：为什么 `ngx_http_static_module` 的 `init_module` 字段是 `NULL`（见 [src/http/modules/ngx_http_static_module.c:38](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_static_module.c#L38)），但它依然能在 content 阶段处理请求？
**答案**：HTTP 模块的「接入运行时」用的是 `ngx_http_module_t` 里的 `postconfiguration` 回调（见 [src/http/modules/ngx_http_static_module.c:17-29](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_static_module.c#L17-L29) 的 `ngx_http_static_init`），它在 http 块解析时由 HTTP 框架调用，把 handler 注册到 content phase——这是 HTTP 专属的「类内回调」，走 `ctx` 而非 `init_module`。`init_module` 是通用钩子，HTTP 模块通常用不到它。

### 4.4 动态模块加载：ngx_load_module 与 ngx_add_module

#### 4.4.1 概念说明

动态模块（`.so` 文件）让你可以**不重新编译 nginx 主程序**就增减功能。使用方式很简单，在 `nginx.conf` 最顶部写：

```nginx
load_module modules/ngx_http_image_filter_module.so;
```

背后的机制是标准的 `dlopen`/`dlsym`。nginx 把 `.so` 加载进进程地址空间，从中取出它自带的 `ngx_modules[]`（注意：每个 `.so` 内部也有一个自己的 `ngx_modules[]` 数组，列出它携带的模块），逐个用 `ngx_add_module` 登记进当前 cycle 的模块表。

但跨进程加载 C 结构体有一个致命风险：**二进制兼容性**。`.so` 里的 `ngx_module_t` 结构体布局、字段大小，必须和正在运行的 nginx 完全一致，否则取出来的指针全是乱码。因此 nginx 做了两道校验：

1. **版本号校验**：`.so` 编译时的 `nginx_version` 必须等于当前二进制的 `nginx_version`。
2. **二进制兼容签名**：一个 35 位的特征字符串，编码了指针大小、`time_t` 大小、是否启用 epoll/SSL/PCRE 等关键 ABI 特性，必须逐字符相等。

只要二者任一不匹配，nginx 就拒绝加载（报错 "is not binary compatible"），避免运行时崩溃。

#### 4.4.2 核心流程

```text
nginx.conf 顶部: load_module xxx.so;
      │
      ▼
ngx_conf_parse 遇到 load_module 指令
      │
      ▼
ngx_load_module(cf, cmd, conf)            # nginx.c
      │
      ├─ 若 cycle->modules_used 已置位 → 报 "is specified too late"（必须早于 http{}）
      ├─ ngx_dlopen(file)                 # 打开 .so
      ├─ 注册 cleanup ngx_unload_module   # 解析失败时 dlclose 回滚
      ├─ ngx_dlsym(handle, "ngx_modules") # 取 .so 内部的模块指针数组
      ├─ ngx_dlsym(handle, "ngx_module_names")
      └─ 对每个 module:
            ngx_add_module(cf, file, module, order)
               ├─ 校验 cf->cycle->modules_n < ngx_max_module
               ├─ 校验 module->version == nginx_version       # 版本号
               ├─ 校验 module->signature == NGX_MODULE_SIGNATURE # 二进制签名
               ├─ 查重: 名字不能与已加载模块重复
               ├─ module->index = ngx_module_index(...)        # 分配全局 index
               ├─ 按 order 插入 cycle->modules（可调整模块顺序）
               └─ 若是核心模块，立即 create_conf 并挂到 conf_ctx[index]
```

一个关键约束：`load_module` 必须出现在所有协议块（`events{}`/`http{}`/...）**之前**。因为一旦进入 `http{}` 解析，`ngx_count_modules` 会设置 `cycle->modules_used = 1`「锁门」，此后再 `load_module` 会得到 "is specified too late" 错误——道理很简单：HTTP 模块的 `ctx_index` 已经分配完了，配置数组也开好了，再塞新模块就会越界。

#### 4.4.3 源码精读

`load_module` 指令本身的注册：

[src/core/nginx.c:149-154](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L149-L154) 把 `load_module` 声明为一个 `NGX_MAIN_CONF|NGX_DIRECT_CONF` 作用域、`NGX_CONF_TAKE1`（一个参数）的指令，handler 是 `ngx_load_module`。它出现在 `ngx_core_commands[]` 表里，归 `ngx_core_module` 所有。

加载入口 `ngx_load_module`：

[src/core/nginx.c:1581-1660](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L1581-L1660) 是核心实现，要点：

- 整段被 `#if (NGX_HAVE_DLOPEN)` 包裹（第 1584 / 1652 行）。若平台不支持 `dlopen`，则走 `#else` 分支报 "load_module is not supported on this platform"（第 1654–1657 行）。
- 第 1592–1594 行：检查 `cf->cycle->modules_used`，若为真说明已经过了 `ngx_count_modules`，返回 `"is specified too late"`。
- 第 1609 行：`ngx_dlopen(file.data)` 加载 `.so`。
- 第 1604–1618 行：先注册一个内存池 cleanup（`ngx_unload_module`，定义在 [src/core/nginx.c:1665-1674](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L1665-L1674)，执行 `ngx_dlclose`）。这样一旦本次配置解析失败、cycle 被销毁，`.so` 会被自动卸载，不会泄漏。
- 第 1620、1628 行：用 `ngx_dlsym` 从 `.so` 里取出 `ngx_modules` 与 `ngx_module_names` 两个符号。注意这俩名字和主程序里的全局数组同名——每个动态 `.so` 都自带一份。
- 第 1636 行：可选取出 `ngx_module_order`（模块排序提示）。
- 第 1638–1648 行：遍历 `.so` 内的 `ngx_modules[]`，给每个赋上 `name`，然后调 `ngx_add_module` 登记进当前 cycle。

真正的「登记 + 校验」逻辑在 `ngx_add_module`：

[src/core/ngx_module.c:156-276](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.c#L156-L276)，关键校验段：

- 第 164–168 行：容量校验 `modules_n >= ngx_max_module` → "too many modules loaded"。
- **第 170–175 行：版本号校验**。`module->version != nginx_version` 则报 `module "..." version %ui instead of %ui`。`version` 字段由 `NGX_MODULE_V1` 宏在编译期填成当时的 `nginx_version`，所以这里要求「`.so` 编译时的 nginx 源码版本」和「当前运行的 nginx 版本」完全相等。
- **第 177–182 行：二进制签名校验**。`ngx_strcmp(module->signature, NGX_MODULE_SIGNATURE) != 0` 则报 `"is not binary compatible"`。`NGX_MODULE_SIGNATURE` 是 35 段拼接的字符串，见 [src/core/ngx_module.h:205-217](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.h#L205-L217)。其中第 0 段 `NGX_MODULE_SIGNATURE_0`（[src/core/ngx_module.h:21-24](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.h#L21-L24)）最关键——它编码了 `NGX_PTR_SIZE`（指针大小）、`NGX_SIG_ATOMIC_T_SIZE`、`NGX_TIME_T_SIZE`，这三者任何一个不一致，结构体布局就会错位。其余位编码 epoll/SSL/PCRE/QUIC 等特性开关，确保 `.so` 引用的符号都存在。
- 第 184–191 行：按 `name` 查重，禁止重复加载同名模块。
- 第 197–205 行：若 `index` 未定，调 `ngx_module_index` 分配全局 index。

`ngx_module_index` 怎么找一个空闲 index：

[src/core/ngx_module.c:279-315](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.c#L279-L315) 用 `goto again` 的朴素算法：从 0 开始试，遇到某个已加载模块占用该 index 就 +1 重试，直到找到一个空位。同样会扫 `old_cycle` 防止 reload 回滚时撞车。

随后第 211–252 行按可选的 `order` 列表把模块插入到 `cycle->modules` 的合适位置（用 `ngx_memmove` 腾位），核心模块还会立即 `create_conf` 并挂到 `conf_ctx[index]`（第 254–273 行）——注释第 256–261 行说明：核心模块足够简单可以现场初始化，而 HTTP 模块必须赶在 `http{}` 块解析之前加载。

> 一句话区分两道校验：**版本号管「源码版本」，签名管「ABI 布局」**。两者都过，才允许把 `.so` 里的模块指针纳入 `cycle->modules`。

#### 4.4.4 代码实践

**实践目标**：体验 `load_module` 的「时序约束」与「二进制兼容校验」，并理解它们为何存在。

**操作步骤**：

1. **阅读时序约束**：在 [src/core/nginx.c:1592-1594](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L1592-L1594) 看到 `modules_used` 检查；再在 [src/core/ngx_module.c:150](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.c#L150) 看到 `ngx_count_modules` 把它置 1。据此回答：为什么 `load_module` 必须写在 `http{}` 之前？
2. **阅读版本校验**：对照 [src/core/ngx_module.c:170-175](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.c#L170-L175) 与 [src/core/ngx_module.h:220-222](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.h#L220-L222)（`NGX_MODULE_V1` 把 `version` 填成 `nginx_version`），说明版本号从哪来、在哪比。
3. **（可选，待本地验证）实操**：用 `--add-dynamic-module` 编译一个动态模块得到 `xxx.so`，在 `nginx.conf` 顶部加 `load_module`，用 `nginx -t` 验证；再把同一个 `.so` 拿到一个不同版本的 nginx 上加载，观察 `nginx -t` 是否报 "is not binary compatible"。

**需要观察的现象**：把 `load_module` 写在 `http{}` 之后时，`nginx -t` 报 "is specified too late"；版本/签名不匹配时报 "is not binary compatible"。

**预期结果**：你会直观理解「时序锁门」与「双重校验」两道安全阀的作用。第 3 步若无法本地构建，请明确标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么动态模块要同时检查 `version` 和 `signature`，只查一个不行吗？
**答案**：不行。`version` 是源码版本号，两个相邻小版本可能 ABI 兼容，也可能不兼容，它粒度太粗；`signature` 则精确编码了指针/`time_t` 大小与各项编译特性，能捕捉到「同版本但不同编译选项」（如一个开了 SSL 一个没开）导致的不兼容。两者互补：版本号挡住「跨大版本」，签名挡住「同版本异 ABI」。

**练习 2**：`ngx_load_module` 为什么要在 `ngx_dlopen` 之后立刻注册 `ngx_unload_module` 这个 cleanup？
**答案**：配置解析可能失败（例如后续 `ngx_add_module` 校验不通过），失败时 cycle 会被销毁、其内存池随之释放。注册在池上的 cleanup 会在池销毁时被调用，从而 `ngx_dlclose` 关闭 `.so`，避免文件描述符与地址空间泄漏。这是「资源生命周期绑定到内存池 cleanup 链」的典型用法（见 u2-l1）。

## 5. 综合实践

把本讲三处知识点串起来，完成规格要求的追踪任务：**追踪一个编译进来的 HTTP 模块（`ngx_http_static_module`）从 `ngx_modules` 表注册到 `init_module` 回调被调用的全过程，说明它的 `index` 与 `ctx_index` 如何分配**。

请按顺序完成以下源码追踪，并填写结论：

1. **编译期登记**：阅读 [auto/modules:1568-1590](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/modules#L1568-L1590)，确认 `ngx_http_static_module` 会被 `echo "    &ngx_http_static_module,"` 写进全局 `ngx_modules[]`。它在数组中的位置 `i` 就是它未来的 `index`（待本地构建后用 `objs/ngx_modules.c` 核对）。

2. **启动期赋 index**：阅读 [src/core/ngx_module.c:25-39](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.c#L25-L39)（`ngx_preinit_modules`）和它的调用点 [src/core/nginx.c:289-291](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L289-L291)。确认 `ngx_http_static_module->index` 被赋为它在 `ngx_modules[]` 中的下标，且此时尚未碰 `ctx_index`（仍为 -1）。

3. **cycle 拷贝**：阅读 [src/core/ngx_module.c:42-62](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.c#L42-L62)，确认 `ngx_init_cycle` 中 `cycle->modules` 拷贝了这份表（由 u3-l2 可知发生在装配线阶段 1）。

4. **解析 http{} 时赋 ctx_index**：阅读 [src/http/ngx_http.c:150](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L150) 与 [src/core/ngx_module.c:82-153](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.c#L82-L153)。`ngx_count_modules(cycle, NGX_HTTP_MODULE)` 遍历到 `ngx_http_static_module`（`type == NGX_HTTP_MODULE` 且 `ctx_index == -1`），调 `ngx_module_ctx_index` 给它分配一个 HTTP 类内的空闲序号。**这就是它的 `ctx_index`，用来在 `main_conf/srv_conf/loc_conf[ctx_index]` 取配置。**

5. **init_module 触发**：阅读 [src/core/ngx_cycle.c:649-652](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L649-L652) 与 [src/core/ngx_module.c:65-79](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.c#L65-L79)。装配线末尾 `ngx_init_modules` 遍历 `cycle->modules` 调 `init_module`。注意 `ngx_http_static_module` 的 `init_module` 是 `NULL`（见 [src/http/modules/ngx_http_static_module.c:38](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_static_module.c#L38)），所以本次跳过——它的运行时初始化走的是 HTTP 专属的 `postconfiguration` 回调 `ngx_http_static_init`（[src/http/modules/ngx_http_static_module.c:19](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_static_module.c#L19)）。

**最终产出**：画一张时序图，横轴是「编译期 → 启动期 → 解析 http{} → 装配末尾 → worker 启动」，在 `ngx_http_static_module` 这一行标出 `index`、`ctx_index` 分别在哪个时刻、由哪个函数赋值，以及它的 `init_module` 为何不被调用。

**预期结论**：
- `index`：编译期进 `ngx_modules[]`，启动期由 `ngx_preinit_modules` 赋为下标，之后稳定不变。
- `ctx_index`：解析到 `http{}` 时由 `ngx_count_modules` 赋为 HTTP 类内序号。
- `init_module`：装配线末尾由 `ngx_init_modules` 统一调度，但本模块该字段为 `NULL`，实际「接入运行时」由 HTTP 框架通过 `postconfiguration` 完成。

## 6. 本讲小结

- nginx 所有模块共享同一个 `ngx_module_t` 外壳，靠 `type` 区分种类、靠 `ctx` 携带类内专属回调表——这是「标签联合」设计。
- **两套索引**：`index` 是全局学号（启动期 `ngx_preinit_modules` 分配），`ctx_index` 是班内序号（解析配置块时 `ngx_count_modules` 分配），分别索引全局模块表与各类配置数组。
- `ngx_max_module = 静态模块数 + 128`，预留的索引空间让动态模块能就地插入 `cycle->modules` 而无需重新分配。
- 静态模块表 `ngx_modules[]` 与 `ngx_module_names[]` 由 `auto/modules` 在 `./configure` 时生成进 `objs/ngx_modules.c`，运行时由 `ngx_cycle_modules` 拷贝到每个 cycle 自己的表中。
- 模块回调分两层：配置层（`create_conf`/`init_conf`，仅核心模块）与运行时层（`init_module`/`init_process`，所有模块）；`init_module` 在 `ngx_init_cycle` 末尾由 `ngx_init_modules` 触发，`init_process` 在 worker 启动时触发。
- 动态模块通过 `load_module` → `ngx_load_module`（`dlopen`/`dlsym`）→ `ngx_add_module` 加载，必须通过**版本号 + 二进制签名**双重校验，且必须早于 `http{}` 等配置块（`modules_used` 锁门）。

## 7. 下一步学习建议

- **进入 HTTP 框架**：本讲是理解 HTTP 模块机制的地基。下一讲 u6-l1「HTTP 模块框架与上下文」会展示 `ngx_http_module_t` 这个「类内回调表」的完整字段，以及 `main_conf/srv_conf/loc_conf` 三层配置数组如何用本讲讲的 `ctx_index` 来索引——届时你会真正看到 `ctx_index` 的用武之地。
- **动手写模块**：u10-l4「编写自定义 HTTP 模块（实战）」会综合本讲的模块结构体知识，演示如何用 `auto/module` 把一个自定义 content handler 模块编译进 nginx。
- **延伸阅读**：动手 `./configure` 后打开 `objs/ngx_modules.c`，对照本讲 4.2 看 `ngx_modules[]` 的真实内容；再读 [src/core/ngx_module.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.c) 全文，把 `ngx_add_module` 的 `order` 排序逻辑也读懂（本讲略过，留给有兴趣的读者）。
