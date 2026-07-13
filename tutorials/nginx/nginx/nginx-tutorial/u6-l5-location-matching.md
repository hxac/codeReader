# location 匹配与配置合并

## 1. 本讲目标

本讲是 HTTP 核心处理的第五篇。上一篇我们讲完了请求的 11 个 phase 阶段，知道请求会按固定顺序流过 `POST_READ → ... → FIND_CONFIG → ... → CONTENT`。本讲就钻进其中最关键的 `FIND_CONFIG` 阶段，回答两个核心问题：

1. nginx 收到一个 URI 后，到底按什么规则从几十个 `location {}` 里挑出「那一个」来处理？
2. 一个嵌套的 `location` 里没有显式写的指令（比如没写 `root`），它的值是从哪里来的？

学完本讲，读者应该能够：

- 说清 `= / ^~ / ~ / ~* / @` 五种 location 修饰符的含义，以及「精确 > 前缀（最长） > 正则」这条匹配优先级链；
- 看懂 `ngx_http_core_find_location` 与 `ngx_http_core_find_static_location` 的源码，理解静态前缀树如何把 O(n) 的逐个比对优化成 O(log n) 的二叉搜索；
- 理解 named location（`@name`）为何只能在 server 层定义、又如何通过内部重定向被使用；
- 理解 `merge_loc_conf` 回调如何沿配置树自顶向下继承默认值，让嵌套 location 不写指令也能正确工作。

## 2. 前置知识

本讲假设你已经掌握（详见前置讲义）：

- **三层配置结构**（u6-l1）：HTTP 配置被组织成 `main / srv / loc` 三层指针数组，每层按模块的 `ctx_index` 索引；每个 `location {}` 都对应一份独立的 `loc_conf` 数组。
- **指令描述符与 slot**（u3-l1、u3-l4）：每条指令的 `set` 回调（通常是 `ngx_conf_set_*_slot`）借助 `offsetof` 把值写进配置结构体的固定字段，未设置时字段初值为 `NGX_CONF_UNSET` 哨兵。
- **phase 机制**（u6-l4）：请求按 `phase_engine` 一维数组顺序流过各 handler；`FIND_CONFIG` 阶段只有一个框架 handler，职责就是「为当前 URI 选 location」。

几个本讲反复用到的小概念：

- **`ngx_filename_cmp`**：nginx 自己写的字节比较函数，语义同 `memcmp`，但只比较指定长度，返回值的正负表示字典序大小。location 前缀树正是靠它做二叉搜索。
- **`r->loc_conf`**：请求对象上的一个「配置指针数组」，`FIND_CONFIG` 阶段选出某个 location 后，就把它的 `clcf->loc_conf` 整个赋给 `r->loc_conf`，此后所有模块通过 `ngx_http_get_module_loc_conf(r, ...)` 拿到的都是这个 location 的配置。换 location 就是换这整个数组。
- **`loc_conf` 与 `locations`**：每个 location 持有一份 `loc_conf`（自己的配置），同时持有一个 `locations` 队列（它的子 location 列表）。这二者构成一棵「配置树」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/http/ngx_http_core_module.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.h) | 定义 `ngx_http_core_loc_conf_s`（location 配置结构体）、`ngx_http_location_queue_t`（配置期的 location 队列节点）、`ngx_http_location_tree_node_s`（运行期静态前缀树的节点）。 |
| [src/http/ngx_http_core_module.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c) | location 指令的解析函数 `ngx_http_core_location`、运行期匹配 `ngx_http_core_find_location` / `ngx_http_core_find_static_location`、`FIND_CONFIG` 阶段 handler `ngx_http_core_find_config_phase`、named location 重定向 `ngx_http_named_location`、合并函数 `ngx_http_core_merge_loc_conf`。 |
| [src/http/ngx_http.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c) | 配置期对 location 列表做排序、分类（静态/正则/named）、构建静态树的 `ngx_http_init_locations` / `ngx_http_init_static_location_trees` / `ngx_http_cmp_locations`，以及沿树合并配置的 `ngx_http_merge_servers` / `ngx_http_merge_locations`。 |

