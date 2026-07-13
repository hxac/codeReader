# HTTP 模块框架与上下文

## 1. 本讲目标

本讲是第六单元「HTTP 核心处理」的第一篇，目标是把 `http {}` 这个配置块背后到底发生了什么讲清楚。读完本讲，你应该能够：

- 说清 nginx 为什么要把 HTTP 配置拆成 **main / server / location 三层**，以及这三层在内存里是怎样用 `ngx_http_conf_ctx_t` 组织的。
- 说清一个 HTTP 模块的「上下文」`ngx_http_module_t` 里那 8 个回调（`create_main_conf` / `init_main_conf` / `create_srv_conf` / `merge_srv_conf` / `create_loc_conf` / `merge_loc_conf` / `preconfiguration` / `postconfiguration`）分别在什么时候被调用。
- 区分两个容易混淆的「核心」：负责把 `http {}` 块接入配置世界的 `ngx_http_module`（CORE 模块），与 HTTP 层内部真正的框架核心 `ngx_http_core_module`（HTTP 模块）。
- 沿着 `ngx_http_block` 的初始化流程，把「建空壳 → 解析配置 → 合并配置 → 建 location 树 → 初始化 phases → postconfiguration → 优化监听端口」这一整条线串起来。

本讲只讲「框架与配置结构」，不展开请求生命周期、phases 调度、location 匹配算法等细节——它们各有后续讲义（u6-l2 ~ u6-l5）。

## 2. 前置知识

在进入 HTTP 框架前，请确认你已经掌握以下来自前面讲义的概念，本讲会直接使用它们而不再重复解释：

- **指令描述符 `ngx_command_t` 与配置解析器 `ngx_conf_parse`**（u3-l1）：nginx 配置被逐词切分后，由 `ngx_conf_handler` 遍历所有模块的指令表匹配，匹配到就调它的 `set` 回调。块指令的 `set` 回调会递归调用 `ngx_conf_parse(cf, NULL)` 解析块内部内容。
- **`cf->ctx` 与 `cf->cmd_type`**（u3-l1）：`cf->ctx` 是当前配置上下文（一个指向「本层所有模块配置」的指针），`cf->cmd_type` 是当前块允许的指令作用域（如 `NGX_HTTP_MAIN_CONF` / `NGX_HTTP_SRV_CONF` / `NGX_HTTP_LOC_CONF`）。进块时切换、出块时恢复。
- **模块系统与两套索引**（u3-l3）：每个模块有全局唯一的 `index` 和类内唯一的 `ctx_index`；`ngx_count_modules(cf->cycle, NGX_HTTP_MODULE)` 会给所有 HTTP 模块分配 `ctx_index` 并返回总数，存入 `ngx_http_max_module`。
- **`cycle->conf_ctx[]`**（u3-l2 / u3-l3）：`ngx_cycle_t` 的 `conf_ctx` 是一个按模块 `index` 索引的指针数组，每个核心模块把自己的全局配置挂在这里。HTTP 的 main 配置就挂在 `conf_ctx[ngx_http_module.index]`。
- **`NGX_CONF_UNSET` 哨兵与 merge**（u3-l4）：`create_*_conf` 时把字段初始化成 `NGX_CONF_UNSET` / `NGX_CONF_UNSET_SIZE` / `NGX_CONF_UNSET_MSEC` 等哨兵值，`merge_*_conf` 据此判断「子层有没有显式设置」——设置了就用子层的值，没设置就从父层继承。
- **连接管理 `ngx_connection_t`**（u5-l3）：accept 出来的连接最终会调监听套接字的 `ls->handler`，HTTP 模块在初始化时把这个 handler 设成 `ngx_http_init_connection`。本讲只涉及配置阶段，不涉及运行时 accept，但要知道 HTTP 框架最终要在监听端口上挂 handler。

如果上面任何一项你觉得陌生，建议先回到对应讲义复习。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| `src/http/ngx_http_config.h` | 定义 HTTP 配置的核心数据结构：三层上下文 `ngx_http_conf_ctx_t`、HTTP 模块上下文类型 `ngx_http_module_t`、作用域位掩码与取配置的宏。 |
| `src/http/ngx_http_core_module.h` | 定义 HTTP 层「核心模块」的各种配置结构体：`ngx_http_core_main_conf_t`、`ngx_http_core_srv_conf_t`、`ngx_http_core_loc_conf_t`，以及 phases 枚举。 |
| `src/http/ngx_http.c` | 定义把 `http {}` 块接入配置世界的 CORE 模块 `ngx_http_module`，以及整个 HTTP 配置阶段的入口函数 `ngx_http_block`、合并函数 `ngx_http_merge_servers` / `ngx_http_merge_locations`。 |
| `src/http/ngx_http_core_module.c` | 定义 HTTP 层真正的框架核心 `ngx_http_core_module`，以及 `server {}`、`location {}` 两个块指令的处理函数 `ngx_http_core_server` / `ngx_http_core_location`，还有 `create_*_conf` / `merge_*_conf` 的具体实现。 |

一个总览性的认知：`ngx_http.c` 负责「打开 HTTP 这扇门」，`ngx_http_core_module.c` 负责「门里面的框架骨架」。两者分工是本讲最重要的区分点之一。

## 4. 核心概念与源码讲解

### 4.1 三层配置结构 ngx_http_conf_ctx_t

#### 4.1.1 概念说明

nginx 的 HTTP 配置天然是三层嵌套的：

```
http {            # main 层：对所有 server/location 生效
    server {      # srv 层：对某个虚拟主机生效
        location / {  # loc 层：对匹配某 URI 的请求生效
        }
    }
}
```

问题是：一个配置文件里可能有几十个 server、每个 server 里又有几十个 location，而 HTTP 模块（gzip、access、rewrite、proxy……）有几十个。每个模块在每一层都可能有自己的配置结构体。如果用嵌套结构体硬写，会得到一个维度爆炸的巨型结构。

nginx 的解法是**「按层组织指针数组」**：每一层用一个上下文结构 `ngx_http_conf_ctx_t`，里面装三个指针数组 `main_conf` / `srv_conf` / `loc_conf`，每个数组按模块的 `ctx_index` 索引，第 `i` 个槽位存放第 `i` 个 HTTP 模块在本层的配置结构体指针。

这样设计的好处是：

- 模块与模块之间完全解耦——新增一个 HTTP 模块，只要给它分配一个新的 `ctx_index`，所有层的数组自动多一个槽位，其它模块毫不知情。
- 取配置极快——运行时拿到一个请求的 `ctx`，要读某个模块的 loc_conf，就是 `ctx->loc_conf[module.ctx_index]`，一次数组下标访问。
- 三层可以分别继承——`main_conf` 全局共享，`srv_conf` 按 server 隔离，`loc_conf` 按 location 隔离，merge 时沿「http → server → location」链向下传递。

