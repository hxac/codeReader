# 指令类型、conf slot 与地址族

## 1. 本讲目标

上一篇（u3-l1）我们看完了「文本 nginx.conf → 指令分发 → set 回调」的整条解析链。本讲要回答两个紧接着的问题：

1. **每个模块的 `set` 回调，都要自己手写一遍吗？** nginx 有成百上千条指令，绝大多数只是在「把第二个参数转换成某种 C 类型，写到本模块配置结构体的某个字段」。如果每条都手写，会有海量重复代码。本讲讲清楚 nginx 提供的一组**通用 slot 函数**（`ngx_conf_set_flag_slot` / `ngx_conf_set_str_slot` / `ngx_conf_set_num_slot` …），它们如何借助 `offsetof` 实现「反射式赋值」。

2. **`listen 127.0.0.1:8080;`、`allow 10.0.0.0/8;` 这类带地址的指令，文本是怎么变成内核认识的 `sockaddr` 和 CIDR 掩码的？** 这由 `src/core/ngx_inet.c` 中的解析函数负责，它们是 `listen`、`allow/deny`、`geo`、`proxy_pass` 等指令的共用底座。

学完本讲，你应该能够：

- 说清楚 `ngx_command_t` 里 `set` / `conf` / `offset` / `post` 四个字段的含义，以及 `offset` 如何被通用 slot 函数用来定位字段；
- 看懂 flag、num、str、keyval、size、msec、sec、bufs、enum、bitmask 等各种 slot 函数各自的输入、输出和「重复」判定方式；
- 理解 `ngx_inet_addr` / `ngx_ptocidr` / `ngx_cidr_match` / `ngx_parse_addr` 四个函数如何把 IPv4/IPv6 文本解析成网络字节序地址、CIDR 掩码和 `sockaddr`。

## 2. 前置知识

阅读本讲前，你需要先掌握（已在 u3-l1 建立）：

- **指令描述符 `ngx_command_t`**：每条指令对应一个结构体，描述它的名字、参数个数/作用域位标志（`type`）、`set` 回调、`conf` 与 `offset`、`post`。
- **`type` 位掩码的三组字段**：`ngx_conf_file.h` 顶部那行注释 `AAAA FF TT` 说明 `type` 一个整数同时编码了「参数个数（AAAA）」「命令标志（FF，如 BLOCK/FLAG）」「作用域类型（TT，如 HTTP 的 LOC/SRV/MAIN）」三组信息。
- **`cf->args`**：当前指令被切词后，所有 token（含指令名本身）组成的 `ngx_array_t`，所以 `value[0]` 是指令名，`value[1]` 是第一个参数。
- **HTTP 三层配置**：`ngx_http_conf_ctx_t` 含 `main_conf` / `srv_conf` / `loc_conf` 三个指针数组，按模块的 `ctx_index` 索引。
- **`offsetof`**：C 标准库 `<stddef.h>` 提供的宏，给出「某字段在结构体里的字节偏移量」，编译期常量。

如果你对其中任何一条感到陌生，建议先回看 u3-l1 再继续。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/core/ngx_conf_file.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.h) | 定义 `ngx_command_t`、各类 `NGX_CONF_UNSET*` 哨兵、`ngx_conf_*_slot` 函数声明、`ngx_conf_enum_t`/`ngx_conf_bitmask_t`/`ngx_conf_num_bounds_t` 等 post 结构 |
| [src/core/ngx_conf_file.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c) | 实现 `ngx_conf_set_flag_slot` / `set_str_slot` / `set_keyval_slot` / `set_num_slot` / `set_size_slot` / `set_msec_slot` 等全部 slot 函数，以及分发器 `ngx_conf_handler` 中计算 `conf` 指针的段落 |
| [src/core/ngx_inet.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_inet.h) | 定义 `ngx_cidr_t`、`ngx_addr_t`、`ngx_url_t`，以及地址/CIDR 解析函数声明 |
| [src/core/ngx_inet.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_inet.c) | 实现 `ngx_inet_addr`（IPv4 文本→网络字节序）、`ngx_ptocidr`（`addr/prefix` → `ngx_cidr_t`）、`ngx_cidr_match`、`ngx_parse_addr`（文本→`sockaddr`） |
| [src/http/ngx_http_core_module.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c) | 一个真实范例：`variables_hash_max_size` 等指令就是用 `ngx_conf_set_num_slot` + `offsetof` 注册的 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. 通用 slot 函数与 `offsetof` 反射式赋值（以 flag / num 为代表）；
2. 各类 slot 函数全表（str / keyval / size / msec / sec / bufs / enum / bitmask）；
3. `ngx_inet` 的地址与 CIDR 解析（`ngx_inet_addr` / `ngx_ptocidr` / `ngx_cidr_match` / `ngx_parse_addr`）。

### 4.1 通用 slot 函数与 offsetof 反射式赋值

#### 4.1.1 概念说明

设想你写了一个模块，它的 location 级配置结构体里有这些字段：

```c
/* 示例代码：虚构模块的配置结构体，非 nginx 原有代码 */
typedef struct {
    ngx_flag_t      enable;        /* on / off */
    ngx_int_t       max_count;     /* 整数 */
    ngx_msec_t      timeout;       /* 毫秒，可带单位 */
    ngx_str_t       greeting;      /* 字符串 */
} ngx_http_demo_loc_conf_t;
```

对应配置文件里你希望写：

```nginx
demo_enable    on;
demo_max_count 100;
demo_timeout   30s;
demo_greeting  "hello";
```

最朴素的写法是为每条指令单独写一个 `set` 回调，把 `value[1]` 转换成对应类型再赋给字段。nginx 早期也曾这样，但很快发现：**几乎所有「单参数、写一个字段」的指令，逻辑完全一样**，只有「目标字段在结构体里的位置」和「文本到 C 类型的转换方式」不同。

于是 nginx 提炼出一组**通用 slot 函数**，把「写到哪个字段」这件事用 `offsetof` 在**编译期**固化进指令表，运行时再据此定位。其精髓是这一行（出现在每一个 slot 函数里）：