一句话地图：**配置期**在 `ngx_http.c` 里把乱序的 location 列表整理成一棵静态前缀树 + 一个正则数组 + 一个 named 数组；**运行期**在 `ngx_http_core_module.c` 里用 `find_location` 指挥三套数据结构按优先级匹配。

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：① 配置期解析五种修饰符；② 运行期匹配总调度与优先级；③ 静态前缀树的二叉搜索；④ named location 与内部重定向；⑤ 配置合并 `merge_loc_conf` 沿树继承。

### 4.1 location 的种类与配置期解析

#### 4.1.1 概念说明

nginx 的 `location` 指令不是只有「前缀匹配」一种写法。根据 URI 与 location 名字的关系，nginx 把 location 分成五类，由 location 名字前的「修饰符」决定：

| 修饰符 | 名称 | 含义 |
| --- | --- | --- |
| `=` | 精确匹配（exact） | URI 必须**完全等于** location 名字才命中，命中即停止后续一切匹配。 |
| `^~` | 优先前缀（prefix, no-regex） | 前缀匹配；一旦命中则**跳过后续正则**匹配。 |
| `~` | 区分大小写正则（regex） | 用正则匹配 URI。 |
| `~*` | 不区分大小写正则（regex） | 同上但忽略大小写。 |
| （无修饰符） | 普通前缀（inclusive） | 前缀匹配；命中后仍可能被后续正则覆盖。 |
| `@` | 命名 location（named） | 不参与 URI 匹配，只能由内部重定向（如 `error_page`、`try_files`）跳转进入。 |

注意：`^~` 和普通前缀在「能否被正则覆盖」上不同，这正好是后续匹配优先级的关键。

#### 4.1.2 核心流程

配置解析时，`location` 是一个块指令（`BLOCK`），它的 `set` 回调是 `ngx_http_core_location`。该函数做四件事：

1. 为这个 location 新建一份 `loc_conf` 数组（遍历所有 HTTP 模块调 `create_loc_conf`）；
2. 解析修饰符，设置 `clcf` 上的几个 1 位标志：`exact_match`（`=`）、`noregex`（`^~`）、`regex`（`~`/`~*`）、`named`（`@`）；
3. 把自己挂到父 location 的 `locations` 队列上；
4. 递归调用 `ngx_conf_parse` 解析块体内的子指令（包括嵌套的 `location`）。

#### 4.1.3 源码精读

修饰符的判定分两种写法支持：`location = /uri`（两个参数）和 `location =/uri`（一个参数，修饰符贴在名字前）。下面是两参数分支的核心，逐字符判定修饰符并设置标志位：

[nginx/src/http/ngx_http_core_module.c:3170-3202](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L3170-L3202) —— 解析 `=`、`^~`、`~`、`~*` 四种修饰符，分别置 `exact_match`、`noregex`，或编译正则。

其中 `=` 分支只置 `clcf->exact_match = 1`；`^~` 分支只置 `clcf->noregex = 1`（注意 `noregex` 的含义是「匹配到这个前缀后不再走正则」，这正是 `^~` 的语义）；`~` / `~*` 都调用 `ngx_http_core_regex_location` 编译 PCRE 正则并存入 `clcf->regex`。

而 named location（`@name`）则在没有修饰符的分支里被识别：

[nginx/src/http/ngx_http_core_module.c:3240-3247](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L3240-L3247) —— 名字以 `@` 开头则置 `clcf->named = 1`。

这五个标志位定义在结构体里，都是 1 位字段：