#### 4.1.2 核心流程

三层 `ngx_http_conf_ctx_t` 的创建与共享关系如下（伪代码）：

```
进入 http{} 块 (ngx_http_block):
    ctx_http.main_conf = 新数组[ngx_http_max_module]   # 全局唯一一份
    ctx_http.srv_conf  = 新数组[...]                    # http 层的"空"srv_conf，作 merge 基准
    ctx_http.loc_conf  = 新数组[...]                    # http 层的"空"loc_conf，作 merge 基准
    对每个 HTTP 模块调 create_main_conf / create_srv_conf / create_loc_conf 填进数组

进入 server{} 块 (ngx_http_core_server):
    ctx_srv.main_conf = ctx_http.main_conf             # 共享同一份 main_conf
    ctx_srv.srv_conf  = 新数组[...]                    # 本 server 专属
    ctx_srv.loc_conf  = 新数组[...]                    # 本 server 的"空"loc_conf
    对每个 HTTP 模块调 create_srv_conf / create_loc_conf 填进数组

进入 location{} 块 (ngx_http_core_location):
    ctx_loc.main_conf = pctx.main_conf                 # 共享
    ctx_loc.srv_conf  = pctx.srv_conf                  # 共享所属 server 的 srv_conf
    ctx_loc.loc_conf  = 新数组[...]                    # 本 location 专属
    对每个 HTTP 模块调 create_loc_conf 填进数组

合并阶段 (ngx_http_merge_servers → ngx_http_merge_locations):
    对每个模块调 merge_srv_conf(http.srv_conf, server.srv_conf)
    对每个模块调 merge_loc_conf(server.loc_conf, location.loc_conf)
    递归处理嵌套 location
```

关键点：**`main_conf` 数组在整个 HTTP 配置里只有一份，所有 server 和 location 的 ctx 都指向它**；`srv_conf` 每 server 一份；`loc_conf` 每 location 一份（http 层和 server 层也各有一份「空」loc_conf 作为 merge 的父节点）。

#### 4.1.3 源码精读

三层上下文的定义极其简洁，只有三个 `void **`：