```c
np = (ngx_int_t *) (p + cmd->offset);
```

其中 `p = conf`（指向本模块当前层级的配置结构体），`cmd->offset` 是字段在结构体里的字节偏移（在指令表里写成 `offsetof(ngx_http_demo_loc_conf_t, max_count)`）。两者相加就是该字段的地址。这就是「反射式赋值」——C 没有运行时反射，但 `offsetof` 提供了等价的、零开销的编译期反射。

回到指令描述符 `ngx_command_t`，本讲重点看后四个字段：

```c
struct ngx_command_s {
    ngx_str_t             name;
    ngx_uint_t            type;
    char               *(*set)(ngx_conf_t *cf, ngx_command_t *cmd, void *conf);
    ngx_uint_t            conf;       /* 选「哪一层」配置数组 */
    ngx_uint_t            offset;     /* 选「该层结构体里的哪个字段」 */
    void                 *post;       /* 可选：解析后的后处理回调或枚举表 */
};
```

定义见 [src/core/ngx_conf_file.h:77-84](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.h#L77-L84)。`conf` 与 `offset` 一前一后：`conf` 先从三层配置数组里选出本模块的结构体指针，`offset` 再在该结构体里定位字段。

#### 4.1.2 核心流程

一条「单参数 slot 指令」从文本到赋值的完整链路：

1. **词法器** `ngx_conf_read_token` 切出 token，`cf->args` 现在含 `[指令名, 参数]`。
2. **分发器** `ngx_conf_handler` 遍历模块表匹配到指令，校验参数个数（`FLAG` 要求恰好 2 个元素）、作用域。
3. **分发器计算 `conf` 指针**：根据 `cmd->type` 里是否含 `NGX_DIRECT_CONF` / `NGX_MAIN_CONF` 走不同分支，对 HTTP 模块走 `cmd->conf`（如 `NGX_HTTP_LOC_CONF_OFFSET`）取出本模块的 loc_conf 结构体指针。
4. **调用** `cmd->set(cf, cmd, conf)`，即某个通用 slot 函数。
5. **slot 函数内部**：`p = conf`，用 `p + cmd->offset` 定位字段，做「重复」哨兵判定，调用对应解析器（`ngx_atoi` / `ngx_parse_time` / …）写入字段，最后若有 `cmd->post` 再调后处理。

第 3 步的关键代码在分发器里：

```c
conf = NULL;

if (cmd->type & NGX_DIRECT_CONF) {
    conf = ((void **) cf->ctx)[cf->cycle->modules[i]->index];

} else if (cmd->type & NGX_MAIN_CONF) {
    conf = &(((void **) cf->ctx)[cf->cycle->modules[i]->index]);

} else if (cf->ctx) {
    confp = *(void **) ((char *) cf->ctx + cmd->conf);   /* 选层 */
    if (confp) {
        conf = confp[cf->cycle->modules[i]->ctx_index];   /* 选模块 */
    }
}

rv = cmd->set(cf, cmd, conf);                            /* 调 slot */
```

见 [src/core/ngx_conf_file.c:447-463](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L447-L463)。`else if (cf->ctx)` 分支就是绝大多数 HTTP 模块走的路径：`cmd->conf` 是 `offsetof(ngx_http_conf_ctx_t, loc_conf)`（在 [src/http/ngx_http_config.h:50-52](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_config.h#L50-L52) 定义），`(char *) cf->ctx + cmd->conf` 取到 `ctx->loc_conf`（指针数组），再用模块的 `ctx_index` 取到本模块的 loc_conf 结构体。

> 注意「选层」和「选字段」用的是**同一个 `offsetof` 思路**，只是选层作用在 `ngx_http_conf_ctx_t` 上（数组指针），选字段作用在本模块自己的 conf 结构体上。两者都靠「基址 + 编译期偏移」定位，没有任何字符串查找。

#### 4.1.3 源码精读

**(1) `ngx_conf_set_flag_slot`** —— 最经典的 slot，处理 `on` / `off`：

```c
char *
ngx_conf_set_flag_slot(ngx_conf_t *cf, ngx_command_t *cmd, void *conf)
{
    char  *p = conf;
    ngx_str_t        *value;
    ngx_flag_t       *fp;
    ngx_conf_post_t  *post;

    fp = (ngx_flag_t *) (p + cmd->offset);          /* 定位字段 */

    if (*fp != NGX_CONF_UNSET) {                    /* 同层重复检测 */
        return "is duplicate";
    }

    value = cf->args->elts;

    if (ngx_strcasecmp(value[1].data, (u_char *) "on") == 0) {
        *fp = 1;
    } else if (ngx_strcasecmp(value[1].data, (u_char *) "off") == 0) {
        *fp = 0;
    } else {
        /* ... 报错：必须是 on 或 off ... */
        return NGX_CONF_ERROR;
    }

    if (cmd->post) {                                /* 可选后处理 */
        post = cmd->post;
        return post->post_handler(cf, post, fp);
    }
    return NGX_CONF_OK;
}
```

见 [src/core/ngx_conf_file.c:1025-1062](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L1025-L1062)。三个要点：

- `fp = (ngx_flag_t *) (p + cmd->offset)` 是反射式定位；
- `*fp != NGX_CONF_UNSET` 判断「同作用域里是否已写过这条指令」，是则返回 `"is duplicate"`，这正是 nginx 报错 `directive is duplicate` 的来源；
- `cmd->post` 是可选的「解析后处理器」，下面 4.2 会看到它如何承载枚举表、数值范围校验等。

**(2) `ngx_conf_set_num_slot`** —— 整数版本，结构完全一致，只换了字段类型与解析器：

```c
np = (ngx_int_t *) (p + cmd->offset);
if (*np != NGX_CONF_UNSET) {
    return "is duplicate";
}
value = cf->args->elts;
*np = ngx_atoi(value[1].data, value[1].len);
if (*np == NGX_ERROR) {
    return "invalid number";
}
```

见 [src/core/ngx_conf_file.c:1166-1194](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L1166-L1194)。`ngx_atoi` 是 u2-l2 讲过的长度限定整数解析器。注意这里的「重复哨兵」用的是 `NGX_CONF_UNSET`（值 `-1`），因为字段类型是 `ngx_int_t`。

**(3) 哨兵值**。每种 C 类型有自己的「未设置」哨兵，定义在 [src/core/ngx_conf_file.h:56-60](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.h#L56-L60)：

| 哨兵宏 | 值 | 用于字段类型 |
| --- | --- | --- |
| `NGX_CONF_UNSET` | `-1` | `ngx_int_t` / `ngx_flag_t` / `time_t` |
| `NGX_CONF_UNSET_UINT` | `(ngx_uint_t)-1` | `ngx_uint_t` |
| `NGX_CONF_UNSET_PTR` | `(void *)-1` | 指针（如 `ngx_array_t *`） |
| `NGX_CONF_UNSET_SIZE` | `(size_t)-1` | `size_t` |
| `NGX_CONF_UNSET_MSEC` | `(ngx_msec_t)-1` | `ngx_msec_t` |

这些哨兵同时承担两个职责：① slot 函数据此判「是否重复」；② 后续 `merge` 阶段据此判「是否需要在父子配置间继承默认值」（u6 会详讲 `ngx_conf_merge_*` 宏）。所以模块的 `create_loc_conf` 必须把字段初始化成对应哨兵，slot 机制才能正常运转。

**(4) 一个真实例子**。HTTP 核心模块的 `variables_hash_max_size` 指令就是用 num slot 注册的：

```c
{ ngx_string("variables_hash_max_size"),
  NGX_HTTP_MAIN_CONF|NGX_CONF_TAKE1,
  ngx_conf_set_num_slot,
  NGX_HTTP_MAIN_CONF_OFFSET,
  offsetof(ngx_http_core_main_conf_t, variables_hash_max_size),
  NULL },
```

见 [src/http/ngx_http_core_module.c:184-190](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L184-L190)。`conf = NGX_HTTP_MAIN_CONF_OFFSET`（选 main 层数组），`offset = offsetof(..., variables_hash_max_size)`（选字段）。当用户写 `variables_hash_max_size 1024;` 时，分发器把 main_conf 结构体指针传给 `ngx_conf_set_num_slot`，后者 `+offset` 后用 `ngx_atoi("1024")` 写入。**整条链路没有任何为这条指令专门写的代码。**

#### 4.1.4 代码实践

**实践目标**：亲手把「文本 → offset → 字段」的反射链走一遍，验证 `offsetof` 如何消除重复代码。

**操作步骤**：

1. 在 `src/http/ngx_http_core_module.c` 的指令表里，数一数有多少条指令的 `set` 字段填的是 `ngx_conf_set_num_slot`、`ngx_conf_set_flag_slot`、`ngx_conf_set_msec_slot`。你会看到几十条都复用这几个函数。
2. 任选其中一条（例如 `client_header_timeout`，见 [src/http/ngx_http_core_module.c:233-239](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L233-L239)），找到它 `offset` 指向的结构体（`ngx_http_core_srv_conf_t`）与字段（`client_header_timeout`），确认该字段类型是 `ngx_msec_t`。
3. 在该模块的 `create_srv_conf` 里搜索这个字段，确认它被初始化为 `NGX_CONF_UNSET_MSEC`。这正是 slot 函数「重复判定」依赖的初值。
4. 在脑中（或纸上）追踪：用户写 `client_header_timeout 30s;`，`ngx_conf_set_msec_slot` 收到的 `conf` 指向哪个结构体？`p + cmd->offset` 落在哪个字段？写进去的最终值是多少毫秒？

**需要观察的现象 / 预期结果**：

- `client_header_timeout` 字段在 `create_srv_conf` 中初值为 `NGX_CONF_UNSET_MSEC`；
- 配置写 `30s` 后，`ngx_parse_time(&value[1], 0)` 返回 `30000`（毫秒），写入该字段；
- 若在同 `server{}` 里写两次 `client_header_timeout`，第二次会得到 `directive is duplicate` 报错（来自 slot 函数的 `*msp != NGX_CONF_UNSET_MSEC` 分支）。

> 第 4 步的精确毫秒值（`30000`）来自 u2-l2 讲过的 `ngx_parse_time` 语义，可在本机用一个小 C 程序调用验证；若不便编译，标注为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ngx_command_t` 里 `offset` 的类型是 `ngx_uint_t` 而不是某种「字段指针」？

**参考答案**：因为 slot 函数是**跨模块通用**的，它不可能知道每个模块结构体的具体类型，无法持有「指向某模块某字段的指针」。`offset` 只是一个「字节偏移量」，配合运行时拿到的 `conf` 基址（`p + offset`）才能定位字段。这正是 `offsetof` 解耦「定义点（指令表）」与「使用点（slot 函数」的关键。

**练习 2**：如果模块作者忘了在 `create_loc_conf` 里把某 `ngx_int_t` 字段初始化为 `NGX_CONF_UNSET`，会发生什么？

**参考答案**：该字段会是 0（`ngx_pcalloc` 清零）。于是 slot 函数里 `*np != NGX_CONF_UNSET` 为真，**第一次**写该指令就会被误判为「重复」，报 `directive is duplicate`，导致一条本应合法的指令无法使用。这解释了为什么 nginx 模块的 `create_*_conf` 总是先把所有可写字段设成对应哨兵。

---

### 4.2 各类型 slot 函数全表

#### 4.2.1 概念说明

上一节用 flag/num 讲清了 slot 的「反射骨架」，本节把其余 slot 函数一次过完。它们全都遵循同一个模板：

```
定位字段 (p + offset) → 重复判定 → 解析文本 → 写入 → 可选 post
```

差别只在三处：字段类型、解析器、「重复/初值」用哪个哨兵。下表把全部 slot 函数归到一起：

| slot 函数 | 期望参数 | 字段类型 | 解析器 | 「未设置」哨兵 |
| --- | --- | --- | --- | --- |
| `ngx_conf_set_flag_slot` | `on`/`off` | `ngx_flag_t` | `ngx_strcasecmp` | `NGX_CONF_UNSET` |
| `ngx_conf_set_str_slot` | 一个字符串 | `ngx_str_t` | 直接拷贝 `value[1]` | `field->data == NULL` |
| `ngx_conf_set_str_array_slot` | 一个字符串（可多次写） | `ngx_array_t *` | push 进数组 | `NGX_CONF_UNSET_PTR` |
| `ngx_conf_set_keyval_slot` | `key value` 两参数 | `ngx_array_t *`（`ngx_keyval_t`） | push 进数组 | `NGX_CONF_UNSET_PTR` 或 `NULL` |
| `ngx_conf_set_num_slot` | 整数 | `ngx_int_t` | `ngx_atoi` | `NGX_CONF_UNSET` |
| `ngx_conf_set_size_slot` | 带 K/M | `size_t` | `ngx_parse_size` | `NGX_CONF_UNSET_SIZE` |
| `ngx_conf_set_off_slot` | 带 K/M/G | `off_t` | `ngx_parse_offset` | `NGX_CONF_UNSET` |
| `ngx_conf_set_msec_slot` | 带时间单位 | `ngx_msec_t` | `ngx_parse_time(..., 0)` | `NGX_CONF_UNSET_MSEC` |
| `ngx_conf_set_sec_slot` | 带时间单位 | `time_t` | `ngx_parse_time(..., 1)` | `NGX_CONF_UNSET` |
| `ngx_conf_set_bufs_slot` | `num size` 两参数 | `ngx_bufs_t` | `ngx_atoi` + `ngx_parse_size` | `bufs->num == 0` |
| `ngx_conf_set_enum_slot` | 枚举名 | `ngx_uint_t` | 查 `cmd->post` 枚举表 | `NGX_CONF_UNSET_UINT` |
| `ngx_conf_set_bitmask_slot` | 多个掩码名 | `ngx_uint_t` | 查 `cmd->post` 掩码表，按位或 | 无（允许累加） |

声明集中放在 [src/core/ngx_conf_file.h:280-292](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.h#L280-L292)，实现在 [src/core/ngx_conf_file.c:1025-1431](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L1025-L1431)。

有两类需要特别说明：

- **msec vs sec**：两者都调 `ngx_parse_time`，区别在第二个参数 `is_sec`。`msec_slot` 传 `0`（返回毫秒，字段是 `ngx_msec_t`），`sec_slot` 传 `1`（返回秒，字段是 `time_t`）。这是 u2-l2 讲过的时间解析器在配置层的两副面孔。
- **str / str_array / keyval**：`str_slot` 是「单值字段，重复即报错」；`str_array_slot` 与 `keyval_slot` 则是「可多次写、每次追加进数组」，所以它们的「重复判定」逻辑反而不是报错，而是「数组还没建就先建」。

#### 4.2.2 核心流程

**str_slot**（单字符串）：

```c
field = (ngx_str_t *) (p + cmd->offset);
if (field->data) {              /* data 非 NULL 即视为已写过 */
    return "is duplicate";
}
value = cf->args->elts;
*field = value[1];              /* 浅拷贝：只复制 len/data 指针 */
```

见 [src/core/ngx_conf_file.c:1065-1089](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L1065-L1089)。注意它是**浅拷贝**：直接把 `value[1]`（一个 `ngx_str_t`，其 `data` 指向词法器分配的内存）赋给字段。由于配置解析期间分配的字符串生命周期与 cycle 同寿，这种浅拷贝是安全的。它的哨兵不是某个宏，而是「`data == NULL`」——`ngx_pcalloc` 出来的结构体字段天然为 `NULL`。

**keyval_slot**（键值对数组，如 `proxy_set_header` 那种 `key value` 指令的底层）：

```c
a = (ngx_array_t **) (p + cmd->offset);
if (*a == NGX_CONF_UNSET_PTR || *a == NULL) {
    *a = ngx_array_create(cf->pool, 4, sizeof(ngx_keyval_t));
    /* ... */
}
kv = ngx_array_push(*a);
value = cf->args->elts;
kv->key = value[1];
kv->value = value[2];
```

见 [src/core/ngx_conf_file.c:1128-1163](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L1128-L1163)。这里字段类型是 `ngx_array_t *`（二级指针），所以先 `*a` 解引用拿到数组指针，若为空就创建（容量 4、元素为 `ngx_keyval_t`），再 push 一个新元素，把 `value[1]`、`value[2]` 分别作为 key、value。多次写同名指令不会报「重复」，而是不断追加。

**msec_slot**（带单位的时间）：

```c
msp = (ngx_msec_t *) (p + cmd->offset);
if (*msp != NGX_CONF_UNSET_MSEC) { return "is duplicate"; }
value = cf->args->elts;
*msp = ngx_parse_time(&value[1], 0);   /* is_sec=0 → 毫秒 */
if (*msp == (ngx_msec_t) NGX_ERROR) { return "invalid value"; }
```

见 [src/core/ngx_conf_file.c:1259-1287](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L1259-L1287)。对照 `sec_slot`（[src/core/ngx_conf_file.c:1290-1318](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L1290-L1318)），唯一差别就是 `ngx_parse_time` 的 `is_sec` 参数 `0` vs `1`。

**enum_slot / bitmask_slot**——这两个把 `cmd->post` 当「数据表」用：

```c
/* enum_slot：post 指向 ngx_conf_enum_t[] */
np = (ngx_uint_t *) (p + cmd->offset);
if (*np != NGX_CONF_UNSET_UINT) { return "is duplicate"; }
e = cmd->post;
for (i = 0; e[i].name.len != 0; i++) {
    if (e[i].name.len != value[1].len
        || ngx_strcasecmp(e[i].name.data, value[1].data) != 0) {
        continue;
    }
    *np = e[i].value;
    return NGX_CONF_OK;
}
```

见 [src/core/ngx_conf_file.c:1351-1385](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L1351-L1385)。`post` 在别的 slot 里是「后处理函数」，在 enum/bitmask 里被复用为「名字→值」映射表（`ngx_conf_enum_t` / `ngx_conf_bitmask_t`，定义见 [src/core/ngx_conf_file.h:157-168](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.h#L157-L168)）。`bitmask_slot`（[src/core/ngx_conf_file.c:1388-1431](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L1388-L1431)）类似，但用 `|=` 累加多个位、且不做重复判定（同一个位重复写只给 warning）。

#### 4.2.3 源码精读：post 与范围校验

`cmd->post` 还能挂**范围校验器**，典型是 `ngx_conf_check_num_bounds`：

```c
char *
ngx_conf_check_num_bounds(ngx_conf_t *cf, void *post, void *data)
{
    ngx_conf_num_bounds_t  *bounds = post;
    ngx_int_t  *np = data;

    if (bounds->high == -1) {                  /* 只有下界 */
        if (*np >= bounds->low) { return NGX_CONF_OK; }
        /* ... 报错 ... */
        return NGX_CONF_ERROR;
    }
    if (*np >= bounds->low && *np <= bounds->high) {   /* 闭区间 */
        return NGX_CONF_OK;
    }
    /* ... 报错 ... */
    return NGX_CONF_ERROR;
}
```

见 [src/core/ngx_conf_file.c:1459-1486](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L1459-L1486)。用法是：指令表的 `post` 填一个 `ngx_conf_num_bounds_t`（带 `post_handler = ngx_conf_check_num_bounds`、`low`、`high`），`num_slot` 解析完整数后调用它做范围检查。`bounds->high == -1` 表示「只设下界」——注意这里 `-1` 不再是哨兵，而是约定的「无上界」标记，不要和 `NGX_CONF_UNSET` 混淆。

> 小结：slot 函数 = 「反射骨架（offset 定位）」+ 「类型相关解析器」+ 「可选 post（校验/枚举/掩码）」。掌握了骨架，剩下十几个函数都是查表。

#### 4.2.4 代码实践

**实践目标**：通过查源码，把任意一条指令对应到正确的 slot 函数与字段类型，验证「类型→哨兵→解析器」三者一致。

**操作步骤**：

1. 在 [src/http/ngx_http_core_module.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c) 中搜索 `ngx_conf_set_size_slot`，找到使用它的指令（例如 `client_body_buffer_size`、`large_client_header_buffers` 之类）。
2. 对每条指令，沿 `offset` 找到字段，用本节的「全表」反推：该字段类型、初值哨兵、解析器分别是什么。
3. 对 `msec` 与 `sec` 两条线各找一条指令，写出它对 `ngx_parse_time` 传的 `is_sec` 是 0 还是 1，并据此判断 `30s` 在两个字段里分别会被存成多少。

**预期结果**：

- `size_slot` 指令的字段是 `size_t`，初值 `NGX_CONF_UNSET_SIZE`，解析器 `ngx_parse_size`（`1k` → 1024）；
- `msec_slot` 指令 `is_sec=0`，`30s` → `30000`（毫秒）；`sec_slot` 指令 `is_sec=1`，`30s` → `30`（秒）。

> 若无法在本地编译运行，把第 3 步的换算结果标注「待本地验证」，但解析器选择（`is_sec` 取值）必须从源码确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ngx_conf_set_str_slot` 的「重复判定」用 `field->data` 是否为 `NULL`，而不是某个 `NGX_CONF_UNSET*` 哨兵？

**参考答案**：因为 `ngx_str_t` 没有天然的「非法」整数值可用作哨兵（`len`/`data` 都可能是合法值）。而 `ngx_pcalloc` 分配的配置结构体，其 `ngx_str_t` 字段 `data` 默认就是 `NULL`，于是 nginx 约定「`data == NULL` 即未设置」。这也意味着：用 `str_slot` 的字段，其默认值只能是「空字符串」，不能表达「默认是某个具体字符串」——后者需要模块自己在 `merge` 阶段补。

**练习 2**：`ngx_conf_set_keyval_slot` 为什么不在意「重复」，而是无限追加？

**参考答案**：因为它的语义是「键值对集合」（典型如一组 `proxy_set_header K V`），同一条指令在不同行写多次是正常用法，每行都是集合里的一个元素。这与 `str_slot`「单值字段、只能写一次」的语义相反，所以「重复」在这两类 slot 里有相反的处理。

---

### 4.3 ngx_inet 地址与 CIDR 解析

#### 4.3.1 概念说明

很多指令的参数是网络地址：`listen 127.0.0.1:8080;`、`allow 10.0.0.0/8;`、`deny 192.168.1.1;`、`proxy_pass http://upstream;`。文本地址要变成内核能用的形式，需要三类产出：

- **网络字节序的整数地址**（`in_addr_t`）——IPv4 是一个 32 位数；
- **CIDR 表示**（地址 + 掩码）——`ngx_cidr_t`，用于 `allow/deny`、`geo`；
- **完整的 `sockaddr`**——`ngx_addr_t` 里的 `sockaddr` + `socklen`，用于 `listen`、连接。

`src/core/ngx_inet.c` 提供了一条「由简到繁」的函数链。先看三个核心数据结构（定义在 [src/core/ngx_inet.h:47-106](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_inet.h#L47-L106)）：

```c
/* 单段 IPv4 CIDR：地址 + 掩码 */
typedef struct {
    in_addr_t  addr;
    in_addr_t  mask;
} ngx_in_cidr_t;

/* 通用 CIDR：靠 family 区分 v4/v6，u 是联合体 */
typedef struct {
    ngx_uint_t  family;
    union {
        ngx_in_cidr_t   in;
#if (NGX_HAVE_INET6)
        ngx_in6_cidr_t  in6;
#endif
    } u;
} ngx_cidr_t;

/* 一个带 sockaddr 的地址（listen、连接都用它） */
typedef struct {
    struct sockaddr  *sockaddr;
    socklen_t         socklen;
    ngx_str_t         name;
} ngx_addr_t;
```

`ngx_cidr_t` 是「标签联合（tagged union）」：`family` 是标签，`u` 按标签解释成 v4 或 v6。这种模式 nginx 里反复出现（模块的 `type`+`ctx` 也是）。`ngx_url_t`（[src/core/ngx_inet.h:81-106](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_inet.h#L81-L106)）更复杂，承载 `listen`/`proxy_pass` 这类「可能含主机名、端口、URI、多地址（DNS 解析后）」的场景，本讲只触及它的地址解析部分，完整 `ngx_parse_url` 留给后续 upstream 讲义。

#### 4.3.2 核心流程

**(a) `ngx_inet_addr`：IPv4 文本 → 网络字节序整数。** 这是整条链的地基。算法是状态机式的逐字符扫描：维护当前 `octet`（0–255）和点号计数 `n`，遇数字累乘 10，遇点号把当前 octet 左移并入 `addr`，结束时要求恰好 3 个点。关键两点：① 每个 octet 超过 255 即失败（返回 `INADDR_NONE`）；② 返回前用 `htonl` 转成网络字节序。

**(b) `ngx_ptocidr`：`"addr/prefix"` → `ngx_cidr_t`。** 流程：

1. 用 `ngx_strlchr` 找 `/`，把文本切成地址段和前缀段；
2. 先试 `ngx_inet_addr`（v4），失败再试 `ngx_inet6_addr`（v6）；
3. 没有 `/` → 掩码取全 1（`0xffffffff`）；
4. 有 `/` → `shift = ngx_atoi(前缀)`，按 family 生成掩码；
5. **规范化**：把地址里「掩码之外的位」清零，若发生了清零则返回 `NGX_DONE`（提示调用者：你给的地址有主机位，我帮你抹掉了）。

掩码计算（IPv4）：

\[ \text{mask} = \text{htonl}\!\left(0\text{x}ffffffff \ll (32 - \text{shift})\right) \]

例如 `/24`：`shift=24`，`0xffffffff << 8 = 0xffffff00`，`htonl` 后即 `255.255.255.0`。一个边界陷阱：`shift == 0` 时 `32 - shift == 32`，而 x86 的 `shl` 对 32 取模会把「左移 32」当成「左移 0」，得到 `0xffffffff` 而非 0，所以代码对 `shift==0` 单独返回 0（见 [src/core/ngx_inet.c:455-461](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_inet.c#L455-L461) 的注释）。

**(c) `ngx_cidr_match`：`sockaddr` 是否落在某个 CIDR 集合内。** 对集合里每个 `ngx_cidr_t`，先比 family，再用 `(inaddr & mask) == addr` 判定。IPv4 一行搞定；IPv6 逐字节比较 16 字节；还特判了「v4 映射到 v6」的地址（`::ffff:a.b.c.d`），把它拆回 v4 再比。

**(d) `ngx_parse_addr`：纯地址文本 → `ngx_addr_t`（含 `sockaddr`）。** 先试 v4 再试 v6，成功则从内存池分配 `sockaddr`、设 family、拷地址。**注意：它只解析地址，不解析端口**；带端口的 `1.2.3.4:80` 由 `ngx_parse_addr_port` 处理，更完整的 URL 由 `ngx_parse_url` 处理。

#### 4.3.3 源码精读

**`ngx_inet_addr`**（IPv4 解析，[src/core/ngx_inet.c:19-59](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_inet.c#L19-L59)）：

```c
addr = 0; octet = 0; n = 0;
for (p = text; p < text + len; p++) {
    c = *p;
    if (c >= '0' && c <= '9') {
        octet = octet * 10 + (c - '0');
        if (octet > 255) { return INADDR_NONE; }
        continue;
    }
    if (c == '.') {
        addr = (addr << 8) + octet;     /* 每段拼进高位 */
        octet = 0; n++;
        continue;
    }
    return INADDR_NONE;                 /* 非法字符 */
}
if (n == 3) {
    addr = (addr << 8) + octet;         /* 拼最后一段 */
    return htonl(addr);                 /* 转网络字节序 */
}
return INADDR_NONE;
```

要点：`addr` 是**主机字节序**累加（每段塞进低 8 位、整体左移），最后 `htonl` 一次转成网络序。`n==3` 严格要求 4 段、3 个点，所以 `1.2.3` 或 `1.2.3.4.5` 都会被拒。

**`ngx_ptocidr`**（CIDR 解析，[src/core/ngx_inet.c:374-471](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_inet.c#L374-L471)），IPv4 分支的关键几行：

```c
mask = ngx_strlchr(addr, last, '/');
len = (mask ? mask : last) - addr;
cidr->u.in.addr = ngx_inet_addr(addr, len);
/* ... family 判定、无掩码则全 1 ... */
shift = ngx_atoi(mask, last - mask);
/* ... */
if (shift) {
    cidr->u.in.mask = htonl((uint32_t) (0xffffffffu << (32 - shift)));
} else {
    /* x86 compilers use a shl instruction that shifts by modulo 32 */
    cidr->u.in.mask = 0;
}
if (cidr->u.in.addr == (cidr->u.in.addr & cidr->u.in.mask)) {
    return NGX_OK;                      /* 本就是网络地址 */
}
cidr->u.in.addr &= cidr->u.in.mask;     /* 抹掉主机位 */
return NGX_DONE;                        /* 告知：我帮你规范化了 */
```

`NGX_DONE` 的返回是给上层的一个「软提示」：用户写的可能是 `10.0.1.5/24`（带主机位），nginx 不会拒绝，但会把它规整成 `10.0.0.0/24`，并通过 `NGX_DONE` 让调用方有机会打一条 warning。这是 nginx「宽容输入、但明确告知」的典型风格。

**`ngx_cidr_match`**（CIDR 匹配，[src/core/ngx_inet.c:474-558](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_inet.c#L474-L558)），IPv4 核心判定：

```c
for (cidr = cidrs->elts, i = 0; i < cidrs->nelts; i++) {
    if (cidr[i].family != family) { goto next; }
    switch (family) {
    /* ... AF_INET6 / AF_UNIX ... */
    default: /* AF_INET */
        if ((inaddr & cidr[i].u.in.mask) != cidr[i].u.in.addr) {
            goto next;
        }
        break;
    }
    return NGX_OK;          /* 命中任一条即放行 */
next:
    continue;
}
return NGX_DECLINED;
```

`(inaddr & mask) == addr` 是 CIDR 匹配的本质：掩掉主机位后比较网络前缀。`allow`/`deny` 模块正是把配置里的每条 `allow 10.0.0.0/8;` 用 `ngx_ptocidr` 存成 `ngx_cidr_t`，请求到来时用 `ngx_cidr_match` 判客户端 IP 是否命中。

**`ngx_parse_addr`**（文本 → `sockaddr`，[src/core/ngx_inet.c:561-618](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_inet.c#L561-L618)）：

```c
inaddr = ngx_inet_addr(text, len);
if (inaddr != INADDR_NONE) {
    family = AF_INET;
    len = sizeof(struct sockaddr_in);
#if (NGX_HAVE_INET6)
} else if (ngx_inet6_addr(text, len, inaddr6.s6_addr) == NGX_OK) {
    family = AF_INET6;
    len = sizeof(struct sockaddr_in6);
#endif
} else {
    return NGX_DECLINED;            /* 都不认识 */
}
addr->sockaddr = ngx_pcalloc(pool, len);   /* 从内存池分配 */
addr->sockaddr->sa_family = (u_char) family;
addr->socklen = len;
/* 按 family 把地址拷进 sockaddr_in / sockaddr_in6 */
```

注意它返回 `NGX_DECLINED` 表示「不是地址」（可能是主机名），调用方据此决定是否走 DNS 解析路径——这正是 `ngx_parse_url` 的分流依据之一。

#### 4.3.4 代码实践

**实践目标**：把「`allow 10.0.0.0/8;` 这行文本最终如何拦截/放行一个客户端 IP」整条链跑通，体会 `ngx_ptocidr`（配置加载期）与 `ngx_cidr_match`（请求期）的分工。

**操作步骤**：

1. 打开 [src/http/modules/ngx_http_access_module.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_access_module.c)，找到 `allow`/`deny` 指令的 `set` 回调 `ngx_http_access_rule`。注意：它**不是**通用 slot 函数，而是自定义回调，因为 `allow` 的参数可能是 CIDR、`all` 或 Unix socket，需要自己分流。
2. 在该回调里搜索 `ngx_ptocidr`，确认配置加载期：文本 `10.0.0.0/8` → `ngx_cidr_t`，存进模块的规则数组。
3. 找到请求处理 handler `ngx_http_access_handler`，搜索 `ngx_cidr_match`，确认请求期：拿客户端 `r->connection->sockaddr` 去匹配规则数组。
4. 自行推演：客户端 IP `10.5.6.7` 在网络字节序下与 `mask=10.0.0.0 的 /8 掩码`做按位与，结果是否等于规则里的 `addr`？`192.168.1.1` 呢？
5. 用一个小 nginx.conf（`location / { allow 10.0.0.0/8; deny all; }`）+ `nginx -t` 验证配置被接受；用 `curl --interface` 或不同来源 IP 访问验证放行/拦截（这一步若环境不允许，标注「待本地验证」）。

**预期结果**：

- 配置加载期：`10.0.0.0/8` 经 `ngx_ptocidr` 得到 `addr=htonl(0x0a000000)`、`mask=htonl(0xff000000)`；
- 请求期：`10.5.6.7` → `inaddr & mask == 0x0a000000`，命中放行；`192.168.1.1` → `inaddr & mask == 0xc0a80000 != 0x0a000000`，不命中，落到 `deny all` 被拒。

#### 4.3.5 小练习与答案

**练习 1**：`ngx_ptocidr` 在地址带主机位时返回 `NGX_DONE` 而不是 `NGX_OK`，这个区分有什么用？

**参考答案**：`NGX_OK` 表示「输入原本就是规范网络地址」，`NGX_DONE` 表示「输入含主机位，我已帮你抹零」。调用方可据此给用户一条 warning（如 `CIDR ... has host bits set, ignoring` 之类），帮助发现配置笔误（比如本想写 `/24` 却把主机地址写进了规则）。两者都代表「解析成功」，区别仅在是否给提示。

**练习 2**：`ngx_parse_addr` 和 `ngx_ptocidr` 都先用 `ngx_inet_addr` 试 IPv4，失败再试 IPv6。为什么不反过来？

**参考答案**：因为现实里 IPv4 地址远多于 IPv6，先试高频路径能省掉大部分情况下对 `ngx_inet6_addr` 的调用；而且 IPv4 文本（`1.2.3.4`）不可能是合法 IPv6 文本，反过来 IPv6 文本（`::1`）也不是合法 IPv4 文本，两者互斥，先后顺序不影响正确性，只影响性能。这是「快路径优先」的常见取舍。

---

## 5. 综合实践

把本讲三块内容（slot 函数、`offsetof`、`ngx_inet`）串起来，完成下面这个「虚构模块指令表设计」任务。

**任务**：设计一个虚构的 `ngx_http_demo_module`，它在 location 级提供四条指令：

```nginx
location /demo {
    demo_enable      on;          # flag
    demo_max_count   100;         # num
    demo_timeout     30s;         # msec
    demo_backend     10.0.0.0/8;  # CIDR，自定义回调
}
```

**要求产出**：

1. 写出 `ngx_http_demo_loc_conf_t` 结构体（注意每个字段的 C 类型与 `create_loc_conf` 里的初值哨兵）。
2. 写出 `ngx_http_demo_commands[]` 指令表，前三条分别用 `ngx_conf_set_flag_slot` / `ngx_conf_set_num_slot` / `ngx_conf_set_msec_slot`，第四条用自定义回调（内部调 `ngx_ptocidr`）。
3. 对前三条指令，逐一说明：分发器算出的 `conf` 指向哪个结构体、`p + cmd->offset` 落在哪个字段、解析器把文本转成什么值写入。

**参考骨架（示例代码，非 nginx 原有代码）**：

```c
/* 1) 配置结构体 */
typedef struct {
    ngx_flag_t   enable;        /* flag_slot，初值 NGX_CONF_UNSET */
    ngx_int_t    max_count;     /* num_slot，初值 NGX_CONF_UNSET */
    ngx_msec_t   timeout;       /* msec_slot，初值 NGX_CONF_UNSET_MSEC */
    ngx_cidr_t   backend;       /* 自定义，初值由 family=AF_UNSPEC 标记未设 */
} ngx_http_demo_loc_conf_t;

/* 2) 指令表 */
static ngx_command_t  ngx_http_demo_commands[] = {
    { ngx_string("demo_enable"),
      NGX_HTTP_LOC_CONF|NGX_CONF_FLAG,
      ngx_conf_set_flag_slot,
      NGX_HTTP_LOC_CONF_OFFSET,
      offsetof(ngx_http_demo_loc_conf_t, enable),
      NULL },

    { ngx_string("demo_max_count"),
      NGX_HTTP_LOC_CONF|NGX_CONF_TAKE1,
      ngx_conf_set_num_slot,
      NGX_HTTP_LOC_CONF_OFFSET,
      offsetof(ngx_http_demo_loc_conf_t, max_count),
      NULL },

    { ngx_string("demo_timeout"),
      NGX_HTTP_LOC_CONF|NGX_CONF_TAKE1,
      ngx_conf_set_msec_slot,
      NGX_HTTP_LOC_CONF_OFFSET,
      offsetof(ngx_http_demo_loc_conf_t, timeout),
      NULL },

    { ngx_string("demo_backend"),
      NGX_HTTP_LOC_CONF|NGX_CONF_TAKE1,
      ngx_http_demo_set_backend,      /* 自定义：内部 ngx_ptocidr */
      NGX_HTTP_LOC_CONF_OFFSET,
      offsetof(ngx_http_demo_loc_conf_t, backend),
      NULL },

    ngx_null_command
};
```

**3) offset 落点追踪**（前三条）：

| 指令 | `conf` 指向 | `p+offset` 落在 | 解析器 | 写入值 |
| --- | --- | --- | --- | --- |
| `demo_enable on` | 本 location 的 demo loc_conf | `enable` 字段 | `ngx_strcasecmp("on")` | `enable = 1` |
| `demo_max_count 100` | 同上 | `max_count` 字段 | `ngx_atoi("100")` | `max_count = 100` |
| `demo_timeout 30s` | 同上 | `timeout` 字段 | `ngx_parse_time("30s", 0)` | `timeout = 30000`（毫秒） |

完成后再回答一个拔高问题：为什么 `demo_backend` 不能直接用某个通用 slot 函数，必须自定义回调？——因为通用 slot 函数里没有「CIDR」这一类，地址族解析（`ngx_ptocidr`）属于业务相关逻辑，需要模块自己写，这与 `allow`/`deny` 用自定义 `ngx_http_access_rule` 是同一道理。

## 6. 本讲小结

- 通用 slot 函数（`ngx_conf_set_flag_slot` 等）用 `offsetof` 把「字段位置」在编译期固化进指令表的 `offset` 字段，运行时 `p + cmd->offset` 反射式定位，从而用一套代码服务成百上千条「单参数写一个字段」的指令。
- 分发器 `ngx_conf_handler` 先用 `cmd->conf`（如 `NGX_HTTP_LOC_CONF_OFFSET`）从三层配置数组选出本模块结构体，再交给 slot 函数用 `offset` 选字段；选层和选字段都是「基址 + 编译期偏移」。
- 每种 slot 函数对应一种 C 类型、一种解析器、一种「未设置」哨兵；`create_*_conf` 必须把字段初始化成对应哨兵，slot 的「重复判定」与后续 `merge` 都依赖它。
- `msec_slot` 与 `sec_slot` 的唯一区别是 `ngx_parse_time` 的 `is_sec` 参数（0 返回毫秒、1 返回秒）；`str_array_slot`/`keyval_slot` 是「累加型」，与「单值型」`str_slot` 的重复处理相反；`enum`/`bitmask` 把 `cmd->post` 当作「名字→值」表。
- `ngx_inet_addr` 用逐字符状态机把 IPv4 文本转成网络字节序整数，是地址解析的地基；`ngx_ptocidr` 在其上拼出掩码并规范化地址（带主机位返回 `NGX_DONE`）；`ngx_cidr_match` 用 `(addr & mask) == net` 做 CIDR 命中判定。
- `ngx_parse_addr` 把纯地址文本变成带 `sockaddr` 的 `ngx_addr_t`（不含端口），它是 `ngx_parse_url` 的子步骤；带端口/主机名的完整解析由后续 upstream 讲义展开。

## 7. 下一步学习建议

本讲把「指令值如何落到字段」和「地址如何解析」补齐，建议接下来：

1. **u3-l2（cycle 生命周期）**：看 slot 写进 conf 结构体后，`ngx_init_cycle` 如何把 main/srv/loc 三层配置装配起来，以及 `create_conf`/`init_conf` 回调如何与 slot 配合（本讲的哨兵值在 `init_conf` 阶段被 `ngx_conf_init_*` 补默认）。
2. **u6（HTTP 核心处理）**：重点看 `merge_loc_conf` 机制——本讲反复提到的「哨兵 → 父子配置继承默认值」在那里完整展开，你会看到 `ngx_conf_merge_*` 宏如何消费这些哨兵。
3. **u10-l2（访问控制与限流）**：把本讲 4.3 的 `ngx_ptocidr` / `ngx_cidr_match` 放进 `allow`/`deny` 模块的真实上下文里，看请求期匹配如何返回 `NGX_OK`/`NGX_DECLINED`/`NGX_HTTP_403`。
4. **u7（upstream）**：本讲刻意没展开的 `ngx_parse_url` / `ngx_url_t`（含端口、主机名、DNS 解析、多地址）在那里是 `proxy_pass`、`upstream {}` 块的核心，是 `ngx_parse_addr` 的「完整版」。