[nginx/src/http/ngx_http_core_module.h:320-327](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.h#L320-L327) —— `noname`、`named`、`exact_match`、`noregex`、`auto_redirect` 等位域标志。

`noname`（无名字）这个标志读者会眼生，它代表「if 块或 limit_except 产生的隐式 location」，本讲不展开，只需知道它在排序时会被甩到队尾。

#### 4.1.4 代码实践

1. **实践目标**：通过故意写错修饰符，观察 nginx 的报错，确认修饰符的合法集合。
2. **操作步骤**：写一个最小 `nginx.conf`，在 `http { server { location =^ /x {} } }` 中故意写一个非法修饰符 `=^`；执行 `objs/nginx -p $(pwd)/ -c nginx.conf -t`。
3. **需要观察的现象**：终端应打印 `invalid location modifier "=^"`。
4. **预期结果**：报错信息正是来自上文 [ngx_http_core_module.c:3199-3201](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L3199-L3201) 的 `ngx_conf_log_error`。
5. 若手头没有编译好的二进制，可标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`location ^~ /api {}` 和 `location /api {}` 在配置结构体上唯一的区别是哪个字段？

**答案**：前者置了 `clcf->noregex = 1`，后者没有。其余（`name`、`loc_conf` 等）完全相同。

**练习 2**：为什么 `@fallback` 这种 named location 不能写 `location /outer { location @x {} }` 嵌套？

**答案**：`ngx_http_core_location` 在嵌套校验里，若 `clcf->named` 为真则直接报错 `named location "..." can be on the server level only`（见 [ngx_http_core_module.c:3276-3282](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L3276-L3282)）；同理 named location 内部也不能再嵌套 location。

### 4.2 运行期匹配总调度与匹配优先级

#### 4.2.1 概念说明

很多人背过「nginx location 匹配顺序 = 精确 > 前缀最长 > 正则」，但容易忽略两点：一是这个「顺序」不是一条简单 if 链，而是 **`find_location` 在三套数据结构之间分阶段决策**的结果；二是 `^~` 并不提高前缀匹配的「优先级」，它只是命中后关掉正则这一关。本模块讲清这个调度逻辑。

#### 4.2.2 核心流程

请求走到 `FIND_CONFIG` 阶段，handler `ngx_http_core_find_config_phase` 调用 `ngx_http_core_find_location(r)`。后者的决策流程（伪代码）：

```
find_location(r):
    rc = find_static_location(r, 当前location的 static_locations 树)
    # static_location 树同时承载 = 精确与普通前缀两类
    if rc == NGX_AGAIN:        # 命中了一个前缀（非精确），但还可能往下嵌套
        noregex = 当前location.noregex   # 记下 ^~ 标志
        rc = find_location(r)            # 递归进子 location 树再找
    if rc == NGX_OK or NGX_DONE:         # 精确命中或自动重定向
        return rc
    # 到这里说明前缀没命中精确、最多只是前缀
    if 不是 noregex 且 存在 regex_locations:
        for 每个正则 location（配置时的书写顺序）:
            if 正则匹配 r->uri:
                r->loc_conf = 该正则location.loc_conf
                再递归 find_location 进它的子树
                return NGX_OK
    return rc
```

关键点：**前缀树里先找，找到精确（`NGX_OK`）或自动重定向（`NGX_DONE`）就直接返回、根本不碰正则**；只有当前缀结果是「包容性前缀」（`NGX_AGAIN`）或「没匹配」（`NGX_DECLINED`）时，才轮到正则数组按书写顺序逐个试。而 `^~` 的作用体现在 `noregex` 变量：只要当前命中的前缀 location 带了 `noregex`，正则分支就被整体跳过。

#### 4.2.3 源码精读

总调度函数本体，注意它对 `NGX_AGAIN` 的特殊处理和对 `noregex` 的读取：

[nginx/src/http/ngx_http_core_module.c:1436-1502](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L1436-L1502) —— `ngx_http_core_find_location`。

逐段读：

- 第 1450 行先在静态树里找。
- 第 1452-1463 行：若静态树返回 `NGX_AGAIN`（命中了一个前缀但不是精确），先记录下这个 location 的 `noregex` 标志，然后**递归** `find_location` 进它的子树——这就是嵌套 location 的查找。
- 第 1465-1467 行：精确命中（`OK`）或自动重定向（`DONE`）直接返回，**不再看正则**。
- 第 1471-1498 行：正则分支。条件是 `noregex == 0 && pclcf->regex_locations`，即当前 level 没有 `^~` 拦截、且存在正则 location。正则按数组顺序（即配置文件里的书写顺序）逐个匹配，第一个命中的胜出。

调用方 `ngx_http_core_find_config_phase` 拿到结果后，把选中的 location 的 `loc_conf` 已经由 `find_static_location` 写进 `r->loc_conf`（或由正则分支写入），随后调用 `ngx_http_update_location_config` 让请求的运行时字段（如 `limit_except`、sendfile、error_log）跟上新 location：

[nginx/src/http/ngx_http_core_module.c:981-1000](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L981-L1000) —— `FIND_CONFIG` 阶段 handler 调用 `find_location` 后再 `ngx_http_update_location_config`。

`r->internal` 与 `clcf->internal` 的校验（第 990-993 行）保证标了 `internal` 的 location 只能被内部重定向访问，外部直接请求 URI 命中会得到 404。

#### 4.2.4 代码实践

1. **实践目标**：验证「前缀命中精确时根本不评估正则」。
2. **操作步骤**：写如下配置，其中正则 `~ \.gif$` 看似会拦截 `.gif`，但精确 `= /a.gif` 应优先：

   ```nginx
   # 示例代码：nginx.conf 片段
   events {}
   http {
       server {
           listen 8080;
           location = /a.gif { return 200 "exact\n"; }
           location ~ \.gif$ { return 200 "regex\n"; }
       }
   }
   ```

3. **需要观察的现象**：`curl http://127.0.0.1:8080/a.gif` 返回 `exact`。
4. **预期结果**：因 `find_static_location` 在静态树里直接命中精确返回 `NGX_OK`，正则分支根本不进入。
5. 若无运行环境，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果配置里只有 `location /api {}`（普通前缀）和 `location ~ ^/api {}`（正则），请求 `/api` 会命中哪个？

**答案**：命中正则 `^/api`。因为普通前缀只是「包容性匹配」（`NGX_AGAIN`），不挡正则，于是流程进入正则分支，第一个匹配的正则胜出。要让前缀胜出，需改成 `location ^~ /api {}`。

**练习 2**：`find_location` 为什么是递归的，递归的终止条件是什么？

**答案**：因为 location 可嵌套，子 location 有自己的 `static_locations` 树；递归进子树查找更精确的匹配。终止于：静态树返回 `NGX_OK`（精确命中，第 1465 行直接返回上层）；或整棵树都走完仍只是前缀/无匹配，落到正则分支后返回。

### 4.3 静态前缀树的二叉搜索 ngx_http_core_find_static_location

#### 4.3.1 概念说明

如果一个 server 下有几百个前缀 location，运行时逐个 `strncmp` 是 O(n) 的。nginx 在配置期把所有「静态 location」（即 `=`、`^~`、普通前缀三类，不含正则与 named）按名字整理成一棵**二叉搜索树**，运行时用 `ngx_filename_cmp` 做比较、O(log n) 地下行，再配合一个「包容性子树（`tree`）」指针处理嵌套前缀。这就是 `ngx_http_core_find_static_location` 的作用。

注意这里「静态」二字指的是「名字在配置期就固定、可直接比较」，与「静态文件」无关。

#### 4.3.2 核心流程

节点的结构很关键（见源码地图引用），它同时挂了 `exact` 与 `inclusive` 两个指针，外加 `left/right/tree` 三个子树指针。搜索主循环：

```
find_static_location(r, node):
    rv = NGX_DECLINED          # 默认无匹配
    while node != NULL:
        n = min(len(r->uri), node->len)        # 取较短长度作比较
        rc = ngx_filename_cmp(uri, node->name, n)
        if rc != 0:            # 不相等
            node = (rc < 0) ? node->left : node->right   # 二叉搜索下行
            continue
        # 前 n 字节相等
        if len(uri) > node->len:        # URI 比节点名字长（是前缀）
            if node->inclusive:         # 该节点是个包容性前缀 location
                r->loc_conf = node->inclusive->loc_conf
                rv = NGX_AGAIN          # 记下「至少命中了一个前缀」
                node = node->tree       # 进包容性子树继续找更长前缀
                uri += n; len -= n      # 吃掉已匹配前缀
                continue
            else:                       # 该节点只有精确版本，URI 更长不可能精确
                node = node->right
                continue
        if len(uri) == node->len:       # 长度也相等
            if node->exact:             # 有精确（=）版本 → 精确命中
                r->loc_conf = node->exact->loc_conf
                return NGX_OK
            else:                       # 无精确版但有包容版（普通前缀完全匹配）
                r->loc_conf = node->inclusive->loc_conf
                return NGX_AGAIN
        # len(uri) < node->len：URI 比节点名字短
        if len(uri)+1 == node->len and node->auto_redirect:
            r->loc_conf = exact ?: inclusive
            rv = NGX_DONE               # 触发补斜杠 301 重定向
        node = node->left
    return rv
```

返回码语义在源码注释里写得很清楚：

[nginx/src/http/ngx_http_core_module.c:1505-1510](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L1505-L1510) —— `NGX_OK` 精确、`NGX_DONE` 自动重定向、`NGX_AGAIN` 包容匹配、`NGX_DECLINED` 无匹配。

#### 4.3.3 源码精读

树节点结构定义：

[nginx/src/http/ngx_http_core_module.h:473-484](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.h#L473-L484) —— `left/right` 是同层二叉搜索的左右子树，`tree` 是嵌套前缀的包容性子树；`exact`/`inclusive` 分别指向「同名的精确 location」与「同名的包容 location」（一个节点可同时承载两者）。

搜索主循环本体：

[nginx/src/http/ngx_http_core_module.c:1525-1589](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L1525-L1589) —— `find_static_location` 主循环。

读这段有两个细节最值得注意：

1. **「最长前缀」是如何得到的**（1545-1557 行）：当 URI 比节点名字长且节点是包容性前缀时，记 `rv = NGX_AGAIN` 并进入 `node->tree` 子树继续找。子树里存的是「以当前前缀为前缀」的更长 location，于是天然实现了「在所有命中的前缀里取最长」。注意 `uri += n; len -= n` 把已匹配前缀「吃掉」，子树里的节点名就不再包含这段前缀，节省了重复比较。
2. **补斜杠重定向**（1580-1585 行）：当 `len(uri)+1 == node->len` 且节点带 `auto_redirect`，例如请求 `/images` 命中 location `/images/`（差一个斜杠），返回 `NGX_DONE`，由上层 `find_config_phase` 生成 301 重定向到补了斜杠的地址。

`auto_redirect` 标志不是普通 location 默认就有的，它由需要补斜杠语义的 handler 主动设置，例如 `proxy_pass` 所在模块：

[nginx/src/http/modules/ngx_http_proxy_module.c:4317](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_proxy_module.c#L4317) —— `proxy_pass` 解析时若发现需要补斜杠，置 `clcf->auto_redirect = 1`。

#### 4.3.4 代码实践

1. **实践目标**：在源码里定位「最长前缀」与「补斜杠」两条逻辑，并用配置验证。
2. **操作步骤**：
   - 在 [ngx_http_core_module.c:1545-1557](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L1545-L1557) 处确认「进 `tree` 子树找更长前缀」的代码。
   - 用如下配置（示例代码）测试：

     ```nginx
     location /a      { return 200 "a\n"; }
     location /a/b    { return 200 "ab\n"; }
     ```

     `curl /a/b` 应命中 `/a/b`（最长前缀），`curl /a/c` 命中 `/a`。
3. **需要观察的现象**：两次请求分别返回 `ab` 与 `a`。
4. **预期结果**：证实「在命中的多个前缀中取最长」。
5. 若无运行环境，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：树节点的 `exact` 和 `inclusive` 两个指针为什么可能同时非空？

**答案**：配置里可以同时写 `location /api {}`（包容）和 `location = /api {}`（精确），它们名字相同。配置期 `ngx_http_join_exact_locations` 会把同名节点合并到同一个树节点，分别挂到 `inclusive` 和 `exact`。运行时按 URI 长度决定用哪个：长度完全相等走 `exact`，URI 更长走 `inclusive`。

**练习 2**：第 1561 行 `node = node->right;`（`exact only` 分支）为什么往右走而不是结束？

**答案**：URI 比节点名字长、且该节点没有包容版本（只有精确），说明当前节点不可能命中，但二叉搜索树里右侧可能还有名字更长、能成为 URI 前缀的节点，所以按字典序继续往右下行而非立即退出。

### 4.4 named location 与内部重定向

#### 4.4.1 概念说明

named location 写成 `location @name {}`，它**不参与 URI 匹配**，配置期被单独收集进 server 级的 `named_locations` 数组，运行期只能由内部重定向跳入。它最常见的用途是配合 `error_page` 或 `try_files` 做集中兜底，例如：

```nginx
error_page 404 = @fallback;
location @fallback { proxy_pass http://backend; }
```

#### 4.4.2 核心流程

- 配置期：`ngx_http_init_locations` 在排序后扫描 location 队列，把 `clcf->named` 为真的项收集进 `cscf->named_locations` 数组（以 NULL 结尾），并从主队列里拆出去，使其不进入静态树。
- 运行期：`ngx_http_named_location(r, name)` 线性遍历 `named_locations` 数组，按名字精确匹配；命中后把 `r->loc_conf` 换成该 location 的配置，重置 phase 游标到 `location_rewrite_index`（即重新从 `SERVER_REWRITE` 之后重跑），实现「内部重定向」。

#### 4.4.3 源码精读

配置期收集 named location：

[nginx/src/http/ngx_http.c:725-765](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L725-L765) —— 命中 `clcf->named` 的项计数并记录起点，随后拆队、拷贝进 `cscf->named_locations`。

运行期按名字查找并执行内部重定向：

[nginx/src/http/ngx_http_core_module.c:2665-2700](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L2665-L2700) —— 遍历 `named_locations`，命中即换 `loc_conf`、清模块上下文、把 phase 游标拨回 `location_rewrite_index` 再 `ngx_http_core_run_phases`。

注意第 2694 行的 `location_rewrite_index`：这是 phase engine 里专门记录的「重写阶段起点」下标（见 u6-l4），内部重定向后请求从此处重跑 phases，于是 `FIND_CONFIG` 会再被执行一次，等于换了个 location 重新走流程。循环保护由 `r->uri_changes--`（第 2644 行）实现，超过 10 次重定向报「redirection cycle」。

#### 4.4.4 代码实践

1. **实践目标**：用 named location 做一次 404 兜底，并确认它真的不被 URI 直接匹配。
2. **操作步骤**（示例代码）：

   ```nginx
   server {
       listen 8080;
       location / {
           try_files $uri @fb;
       }
       location @fb {
           return 200 "from fallback\n";
       }
   }
   ```

3. **需要观察的现象**：`curl /notexist` 返回 `from fallback`；直接 `curl /@fb` 不会命中 named location（会走 `/` 并再次兜底）。
4. **预期结果**：证实 named location 只能内部进入。
5. 若无运行环境，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习**：为什么 named location 只能定义在 server 层、却能在任意 location 里用 `error_page @x` 跳转？

**答案**：定义位置受限是因为 `ngx_http_core_location` 在嵌套校验里禁止 named 嵌套（见 4.1.5）；能被任意地方跳转，是因为 `ngx_http_named_location` 通过 `ngx_http_get_module_srv_conf` 取的是**当前 server** 的 `named_locations`，属于 server 级全局资源，与触发点所在的 location 无关。

### 4.5 配置合并 merge_loc_conf 沿树继承

#### 4.5.1 概念说明

前面四个模块讲的是「选 location」，本模块讲「选出的 location 里那些没写明的指令值从哪来」。答案是从父级沿配置树继承——这就是 merge 机制。

回忆 u6-l1：每个 location 的 `loc_conf` 在 `create_loc_conf` 时所有字段都是 `NGX_CONF_UNSET` 哨兵；用户没写的指令就保持哨兵。merge 阶段逐字段判断：若子仍是哨兵，就取父的值；若父也是哨兵，再取硬编码默认值。

#### 4.5.2 核心流程

merge 沿「http → server → location → 嵌套 location」这棵树自顶向下递归。对每个 HTTP 模块，nginx 调用它注册的 `merge_loc_conf(parent, child)` 回调：

```
merge_servers(模块 m):
    for 每个 server s:
        m.merge_loc_conf(http层loc_conf, server层loc_conf)        # http→server
        merge_locations(server.locations, server层loc_conf, m)    # 进 location 树

merge_locations(queue, parent_loc_conf, m):
    for queue 里每个 location l:
        m.merge_loc_conf(parent_loc_conf, l.loc_conf)             # 父→子
        merge_locations(l.locations, l.loc_conf, m)               # 递归进子 location
```

核心思想：**父的地址作为 `parent` 一路下传，子的 `loc_conf` 作 `child`，每层都让模块自己决定怎么合并**。

#### 4.5.3 源码精读

沿 location 树递归合并的骨架：

[nginx/src/http/ngx_http.c:642-662](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L642-L662) —— `ngx_http_merge_locations` 对每个 location 调 `module->merge_loc_conf(parent, child)`，再递归进它的子 location。注意 `loc_conf`（父指针数组）作为参数层层下传。

外层驱动，先合并 server 层再进 location 树：

[nginx/src/http/ngx_http.c:592-613](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L592-L613) —— `ngx_http_merge_servers`：先 `merge_srv_conf`、再 `merge_loc_conf`（http→server），最后 `merge_locations` 进 location 树。

以核心模块的 `merge_loc_conf` 看一个具体字段的合并（`root`/`alias`，决定静态文件根目录）：

[nginx/src/http/ngx_http_core_module.c:3766-3780](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L3766-L3780) —— 若 `conf->root.data == NULL`（子未设置，哨兵），就继承父的 `root/alias/root_lengths/root_values`；若父也没设，才用硬编码默认 `"html"` 并解析成绝对路径。

这就是为什么内层 location 不写 `root` 也能正确返回静态文件——它继承了外层的 `root`。其余数值类字段普遍用 `ngx_conf_merge_*_value` 宏，写法是 `ngx_conf_merge_value(conf, prev, default)`：`conf` 非哨兵用 `conf`，否则用 `prev`，`prev` 也哨兵则用 `default`。一个例子：

[nginx/src/http/ngx_http_core_module.c:3786-3790](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L3786-L3790) —— `types_hash_max_size` 合并，三参数：子→父→默认 1024。

> 小结：`merge_loc_conf` 是「每个模块各自负责自己字段的继承策略」的回调；nginx 框架只负责沿树把 parent/child 成对喂给它。

#### 4.5.4 代码实践

1. **实践目标**：通过让内层 location 不写 `root`，验证它确实继承了外层 `root`。
2. **操作步骤**（示例代码）：

   ```nginx
   server {
       root /var/www;                 # server 层设 root
       location /a/ {
           # 故意不写 root
           return 200 "matched a\n";
       }
   }
   ```

   再读 [ngx_http_core_module.c:3766-3772](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L3766-L3772)，确认内层 `conf->root.data == NULL` 时会取 `prev->root`（即 `/var/www`）。
3. **需要观察的现象**：把上面的 `return` 换成真实的静态文件配置，访问 `/a/x.txt` 会去 `/var/www/a/x.txt` 找文件，证明 root 被继承。
4. **预期结果**：文件路径前缀是 `/var/www`，即继承自 server 层。
5. 若无运行环境，标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：`merge_loc_conf` 的 `parent` 和 `child` 各指什么？为什么 `parent` 要作为参数层层下传而不是用全局？

**答案**：`child` 是当前 location 的 `loc_conf`，`parent` 是其直接父级（http 层、server 层或外层 location）的 `loc_conf`。层层下传是因为配置继承是「就近」的：一个深层 location 应优先继承最近的祖先，而非全局 http 层。`ngx_http_merge_locations` 递归时把当前 location 的 `loc_conf` 当作下一层的 parent 传入（[ngx_http.c:657-658](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L657-L658)），保证继承链正确。

**练习 2**：为什么 `create_loc_conf` 必须把字段初始化为 `NGX_CONF_UNSET`，而不能初始化成默认值？

**答案**：merge 靠哨兵区分「用户没写」与「用户写了一个恰好等于默认值的值」。若 create 时就填默认值，merge 就无法判断该不该继承父级——一个在 http 层写了 `client_max_body_size 1m` 的配置，本应被子 location 继承，但若子 create 时已填了别的默认值，继承就被破坏。哨兵机制让「未设置」成为可识别状态（详见 u3-l4）。

## 5. 综合实践

把本讲五个模块串起来，完成下面这个「匹配优先级 + 配置合并」的综合验证任务。

**目标**：用一组精心设计的 location，一次性验证「精确 > 最长前缀 > `^~` 拦截正则 > 正则」的完整优先级链，并验证内层 location 继承外层 `root`。

**步骤**（示例代码）：

```nginx
# nginx.conf
events {}
http {
    server {
        listen 8080;
        root  /var/www/default;        # (A) server 层 root

        location = /t      { return 200 "exact\n"; }      # 精确
        location ^~ /p/    { return 200 "prefix-no-regex\n"; } # ^~ 前缀
        location /p/abc    { return 200 "longest-prefix\n"; }  # 更长普通前缀
        location ~ ^/p/    { return 200 "regex\n"; }           # 正则
        location /sub/ {
            root /var/www/sub;        # (B) 内层换 root
            location /sub/inner/ {
                # (C) 故意不写 root，应继承 (B) 的 /var/www/sub
                return 200 "inner\n";
            }
        }
    }
}
```

**要做的三件事**：

1. 用 `objs/nginx -t -p $(pwd) -c nginx.conf` 先校验语法。
2. 分别 `curl` 以下路径，对照源码解释结果：
   - `/t` → `exact`（精确优先，`find_static_location` 直接 `NGX_OK`）；
   - `/p/x` → `prefix-no-regex`（命中 `^~ /p/`，`noregex` 让正则 `^/p/` 被跳过）；
   - `/p/abc` → 思考：会命中更长前缀 `/p/abc` 还是 `^~ /p/`？对照 [find_location 第 1465 行](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L1465) 解释「精确/包容返回后正则被跳过」与 `^~` 的关系；
   - `/sub/inner/` → 若把 `return` 换成静态文件，文件应去 `/var/www/sub/inner/` 找，验证 (C) 继承了 (B) 而非 (A)。
3. 开启 debug 日志（`error_log` 加 `debug`）后重跑，在日志里搜 `test location:` 字样，对照 [find_static_location 第 1531-1533 行](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L1531-L1533) 的 debug 输出，亲眼看到树节点被逐个比较的顺序。

> 若本地无运行环境，本任务的 2、3 步标注「待本地验证」，但第 1 步的源码解释部分可独立完成。

## 6. 本讲小结

- nginx 把 location 分成 `=` 精确、`^~` 优先前缀、`~`/`~*` 正则、普通前缀、`@` named 五类，配置期由 `ngx_http_core_location` 通过修饰符置 `exact_match`/`noregex`/`regex`/`named` 等位标志。
- 配置期 `ngx_http_init_locations` 把乱序列表排序后三分类：静态 location（`=`/`^~`/前缀）建二叉搜索树存进 `static_locations`，正则进 `regex_locations` 数组，named 进 server 级 `named_locations` 数组。
- 运行期 `ngx_http_core_find_location` 按优先级调度：先在静态树找，精确/自动重定向立即返回不碰正则；仅当前缀包容命中或无命中时才评估正则，`^~` 通过 `noregex` 关掉正则这一关。
- `ngx_http_core_find_static_location` 用 `ngx_filename_cmp` 做二叉搜索，靠 `node->tree` 子树实现「最长前缀」，靠 `len+1==node->len && auto_redirect` 实现补斜杠 301。
- named location 不参与 URI 匹配，只能由 `ngx_http_named_location` 内部重定向进入，命中后重置 phase 游标到 `location_rewrite_index` 重跑。
- `merge_loc_conf` 回调沿「http→server→location→嵌套」树自顶向下递归，靠 `NGX_CONF_UNSET` 哨兵区分「未设置」，让子 location 不写的指令就近继承父级或硬编码默认值。

## 7. 下一步学习建议

本讲搞清楚了「请求选哪个 location、这个 location 的配置从哪来」。接下来：

- **u6-l6 过滤器链**：location 选定、`content_handler` 被调用后，响应如何经过 header/body filter 链写出。建议先读 `ngx_http_top_header_filter`/`top_body_filter` 的串联机制。
- **u6-l8 静态文件 content handler**：本讲多次提到 `root`/`alias` 与补斜杠重定向，下一站应读 `ngx_http_static_handler` 看 location 配置如何驱动真实文件查找与 sendfile 输出。
- **u10-l2 访问控制与限流**：`limit_except` 与 `internal` 标志（本讲在 `update_location_config` 里见过）如何影响请求是否被接受。
- 继续阅读源码：[src/http/ngx_http.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c) 的 `ngx_http_join_exact_locations` / `ngx_http_create_locations_tree`，弄清静态前缀树在配置期是如何从排序后的队列被构建出来的，补全本讲略过的「建树」细节。