[src/http/ngx_http_config.h:17-21](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_config.h#L17-L21) —— `ngx_http_conf_ctx_t` 把 main/srv/loc 三层配置组织成三个 `void **` 指针数组，每个数组按模块 `ctx_index` 索引。

`void **` 而非具体类型，是因为不同模块的配置结构体各不相同，这里只存指针、由调用方用宏还原成具体类型。还原靠下面这组取配置宏，它们是运行时访问模块配置的标准入口：

[src/http/ngx_http_config.h:55-58](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_config.h#L55-L58) —— 请求运行时用 `ngx_http_get_module_main_conf(r, module)` 等宏，从请求的 `main_conf/srv_conf/loc_conf` 数组中按 `module.ctx_index` 取出该模块的配置指针。

配置阶段也有对应版本，从 `cf->ctx` 取（注意 `cf->ctx` 总是指向「当前所在层」的 `ngx_http_conf_ctx_t`）：

[src/http/ngx_http_config.h:61-66](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_config.h#L61-L66) —— 配置解析时用 `ngx_http_conf_get_module_main_conf(cf, module)` 等，从 `cf->ctx` 取当前层的配置。

而要把整个 HTTP 的 main 配置挂到 cycle 上（供非 HTTP 代码在运行时按模块 `index` 取），用这个宏：

[src/http/ngx_http_config.h:68-72](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_config.h#L68-L72) —— `ngx_http_cycle_get_module_main_conf(cycle, module)` 从 `cycle->conf_ctx[ngx_http_module.index]` 取出 HTTP 的 ctx，再取 main_conf。注意它用的是模块的 `index`（全局索引），因为 `conf_ctx` 是按 `index` 索引的。

「按层共享 main_conf」在 server 块处理函数里看得最清楚——`ctx->main_conf = http_ctx->main_conf` 直接赋值指针，不拷贝：

[src/http/ngx_http_core_module.c:2993-3013](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L2993-L3013) —— `ngx_http_core_server` 为每个 server 新建一个 `ngx_http_conf_ctx_t`，其中 `main_conf` 直接指向 http 层的 `main_conf`（共享），只有 `srv_conf` 和 `loc_conf` 是新建数组。

location 块同理，`main_conf` 和 `srv_conf` 都从父上下文共享，只有 `loc_conf` 新建：

[src/http/ngx_http_core_module.c:3135-3147](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L3135-L3147) —— `ngx_http_core_location` 为每个 location 新建 ctx，`main_conf` 与 `srv_conf` 沿用父层，只新建 `loc_conf` 数组。

#### 4.1.4 代码实践

**实践目标**：用一个最小 nginx.conf，亲手验证「main_conf 全局共享、srv_conf/loc_conf 按层隔离」。

**操作步骤**：

1. 在源码根目录新建 `myconf/nginx.conf`（示例配置，需自行准备目录）：

   ```nginx
   worker_processes 1;
   events { worker_connections 1024; }

   http {
       # 这里写在 http 层的指令，进入 main_conf 或 http 层的 null loc_conf
       sendfile on;
       client_max_body_size 10m;

       server {
           listen 8080;
           server_name a.example.com;
           # server 层指令进入本 server 的 srv_conf / loc_conf
           client_max_body_size 5m;

           location / {
               # location 层指令进入本 location 的 loc_conf
               client_max_body_size 1m;
           }

           location /api {
               # 不写 client_max_body_size，应继承上层
           }
       }
   }
   ```

2. 用源码验证「每层都新建一个 ctx、main_conf 共享」。在 `ngx_http_core_server`（`src/http/ngx_http_core_module.c:2993`）和 `ngx_http_core_location`（`src/http/ngx_http_core_module.c:3135`）处各下一行断点式的阅读标记，确认 `ctx->main_conf = ...main_conf` 这一行存在。

3. 运行 `objs/nginx -p . -c myconf/nginx.conf -t` 校验配置（`-p` 指定 prefix，`-t` 测试不启动）。

**需要观察的现象**：

- `nginx -t` 输出 `syntax is ok` 和 `test is successful`，说明三层嵌套被正确解析。
- `client_max_body_size` 在 `http`、`server`、`location /` 三层都出现了，但在 `location /api` 里没出现——这正是 merge 要解决的「继承」问题。

**预期结果**：配置通过校验。运行时 `/api` 的 `client_max_body_size` 应等于 server 层的 5m（因为 location /api 没设，从所属 server 继承）；`/` 的等于 1m；其它 server 没设的则从 http 层继承 10m。具体的 merge 算法在 4.2 节验证。

> 待本地验证：`nginx -t` 的确切输出文案与 prefix 路径解析行为，请以本机实际运行为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ngx_http_conf_ctx_t` 里要同时存 `main_conf`、`srv_conf`、`loc_conf` 三个数组，而不是只存一个「当前层」的数组？

**参考答案**：因为 merge 阶段需要在同一层上下文里同时访问「父层的 srv_conf」和「子层的 srv_conf」（loc_conf 同理）。例如合并 server 的 loc_conf 时，父是 http 层的 null loc_conf、子是 server 的 loc_conf，两者都通过同一个 `ngx_http_conf_ctx_t` 的 `loc_conf` 字段访问——`ngx_http_merge_servers` 通过临时把 `ctx->loc_conf` 指向不同来源来实现「父/子切换」（见 4.2.3）。如果只存一层，就无法在 merge 时同时拿到父子两份配置。

**练习 2**：一个 `location {}` 的 `ngx_http_conf_ctx_t` 里，`srv_conf` 指针指向谁？

**参考答案**：指向它所属 server 的 `srv_conf` 数组（`ctx->srv_conf = pctx->srv_conf`，见 `ngx_http_core_module.c:3142`）。所以同一个 server 下所有 location 共享同一份 srv_conf，而不同 server 下的 location 各有各的 srv_conf。

---

### 4.2 HTTP 模块上下文 ngx_http_module_t 与 create/merge 回调

#### 4.2.1 概念说明

光有「三层指针数组」这个容器还不够——谁来填充每个槽位里的具体配置结构体？谁来决定「子层没设的值如何从父层继承」？这些职责由每个 HTTP 模块自己提供，通过「模块上下文」`ngx_http_module_t` 暴露给框架。

回忆 u3-l3：每个 `ngx_module_t` 有一个 `ctx` 字段指向「类内上下文」，CORE 模块的 ctx 是 `ngx_core_module_t`（只有 `create_conf` / `init_conf`），HTTP 模块的 ctx 就是这里的 `ngx_http_module_t`，它是一张 8 槽的回调表，分两组：

- **配置构建组**（6 个）：`create_main_conf` / `init_main_conf` / `create_srv_conf` / `merge_srv_conf` / `create_loc_conf` / `merge_loc_conf`。
- **配置前后钩子组**（2 个）：`preconfiguration` / `postconfiguration`。

框架在 `ngx_http_block` 里会按固定时机依次调用这些回调：先 `create_*` 建空壳，解析配置填值，再 `merge_*` 沿三层继承，最后 `postconfiguration` 让模块完成收尾（如注册 phase handler、建哈希表）。

#### 4.2.2 核心流程

8 个回调的调用时机（全部在 `ngx_http_block` 内，见 4.4）：

```
1. 对每个模块调 create_main_conf / create_srv_conf / create_loc_conf
   → 给 http 层的三个数组填空壳（字段初始化为 NGX_CONF_UNSET 哨兵）
2. 对每个模块调 preconfiguration
   → 模块可在解析前做准备（如注册变量）
3. ngx_conf_parse 解析 http{} 块
   → 块内每条指令的 set 回调把值写进对应层的 conf 结构体
   → 遇到 server{} 调 ngx_http_core_server：建 server ctx，调 create_srv_conf/create_loc_conf，再递归 parse
   → server 内遇到 location{} 调 ngx_http_core_location：建 location ctx，调 create_loc_conf，再递归 parse
4. 对每个模块调 init_main_conf（补 main 层默认值）+ merge_servers（沿 http→server→location 合并 srv/loc conf）
5. ngx_http_init_locations / init_static_location_trees（建 location 查找树）
6. ngx_http_init_phases（建 11 个阶段的 handler 数组）
7. 对每个模块调 postconfiguration（注册 phase handler、建哈希表等）
8. ngx_http_variables_init_vars（变量系统收尾）
9. ngx_http_init_phase_handlers（把各 phase 的 handler 数组拍平成一维执行表）
10. ngx_http_optimize_servers（把端口/地址/server_name 优化成监听套接字）
```

`merge_*_conf(cf, parent, child)` 的语义统一：`parent` 是上层（http 或父 location）的 conf，`child` 是本层的 conf。合并规则是「child 显式设置的（非哨兵）保留，未设置的从 parent 继承，parent 也没设置则用硬编码默认值」。这套规则靠 `NGX_CONF_UNSET` 哨兵 + `ngx_conf_merge_*` 宏实现（u3-l4 已讲过哨兵机制，这里看它在 HTTP 层的具体落地）。

#### 4.2.3 源码精读

HTTP 模块上下文类型——8 个回调函数指针：

[src/http/ngx_http_config.h:24-36](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_config.h#L24-L36) —— `ngx_http_module_t` 定义了 HTTP 模块的全部 8 个回调：两组配置构建回调（main/srv/loc 的 create 与 merge）加 pre/post configuration。

HTTP 模块上下文通过 `ngx_http_core_module_ctx` 这个具体实例看最清楚——它是 `ngx_http_core_module` 的 ctx，8 个槽全填满：

[src/http/ngx_http_core_module.c:806-818](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L806-L818) —— `ngx_http_core_module_ctx` 把 `ngx_http_core_module` 的 8 个回调全部接上，分别是 `preconfiguration` / `postconfiguration` / `create_main_conf` / `init_main_conf` / `create_srv_conf` / `merge_srv_conf` / `create_loc_conf` / `merge_loc_conf`。

`create_loc_conf` 的实现展示「哨兵初始化」的标准套路：先 `ngx_pcalloc` 清零，再把可配置字段一个个置成 `NGX_CONF_UNSET*`：

[src/http/ngx_http_core_module.c:3634-3693](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L3634-L3693) —— `ngx_http_core_create_loc_conf` 用 `ngx_pcalloc` 分配并清零结构体，再把 `client_max_body_size`、`client_body_timeout`、`sendfile` 等字段初始化为对应的 `NGX_CONF_UNSET` 哨兵，为后续 merge 判断「是否显式设置」做准备。

`merge_loc_conf` 展示「子未设则继承父」的合并规则，以 `root` 字段为例：

[src/http/ngx_http_core_module.c:3757-3780](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L3757-L3780) —— `ngx_http_core_merge_loc_conf` 合并 `root`：若 `conf->root.data == NULL`（子层没设 root），就从 `prev->root` 继承；若父层也没设，才用硬编码默认 `"html"`。这就是「http → server → location」逐层继承的运行机制。

合并的「父子切换」技巧在 `ngx_http_merge_servers` 里——它临时改写 `ctx->srv_conf` / `ctx->loc_conf` 指向不同来源，让 merge 回调里的 `ngx_http_conf_get_module_*_conf(cf, module)` 取到正确的「当前层」配置：

[src/http/ngx_http.c:564-622](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L564-L622) —— `ngx_http_merge_servers` 对每个 server 调 `merge_srv_conf(saved.srv_conf[ctx_index], server.srv_conf[ctx_index])` 和 `merge_loc_conf`，合并完一个 server 的 loc_conf 后，再递归 `ngx_http_merge_locations` 处理该 server 下的所有 location。注意它先把原始 ctx 存到 `saved`、结束时 `*ctx = saved` 还原。

递归合并 location 的函数：

[src/http/ngx_http.c:626-638](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L626-L638) —— `ngx_http_merge_locations` 遍历 location 队列，对每个 location 调 `merge_loc_conf(parent_loc_conf, location.loc_conf)`，然后递归处理嵌套 location，实现任意深度配置树的合并。

#### 4.2.4 代码实践

**实践目标**：追踪 `client_max_body_size` 这一条指令从「写进配置结构体」到「被 merge 继承」的全过程，验证 4.1.4 里对 `/api` 继承 5m 的预测。

**操作步骤**：

1. 在 `src/http/ngx_http_core_module.c` 中搜索 `client_max_body_size` 指令的定义（在 `ngx_http_core_commands` 数组里），确认它的 `set` 回调是 `ngx_conf_set_off_slot`、`offset` 指向 `ngx_http_core_loc_conf_t.client_max_body_size`、作用域含 `NGX_HTTP_MAIN_CONF|NGX_HTTP_SRV_CONF|NGX_HTTP_LOC_CONF`（即三层都能写）。

2. 确认 `client_max_body_size` 字段在 `ngx_http_core_loc_conf_t` 中的位置：

   [src/http/ngx_http_core_module.h:358-358](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.h#L358-L358) —— `client_max_body_size` 是 `ngx_http_core_loc_conf_t` 的一个 `off_t` 字段。

3. 确认它被 `create_loc_conf` 初始化为 `NGX_CONF_UNSET`（见 4.2.3 引用的 `ngx_http_core_create_loc_conf`，`clcf->client_max_body_size = NGX_CONF_UNSET;`）。

4. 在 `ngx_http_core_merge_loc_conf` 中搜索 `client_max_body_size` 的 merge 语句，确认用的是 `ngx_conf_merge_size_value(conf->client_max_body_size, prev->client_max_body_size, 1m)`（或等价的 off 合并宏），即「子未设则取父，父也未设则取 1m」。

5. 用 4.1.4 的配置启动 nginx，向 `/api` 上传一个 3m 的文件，观察是否被接收（小于 server 层继承来的 5m，应通过）；再向 `/` 上传一个 3m 文件，观察是否返回 413 Request Entity Too Large（超过 location 层的 1m）。

**需要观察的现象**：

- 源码层面：`client_max_body_size` 走的是标准 `off` slot，写入靠 `offset` 反射式定位（u3-l4 讲过的机制），合并靠 `ngx_conf_merge_*` 宏。
- 行为层面：`/api` 的限制是 5m（从 server 继承），`/` 的限制是 1m（本 location 显式设置）。

**预期结果**：`/api` 上传 3m 成功，`/` 上传 3m 返回 413。这验证了「location 没设的字段从所属 server 继承」。

> 待本地验证：上传行为与 413 阈值请以本机实际 curl 上传测试为准；若用 `curl --data-binary @file http://127.0.0.1:8080/api` 测试，注意文件大小需落在 1m 与 5m 之间才能区分两层。

#### 4.2.5 小练习与答案

**练习 1**：`create_srv_conf` 和 `create_loc_conf` 都会在解析 `http {}` 块时被调用一次（用于建 http 层的「空」srv/loc conf），但这次调用产生的配置并不对应任何具体 server/location。它有什么用？

**参考答案**：它充当 merge 阶段的「顶层父节点」。当合并某个 server 的 srv_conf 时，`merge_srv_conf(cf, http.srv_conf[mi], server.srv_conf[mi])` 里的 `parent` 就是这个 http 层的空 srv_conf。这样 http 层写的指令（如 `sendfile on;` 写在 http 块里）就能通过「server 没设则继承 http」的规则传递给所有 server。没有这个空壳，http 层的指令就无处安放、无法被继承。

**练习 2**：一个 HTTP 模块可以只提供 `create_loc_conf` 而不提供 `merge_loc_conf` 吗？会有什么后果？

**参考答案**：框架允许（回调指针为 NULL 时跳过调用，见 `ngx_http_block` 里 `if (module->merge_loc_conf)` 的判空）。但后果是该模块的 loc_conf 不会沿层继承——location 没显式设置的字段会保持 `create_loc_conf` 时的 `NGX_CONF_UNSET` 哨兵值，运行时若直接使用可能得到无意义值。所以凡是有可继承配置项的模块，都必须同时实现 create 和 merge。

---

### 4.3 两个「核心」：ngx_http_module 与 ngx_http_core_module

#### 4.3.1 概念说明

初读 nginx HTTP 源码的人几乎都会被这两个名字搞晕：

- `ngx_http_module`（定义在 `src/http/ngx_http.c`）—— 它是一个 **CORE 模块**（`NGX_CORE_MODULE`），不是 HTTP 模块。它的唯一职责是注册 `http {}` 这个块指令，让配置文件里能出现 `http { ... }`。它的 `set` 回调就是 `ngx_http_block`，后者负责把整个 HTTP 世界初始化起来。可以把它理解成「HTTP 之门」。

- `ngx_http_core_module`（定义在 `src/http/ngx_http_core_module.c`）—— 它是一个 **HTTP 模块**（`NGX_HTTP_MODULE`），是 HTTP 层内部「排在第一位的框架模块」。它管理 server 列表、location 树、phases 引擎、变量哈希等「HTTP 框架级」状态，并注册 `server {}`、`location {}`、`listen`、`server_name`、`root` 等核心指令。可以把它理解成「门内的框架骨架」。

两者的关系是：`ngx_http_module`（CORE）触发 `ngx_http_block`，`ngx_http_block` 初始化好三层 ctx 后，`ngx_http_core_module`（HTTP）的 `create_main_conf` 产出的 `ngx_http_core_main_conf_t` 就成了整个 HTTP 框架的「总仓库」（`cmcf`），后续所有初始化都围绕它进行。

#### 4.3.2 核心流程

```
配置文件遇到 http{} 指令
  → ngx_conf_handler 在 ngx_http_module 的指令表里匹配到 "http"
  → 调 ngx_http_module 的 set 回调 = ngx_http_block
       → ngx_http_block 创建三层 ctx
       → 调用所有 HTTP 模块(含 ngx_http_core_module)的 create_*_conf
            → ngx_http_core_module.create_main_conf 产出 ngx_http_core_main_conf_t (cmcf)
       → ngx_conf_parse 解析 http{} 内部
            → 遇到 server{} → ngx_http_core_module 的 ngx_http_core_server 回调
            → 遇到 location{} → ngx_http_core_module 的 ngx_http_core_location 回调
       → 合并、建树、初始化 phases、postconfiguration
       → 把 cmcf 通过 ctx->main_conf[ngx_http_core_module.ctx_index] 暴露给所有代码
```

注意「按 `index` 索引」与「按 `ctx_index` 索引」的区别在这里再次体现：`cycle->conf_ctx[ngx_http_module.index]` 存 HTTP 的 ctx（用 CORE 模块的 `index`）；`ctx->main_conf[ngx_http_core_module.ctx_index]` 取 HTTP 核心模块的 main_conf（用 HTTP 模块的 `ctx_index`）。

#### 4.3.3 源码精读

先看「HTTP 之门」`ngx_http_module`——它是 CORE 模块，指令表里只有一条 `http` 块指令：

[src/http/ngx_http.c:86-96](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L86-L96) —— `ngx_http_commands` 只定义了 `http` 一条指令，类型 `NGX_MAIN_CONF|NGX_CONF_BLOCK|NGX_CONF_NOARGS`（只能在最外层 main 作用域、是块、无参数），处理函数 `ngx_http_block`。

[src/http/ngx_http.c:99-119](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L99-L119) —— `ngx_http_module` 的 `type` 是 `NGX_CORE_MODULE`（注意不是 `NGX_HTTP_MODULE`），ctx 是 `ngx_http_module_ctx`（一个只有名字的 `ngx_core_module_t`）。这就是「HTTP 之门」——它让 `http {}` 块出现在核心配置里，自身却不是 HTTP 模块。

再看「门内骨架」`ngx_http_core_module`——它是 HTTP 模块，ctx 是 8 回调全填的 `ngx_http_core_module_ctx`：

[src/http/ngx_http_core_module.c:821-834](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L821-L834) —— `ngx_http_core_module` 的 `type` 是 `NGX_HTTP_MODULE`，ctx 指向 4.2.3 引用的 `ngx_http_core_module_ctx`，指令表 `ngx_http_core_commands` 含 `server`、`location`、`listen` 等几十条核心指令。

`ngx_http_core_module` 产出的「总仓库」`ngx_http_core_main_conf_t`，是整个 HTTP 框架运行时的核心数据结构，里面装着 server 列表、phase 引擎、变量哈希、请求头哈希等：

[src/http/ngx_http_core_module.h:155-179](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.h#L155-L179) —— `ngx_http_core_main_conf_t` 含 `servers` 数组（所有虚拟主机）、`phase_engine`（请求处理执行表）、`headers_in_hash`（请求头解析哈希）、`variables_hash` / `variables`（变量系统）、`ports`（监听端口）和 `phases[NGX_HTTP_LOG_PHASE + 1]`（11 个阶段的 handler 数组）。

`ngx_http_block` 一开始就通过 `ctx->main_conf[ngx_http_core_module.ctx_index]` 拿到这个总仓库：

[src/http/ngx_http.c:251-252](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L251-L252) —— 解析完 http 块后，`cmcf = ctx->main_conf[ngx_http_core_module.ctx_index]` 取出 HTTP 核心模块的 main_conf，后续合并、建树、初始化 phases 全部围绕它进行。

#### 4.3.4 代码实践

**实践目标**：用源码证据区分两个「核心」模块的类型与职责。

**操作步骤**：

1. 在 `src/http/ngx_http.c` 中定位 `ngx_http_module` 的定义（4.3.3 已给出链接），确认其 `type` 字段是 `NGX_CORE_MODULE`。
2. 在 `src/http/ngx_http_core_module.c` 中定位 `ngx_http_core_module` 的定义，确认其 `type` 字段是 `NGX_HTTP_MODULE`。
3. 用只读 git 命令查看这两个符号被哪些地方引用，体会「门」与「骨架」的使用差异：

   ```bash
   # 在源码根目录执行（只读搜索）
   grep -rn "ngx_http_module\b" src/ | head
   grep -rn "ngx_http_core_module\b" src/ | head
   ```

**需要观察的现象**：

- `ngx_http_module` 主要被 `cycle->conf_ctx[ngx_http_module.index]` 这类用法引用（取 HTTP ctx 挂载点）。
- `ngx_http_core_module` 主要被 `ctx->main_conf[ngx_http_core_module.ctx_index]` 这类用法引用（取框架总仓库 cmcf）。

**预期结果**：能清晰说出「`ngx_http_module` 是 CORE 模块、负责注册 `http{}` 块；`ngx_http_core_module` 是 HTTP 模块、是 HTTP 框架骨架」，并且能用 `type` 字段和引用方式两个证据支撑。

> 待本地验证：grep 的具体命中行数与文件分布请以本机仓库为准。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `ngx_http_module` 的 `type` 改成 `NGX_HTTP_MODULE` 会发生什么？

**参考答案**：`ngx_http_block` 在最开头会调用 `ngx_count_modules(cf->cycle, NGX_HTTP_MODULE)` 给所有 HTTP 模块分配 `ctx_index`，此时 `ngx_http_module` 也会被算进去并分到一个 `ctx_index`，挤占索引空间；更关键的是，`ngx_http_module` 的 ctx 是 `ngx_core_module_t`（只有 name），把它当成 `ngx_http_module_t` 用会读到错误字段。同时它的 `http` 指令带 `NGX_MAIN_CONF` 作用域标志，但作为 HTTP 模块它不会出现在 `cf->cmd_type == NGX_HTTP_MAIN_CONF` 的分发上下文里——总之这是错误的、不可行的改动，仅用于理解两者的区别。

**练习 2**：为什么 `ngx_http_core_module` 既是「一个普通 HTTP 模块」，又能充当「框架核心」？

**参考答案**：因为它在机制上和其它 HTTP 模块完全平等——同样有 `ctx_index`、同样在 `ngx_http_block` 里被调 `create_*_conf` / `merge_*_conf` / `postconfiguration`。它的「核心」地位来自两点：一是它的 `create_main_conf` 产出的 `ngx_http_core_main_conf_t` 被框架约定为「总仓库」（存 servers、phase_engine、变量等），所有代码都按 `ctx->main_conf[ngx_http_core_module.ctx_index]` 取它；二是它注册了 `server` / `location` / `listen` 等「结构定义性」指令，这些指令的 set 回调（`ngx_http_core_server` / `ngx_http_core_location`）负责构建三层 ctx 树。即「核心」是职责意义上的，不是机制上的特权。

---

### 4.4 ngx_http_block 初始化流程

#### 4.4.1 概念说明

`ngx_http_block` 是整个 HTTP 配置阶段的总入口，也是本讲的核心。它在配置解析到 `http {}` 块时被调用（作为 `http` 指令的 `set` 回调），负责把一块纯文本配置变成运行时可用的三层 conf 树、location 查找树、phase 执行表和监听套接字。

可以把 `ngx_http_block` 看成一条「装配线」：原料是 `http {}` 块里的文本指令，产物是一棵配置好的 HTTP 框架。装配线分若干阶段，每个阶段都依赖前一阶段的产物，任一阶段失败则整条线回滚（`goto failed`）。

#### 4.4.2 核心流程

`ngx_http_block` 的装配步骤（与 4.2.2 的回调时机表对应，这里聚焦流程本身）：

```
ngx_http_block(cf, cmd, conf):
  1. 防重入：*(ngx_http_conf_ctx_t **)conf 非空则报 "is duplicate"
  2. 建 http 层 ctx，分配 main_conf / srv_conf / loc_conf 三个数组（各 ngx_http_max_module 个槽）
  3. ngx_count_modules(NGX_HTTP_MODULE) → 给所有 HTTP 模块分 ctx_index，得 ngx_http_max_module
  4. 遍历所有 HTTP 模块，调 create_main_conf / create_srv_conf / create_loc_conf 填 http 层三个数组
  5. 保存 cf 到 pcf，设 cf->ctx = ctx
  6. 遍历调 preconfiguration
  7. 设 cf->module_type=NGX_HTTP_MODULE, cf->cmd_type=NGX_HTTP_MAIN_CONF; ngx_conf_parse(cf, NULL) 解析 http{} 内部
       （此间 server{} → ngx_http_core_server, location{} → ngx_http_core_location 递归构建子层 ctx）
  8. 取 cmcf = ctx->main_conf[ngx_http_core_module.ctx_index]; cscfp = cmcf->servers.elts
  9. 遍历模块：调 init_main_conf + ngx_http_merge_servers（合并 srv/loc conf 沿三层继承）
  10. 遍历 servers：ngx_http_init_locations + ngx_http_init_static_location_trees（建 location 查找树）
  11. ngx_http_init_phases（给 11 个 phase 建 handler 数组）+ ngx_http_init_headers_in_hash（请求头哈希）
  12. 遍历调 postconfiguration（模块注册 phase handler、建自身哈希表）
  13. ngx_http_variables_init_vars（变量系统收尾）
  14. *cf = pcf（恢复外层 cf，http 块的 ctx 使命完成）
  15. ngx_http_init_phase_handlers（把 11 个 phase 的 handler 拍平成一维 phase_engine）
  16. ngx_http_optimize_servers（端口/地址/server_name 优化成 ngx_listening_t 监听套接字）
  17. return NGX_CONF_OK
  failed: *cf = pcf; return rv;
```

几个要点：

- **第 7 步是关键递归点**：`ngx_conf_parse` 解析 `http {}` 内部时，遇到 `server {}` 就触发 `ngx_http_core_server`，后者又调 `ngx_conf_parse` 解析 `server {}` 内部，遇到 `location {}` 再触发 `ngx_http_core_location`——这就是 u3-l1 讲过的「块指令 set 回调递归调 `ngx_conf_parse`」在 HTTP 层的具体应用。每次进块都切换 `cf->ctx` 和 `cf->cmd_type`，出块恢复。
- **第 9 步合并发生在解析之后**：先解析完所有层、所有指令都写进各自 conf，再统一 merge。这保证 merge 时父子两份 conf 都已填好。
- **第 15 步拍平 phase_engine**：解析期各模块通过 `postconfiguration` 把自己的 handler `ngx_array_push` 进对应 phase 的 `handlers` 数组（`cmcf->phases[...]`），第 15 步再把这些分散的数组按阶段顺序拍平成一维 `phase_engine.handlers`，供运行时 `ngx_http_core_run_phases` 线性推进（u6-l4 详讲）。
- **第 16 步才真正落实监听**：配置阶段只收集 `listen` 指令登记的端口/地址，直到这里才优化成 `ngx_listening_t`，最终由 cycle 初始化阶段（u3-l2 的 `ngx_open_listening_sockets`）真正 bind。

#### 4.4.3 源码精读

`ngx_http_block` 开头——防重入、建 ctx、数模块、分配三个数组：

[src/http/ngx_http.c:122-181](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L122-L181) —— `ngx_http_block` 先判重（`is duplicate`），建 http 层 `ngx_http_conf_ctx_t`，调 `ngx_count_modules` 得 `ngx_http_max_module`，再分配 `main_conf` / `srv_conf` / `loc_conf` 三个数组。注释点明 srv_conf/loc_conf 是「null」上下文，专门用作 merge 基准。

接着遍历所有 HTTP 模块调 `create_*_conf` 填空壳：

[src/http/ngx_http.c:189-217](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L189-L217) —— 遍历 `cf->cycle->modules`，对每个 `NGX_HTTP_MODULE` 调 `create_main_conf` / `create_srv_conf` / `create_loc_conf`，把返回的配置指针按 `ctx_index` 存进 http 层三个数组。

preconfiguration 后切到 HTTP 作用域解析 http 块内部：

[src/http/ngx_http.c:236-244](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L236-L244) —— 设 `cf->module_type = NGX_HTTP_MODULE`、`cf->cmd_type = NGX_HTTP_MAIN_CONF`，调 `ngx_conf_parse(cf, NULL)` 递归解析 `http {}` 块内部——此间所有 `server {}` / `location {}` 块被构建，子层 ctx 树成形。

解析完后取 cmcf，遍历模块做 `init_main_conf` + `merge_servers`：

[src/http/ngx_http.c:251-275](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L251-L275) —— 取出 `cmcf`，遍历每个 HTTP 模块调 `init_main_conf`（补 main 层默认值）和 `ngx_http_merge_servers`（沿 http→server→location 合并 srv/loc conf）。

建 location 树、初始化 phases 与请求头哈希：

[src/http/ngx_http.c:280-300](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L280-L300) —— 遍历每个 server 调 `ngx_http_init_locations` 和 `ngx_http_init_static_location_trees` 构建 location 查找树；接着 `ngx_http_init_phases` 给 11 个阶段建 handler 数组，`ngx_http_init_headers_in_hash` 建请求头解析哈希。

`ngx_http_init_phases` 给每个阶段初始化一个 `ngx_array_t`，用于在 postconfiguration 阶段收集各模块注册的 handler：

[src/http/ngx_http.c:350-410](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L350-L410) —— `ngx_http_init_phases` 为 `POST_READ` / `SERVER_REWRITE` / `REWRITE` / `PREACCESS` / `ACCESS` / `PRECONTENT` / `CONTENT` / `LOG` 等 8 个允许注册 handler 的阶段各初始化一个数组（注意不是全部 11 个阶段都收 handler，如 `FIND_CONFIG` / `POST_REWRITE` / `POST_ACCESS` 是框架内置的）。

11 个阶段的定义：

[src/http/ngx_http_core_module.h:110-129](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.h#L110-L129) —— `ngx_http_phases` 枚举定义了 11 个请求处理阶段，从 `NGX_HTTP_POST_READ_PHASE` 到 `NGX_HTTP_LOG_PHASE`，是 u6-l4 的核心，本讲只需知道框架在 `ngx_http_block` 里为它们建数组。

postconfiguration 后变量收尾，恢复 cf，最后拍平 phase_engine 并优化监听：

[src/http/ngx_http.c:303-338](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L303-L338) —— 遍历调每个模块的 `postconfiguration`（注册 phase handler 等），`ngx_http_variables_init_vars` 收尾变量系统，`*cf = pcf` 恢复外层上下文，`ngx_http_init_phase_handlers` 把各阶段 handler 数组拍平成一维 `phase_engine`，`ngx_http_optimize_servers` 把端口/地址/server_name 优化成监听套接字结构。

#### 4.4.4 代码实践

**实践目标**：在源码中标注 `ngx_http_block` 的装配线各阶段边界，并把一个最小配置的解析过程映射到这些阶段。

**操作步骤**：

1. 打开 `src/http/ngx_http.c`，在 `ngx_http_block`（L122 起）里用注释或笔记标注以下阶段边界（行号以本 HEAD 为准）：
   - 建数组：L155–L181
   - create conf：L189–L217
   - preconfiguration：L222–L234
   - 解析 http 块：L238–L244
   - init_main_conf + merge：L254–L275
   - location 树：L280–L291
   - init phases + headers hash：L294–L300
   - postconfiguration：L303–L315
   - 变量收尾：L317–L319
   - 恢复 cf + phase_engine + optimize_servers：L326–L338

2. 用 4.1.4 的最小配置，运行 `objs/nginx -p . -c myconf/nginx.conf -T`，`-T` 会把解析后的完整配置（含 include 展开）打印到 stderr。对照输出，确认 `http {}` / `server {}` / `location {}` 三层结构与源码中 `ngx_http_core_server` / `ngx_http_core_location` 的递归构建一一对应。

3. 追踪一条调用链并在笔记里画出来：

   ```
   ngx_conf_parse(主配置)
     → 匹配 "http" 指令 → ngx_http_block
        → ngx_conf_parse(http 块)
           → 匹配 "server" 指令 → ngx_http_core_server
              → ngx_conf_parse(server 块)
                 → 匹配 "location" 指令 → ngx_http_core_location
                    → ngx_conf_parse(location 块)
   ```

   在每一步的源码处确认 `cf->ctx` 和 `cf->cmd_type` 是如何被切换与恢复的（`pcf = *cf; ... cf->ctx = ctx; cf->cmd_type = ...; ... *cf = pcf;`）。

**需要观察的现象**：

- `nginx -T` 输出的配置缩进反映三层嵌套，每一层对应一个新建的 `ngx_http_conf_ctx_t`。
- 调用链呈「`ngx_conf_parse` ↔ 块指令 set 回调」交替递归的形态，正是 u3-l1 递归解析机制在 HTTP 层的体现。

**预期结果**：能画出从主配置 `ngx_conf_parse` 到最内层 `location {}` 的完整递归调用链，并标注每一层 ctx 的创建点（`ngx_http_block` L140、`ngx_http_core_server` L2993、`ngx_http_core_location` L3135）与 `cf->ctx` 的切换点。

> 待本地验证：`nginx -T` 的输出格式与 include 展开行为请以本机实际为准；若 `mime.types` 未找到，配置可能报错，可用绝对路径或去掉 `include` 行。

#### 4.4.5 小练习与答案

**练习 1**：`ngx_http_block` 里 `init_main_conf` / `merge_servers` 为什么必须放在 `ngx_conf_parse` 解析 http 块**之后**，而不能放在之前？

**参考答案**：因为 merge 需要父子两份 conf 都已填好。`ngx_conf_parse` 之前，http 层的 srv_conf/loc_conf 只是 `create_*_conf` 产出的空壳（字段全是 `NGX_CONF_UNSET`），server 层和 location 层的 ctx 甚至还没创建。只有解析完整个 http 块，所有 `server {}` / `location {}` 都已递归构建、所有指令都已写入对应层 conf 后，才能开始「子未设则继承父」的合并。放之前会让 merge 拿到全空壳，继承不到任何东西。

**练习 2**：`ngx_http_block` 末尾 `*cf = pcf`（恢复外层 cf）发生在 `ngx_http_init_phase_handlers` 和 `ngx_http_optimize_servers` **之前**。这两步为什么不再需要 `cf->ctx` 指向 http 的 ctx？

**参考答案**：`ngx_http_init_phase_handlers` 和 `ngx_http_optimize_servers` 只需要 `cmcf`（HTTP 总仓库），而 `cmcf` 已经在 L251 通过 `ctx->main_conf[ngx_http_core_module.ctx_index]` 取出并保存在局部变量里。它们不再依赖 `cf->ctx`，所以可以先把 `cf` 恢复成外层状态（让外层解析继续正常工作），再用局部 `cmcf` 完成收尾。源码注释（L321-L324）也点明 `cf->ctx` 只在 merge 与 postconfiguration 期间需要。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「配置→结构→源码」三段式任务。

**任务**：用一个稍复杂的配置，画出它的三层 conf 指针数组结构图，并用源码解释每个节点是如何被创建和合并的。

**配置**（在源码根目录新建 `myconf/nginx.conf`，示例配置需自行准备）：

```nginx
worker_processes 1;
events { worker_connections 1024; }

http {
    sendfile on;
    client_max_body_size 10m;       # http 层 loc_conf

    server {
        listen 8080;
        server_name a.com;
        client_max_body_size 5m;    # server 层 loc_conf

        location / {
            client_max_body_size 1m;   # location 层 loc_conf
        }

        location /static { }           # 继承 server 的 5m
    }

    server {
        listen 8080;
        server_name b.com;
        # 不设 client_max_body_size，继承 http 的 10m
    }
}
```

**要求**：

1. **画结构图**：在纸上或笔记里画出三个 `ngx_http_conf_ctx_t` 节点（http 层、server a、location /），标注每个节点的 `main_conf` / `srv_conf` / `loc_conf` 指向。要求标出：
   - http 层、server a、location / 三者的 `main_conf` 都指向**同一个**数组（共享）。
   - server a 的 `srv_conf` 是它自己的数组，location / 的 `srv_conf` 指向 server a 的 `srv_conf`（共享）。
   - 每个节点都有自己的 `loc_conf` 数组，但 server b 没有自己的 location，只有一个 server 级 loc_conf。

2. **标 merge 路径**：对 `client_max_body_size` 这个字段，画出三条 merge 路径：
   - http(10m) → server a(5m)：merge_loc_conf(parent=http loc_conf, child=server a loc_conf)，server a 显式设了 5m，结果 5m。
   - server a(5m) → location /(1m)：location / 显式设了 1m，结果 1m。
   - server a(5m) → location /static(UNSET)：location /static 没设，继承 server a 的 5m。
   - http(10m) → server b(UNSET)：server b 没设，继承 http 的 10m。

3. **源码定位**：为结构图里每个箭头找到源码依据：
   - 「main_conf 共享」→ `ngx_http_core_module.c:2999` 与 `:3141`。
   - 「server 的 loc_conf 创建」→ `ngx_http_core_module.c:3010-3013` 与 `:3031-3038`。
   - 「location 的 loc_conf 创建」→ `ngx_http_core_module.c:3144-3163`。
   - 「merge 调用」→ `ngx_http.c:584-602`（merge_srv_conf / merge_loc_conf）与 `ngx_http.c:608-610`（递归 merge_locations）。
   - 「client_max_body_size 的哨兵与合并」→ `ngx_http_core_module.c:3663`（create 时 UNSET）与 merge_loc_conf 中对应合并语句。

4. **验证**：运行 `objs/nginx -p . -c myconf/nginx.conf -t` 确认配置合法；若条件允许，按 4.2.4 的方法用 curl 上传不同大小文件，验证 `/static` 与 server b 的继承值是否符合预期。

**预期结果**：一张清晰的三层 ctx 结构图 + 一条贯穿 create/merge 的源码调用链，能向别人讲清「这条配置在内存里长什么样、每个值是从哪里来的」。

> 待本地验证：上传行为验证与 `nginx -t` 输出请以本机实际为准。

## 6. 本讲小结

- nginx 用 `ngx_http_conf_ctx_t` 把 HTTP 配置组织成 **main / srv / loc 三层指针数组**，每个数组按模块 `ctx_index` 索引；`main_conf` 全局唯一份被所有 server/location 共享，`srv_conf` 每 server 一份，`loc_conf` 每 location 一份（http 层与 server 层也各有一份「空」loc_conf 作 merge 基准）。
- 每个 HTTP 模块通过 `ngx_http_module_t` 暴露 8 个回调：`create_main/srv/loc_conf` 建空壳（字段初始化为 `NGX_CONF_UNSET` 哨兵）、`init_main_conf` 补默认、`merge_srv/loc_conf` 沿「http → server → location」继承（子未设则取父、父也未设则取硬编码默认）、`pre/postconfiguration` 做解析前后的钩子。
- 必须区分两个「核心」：`ngx_http_module`（`src/http/ngx_http.c`）是 **CORE 模块**，只负责注册 `http {}` 块指令、其 set 回调是 `ngx_http_block`；`ngx_http_core_module`（`src/http/ngx_http_core_module.c`）是 **HTTP 模块**，是 HTTP 框架骨架，产出总仓库 `ngx_http_core_main_conf_t`（cmcf），注册 `server`/`location`/`listen` 等结构定义性指令。
- `ngx_http_block` 是一条装配线：建数组 → `create_*_conf` → `preconfiguration` → `ngx_conf_parse`（递归构建 server/location 子层 ctx）→ `init_main_conf` + `merge_servers` → 建 location 树 → `init_phases` + `headers_in_hash` → `postconfiguration` → 变量收尾 → 恢复 cf → `init_phase_handlers` 拍平执行表 → `optimize_servers` 落实监听。
- 三层 ctx 的递归构建靠「块指令 set 回调再调 `ngx_conf_parse`」实现（u3-l1 机制在 HTTP 层的应用）：`http` → `ngx_http_block`、`server` → `ngx_http_core_server`、`location` → `ngx_http_core_location`，每次进块切 `cf->ctx`/`cf->cmd_type`、出块恢复。
- merge 发生在**解析之后**，且靠 `ngx_http_merge_servers` 临时改写 `ctx->srv_conf`/`ctx->loc_conf` 来切换父子来源，再递归 `ngx_http_merge_locations` 处理任意深度的 location 树。

## 7. 下一步学习建议

本讲只搭好了「配置结构」这一静态骨架，还没有进入请求运行时。建议按以下顺序继续：

1. **u6-l2 HTTP 请求生命周期**：看一个真实请求如何从 accept（u5-l3 的 `ngx_http_init_connection`）进入 HTTP 框架、创建 `ngx_http_request_t`、最终 `ngx_http_finalize_request`。请求结构体里的 `main_conf`/`srv_conf`/`loc_conf` 正是本讲三层 ctx 在运行时的落点。
2. **u6-l3 请求行、头部与请求体解析**：看 `ngx_http_parse` 状态机如何把字节流解析成请求结构。
3. **u6-l4 请求处理阶段 phases 机制**：本讲提到的 `phase_engine` 在这里展开——11 个阶段如何被 `ngx_http_core_run_phases` 线性推进，模块如何通过 `postconfiguration` 注册 handler。
4. **u6-l5 location 匹配与配置合并**：本讲的 `ngx_http_init_static_location_trees` 产出的 location 树在这里被用于运行时 URI 匹配；merge 的结果在这里被实际选用。
5. 之后再进入过滤器链（u6-l6）、变量系统（u6-l7）和静态文件 handler（u6-l8），形成完整的 HTTP 处理图景。

继续阅读源码时，建议把 `ngx_http_core_main_conf_t`（`src/http/ngx_http_core_module.h:155-179`）作为「HTTP 全局地图」常备手边——后续讲义涉及的 phases、变量、请求头哈希、ports 都挂在这个结构里。
