# 变量系统 ngx_http_variables

## 1. 本讲目标

nginx 配置里随处可见的 `$remote_addr`、`$request_uri`、`$arg_name`、`$http_host`，以及由 `map`、`geo` 自定义的变量，背后是同一套机制：**变量系统**。本讲学完后，读者应该能够：

- 说清楚一个 `$name` 是怎样从「配置文件里的一段文本」变成「请求处理时的一个字符串值」的；
- 区分两套并存的查找路径——**按 index 查**与**按名字查**，并知道各自何时被使用；
- 理解 `get_handler` 的惰性求值模型，以及 `NOCACHEABLE`、`INDEXED`、`PREFIX`、`NOHASH`、`CHANGEABLE`、`WEAK` 这些标志位的实际作用；
- 看懂 `map` 这类「复杂变量」如何复用变量系统，把一个新变量注册成「求值时再去查映射表」的 handler。

## 2. 前置知识

本讲建立在以下已建立的认知之上，不再重复：

- **`ngx_str_t`**（u2-l2）：长度前缀字符串，`len + data`，求长 O(1)。
- **`ngx_array_t` / `ngx_hash_t`**（u2-l3）：动态数组与配置期一次性建表、运行时只读的哈希表；哈希表值指针低 2 位可编码通配语义。
- **`ngx_http_conf_ctx_t` 三层配置与 `cmcf`**（u6-l1）：`http{}` 块解析出 `ngx_http_core_main_conf_t`（简称 `cmcf`），它是 HTTP 框架的总仓库；模块的 `preconfiguration` / `postconfiguration` 回调分别在配置解析前、后被调用。
- **请求对象 `r`**（u6-l2）：每个请求有一个贯穿全程的 `ngx_http_request_t`，本讲会用到它的 `r->variables` 数组。
- **`offsetof` 反射式赋值**（u3-l4）：把「字段在结构体里的偏移」在编译期固化，运行时用「基址 + 偏移」定位字段。变量系统大量使用这个手法。
- **复杂值 `ngx_http_complex_value_t`**（u7 系列会详讲）：把含 `$变量` 的配置字符串预编译成一段「脚本」，运行时求值。本讲只用到「`$name` 会被编译成一个 index」这一结论。

一个关键直觉先建立起来：变量系统有**两个时间维度**。配置期（`nginx -t` / 启动 / reload）负责「登记变量、把 `$name` 编译成 index」；运行期（处理请求时）负责「按 index 或按名取值」。很多设计只有把两个维度对照看才能看懂。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/http/ngx_http_variables.h` | 变量结构体 `ngx_http_variable_t`、标志位、对外 API 声明 |
| `src/http/ngx_http_variables.c` | 变量系统的全部实现：注册、索引、按名查找、内置变量表、初始化、`map_find` |
| `src/http/modules/ngx_http_map_module.c` | `map` 指令的实现，演示如何把一个新变量注册成「查映射表」的 handler |
| `src/http/ngx_http_script.c` | 把配置里的 `$name` 编译成 index、运行期取值的脚本引擎（本讲只看变量相关片段） |
| `src/http/ngx_http_core_module.{h,c}` | `cmcf` 里存放变量各表的字段；`preconfiguration` 调用 `add_core_vars` |
| `src/http/ngx_http.c` | `ngx_http_block` 在 `postconfiguration` 阶段调用 `init_vars` |
| `src/http/ngx_http_request.c` | 创建请求时分配 `r->variables` 数组 |
| `src/core/ngx_string.h` | 变量值结构 `ngx_variable_value_t` 的定义 |

## 4. 核心概念与源码讲解

### 4.1 变量的注册与索引：两套并存的登记表

#### 4.1.1 概念说明

nginx 的变量并不是一张表，而是**三张表**协同工作，它们都挂在 `cmcf` 上：

- `cmcf->variables_keys`：**配置期的名字 → 变量**哈希表（`ngx_hash_keys_arrays_t`）。所有「被注册过」的变量都在这里，包括内置变量、模块自定义变量、`set`/`map` 创建的变量。它在 `init_vars` 结束后被转建成运行期哈希 `variables_hash` 然后丢弃。
- `cmcf->variables`：**被引用过的变量的索引数组**（`ngx_array_t` of `ngx_http_variable_t`）。每当配置里出现一个 `$name`，它就被追加进这个数组，下标就是它的 `index`。
- `cmcf->prefix_variables`：**前缀变量数组**，如 `http_`、`arg_`、`cookie_`、`sent_http_`。它们不是一个具体变量，而是一类变量的「前缀 handler」。

对应地，运行期有两条取值路径：

- **按 index 取**（`ngx_http_get_indexed_variable`）：配置期已经把 `$name` 编译成了 index，运行期直接用 index 进 `r->variables[index]` 取值。这是最常用的快路径。
- **按名字取**（`ngx_http_get_variable`）：运行期拿一个字符串名字去 `variables_hash` 里查。用于 `map` 的动态键、perl 嵌入、以及那些没法在配置期确定 index 的场景。

> 提示：这里的 `index` 与模块系统的 `ctx_index`（u3-l3）是两套完全独立的东西。`ctx_index` 索引的是「某类模块的配置数组」，而变量的 `index` 索引的是「这个请求里被引用的变量值数组」。

#### 4.1.2 核心流程

配置期与运行期的分工如下：

```
【配置期：nginx -t / 启动】
  ngx_http_block
   ├─ preconfiguration 阶段
   │    └─ ngx_http_variables_add_core_vars
   │         └─ 遍历 ngx_http_core_variables[]，逐个 ngx_http_add_variable 登记
   │            （此时 variables_keys 被填满）
   ├─ ngx_conf_parse 解析配置
   │    └─ 遇到 "$request_uri" 这类含变量的参数
   │         └─ ngx_http_compile_complex_value → ngx_http_script_add_var_code
   │              └─ ngx_http_get_variable_index：把名字追加进 cmcf->variables，拿到 index
   │                 （此时 cmcf->variables 被逐步填满）
   └─ postconfiguration 阶段
        └─ ngx_http_variables_init_vars
             ├─ 为每个 indexed 变量关联 get_handler（查 keys / 前缀）
             ├─ 给 NOHASH 变量移出哈希
             └─ 用 variables_keys 构建 variables_hash，丢弃 variables_keys

【运行期：处理请求】
  创建请求 → ngx_pcalloc 分配 r->variables[cmcf->variables.nelts]
  脚本执行遇到 $request_uri
   └─ ngx_http_script_copy_var_code → ngx_http_get_indexed_variable(r, index)
        └─ 首次：调 v->get_handler 填 r->variables[index]；再次：直接返回缓存
```

注意三个关键点：①内置变量是在 `preconfiguration` 里登记的，早于配置解析，所以配置里写 `$remote_addr` 时它已经存在；②`$name` → index 的编译发生在解析配置时，每出现一次同一个 `$name` 都会复用同一个 index；③真正构建运行期哈希 `variables_hash` 是在 `postconfiguration` 的 `init_vars` 里，之后 `variables_keys` 就被释放（`cmcf->variables_keys = NULL`）。

#### 4.1.3 源码精读

先看变量与值的核心结构。变量本身用 `ngx_http_variable_t` 描述（[src/http/ngx_http_variables.h:L37-L44](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_variables.h#L37-L44)）：

```c
struct ngx_http_variable_s {
    ngx_str_t                     name;   /* must be first to build the hash */
    ngx_http_set_variable_pt      set_handler;
    ngx_http_get_variable_pt      get_handler;
    uintptr_t                     data;
    ngx_uint_t                    flags;
    ngx_uint_t                    index;
};
```

`name` 被注释要求「必须放第一个」，是为了构建哈希表时能把它当成 `ngx_hash_key_t` 直接用；`get_handler` 是取值回调；`data` 是传给 handler 的参数（常是一个 `offsetof` 偏移或一个上下文指针）；`flags` 是行为标志；`index` 在 `init_vars` 里被回填。

变量值用 `ngx_variable_value_t` 描述（[src/core/ngx_string.h:L28-L37](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_string.h#L28-L37)）：

```c
typedef struct {
    unsigned    len:28;
    unsigned    valid:1;
    unsigned    no_cacheable:1;
    unsigned    not_found:1;
    unsigned    escape:1;
    u_char     *data;
} ngx_variable_value_t;
```

这是一个紧凑的位域结构：`len` 与 `data` 表达字符串本身，`valid` 表示「已成功求值」、`not_found` 表示「求过了但不存在」、`no_cacheable` 表示「这个值别缓存、下次重新求」。`r->variables[index]` 就是这个类型，所以「是否已求值」是靠 `valid`/`not_found` 两个位记忆的。

标志位定义在 [src/http/ngx_http_variables.h:L29-L34](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_variables.h#L29-L34)：

| 标志 | 含义 |
| --- | --- |
| `NGX_HTTP_VAR_CHANGEABLE` | 同名变量可重复注册，第二次返回已存在的那一个 |
| `NGX_HTTP_VAR_NOCACHEABLE` | 求值成功后仍标记 `no_cacheable`，下次访问强制重算 |
| `NGX_HTTP_VAR_INDEXED` | 该变量同时是一个 indexed 变量，按名查找时转发到按 index 查找以共享缓存 |
| `NGX_HTTP_VAR_NOHASH` | 不放入运行期 `variables_hash`，只能通过 index 访问 |
| `NGX_HTTP_VAR_WEAK` | 软注册（`set $x` 用），可被同名强注册覆盖 |
| `NGX_HTTP_VAR_PREFIX` | 前缀变量（`arg_`、`http_` 等），走 `prefix_variables` |

内置变量表是一张静态数组 `ngx_http_core_variables[]`（[src/http/ngx_http_variables.c:L168](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_variables.c#L168)），每项就是一个 `ngx_http_variable_t` 字面量。挑几个有代表性的看：

```c
/* src/http/ngx_http_variables.c:L244-L245 —— $request_uri 取 r->unparsed_uri */
{ ngx_string("request_uri"), NULL, ngx_http_variable_request,
  offsetof(ngx_http_request_t, unparsed_uri), 0, 0 },

/* src/http/ngx_http_variables.c:L203-L204 —— $remote_addr 取 r->connection->addr_text */
{ ngx_string("remote_addr"), NULL, ngx_http_variable_remote_addr, 0, 0, 0 },

/* src/http/ngx_http_variables.c:L408-L409 —— $arg_ 前缀变量 */
{ ngx_string("arg_"), NULL, ngx_http_variable_argument,
  0, NGX_HTTP_VAR_NOCACHEABLE|NGX_HTTP_VAR_PREFIX, 0 },
```

`$request_uri` 用通用 handler `ngx_http_variable_request` 配 `offsetof(unparsed_uri)` 直接定位请求里的字段；`$remote_addr` 有专属 handler；`$arg_` 是前缀变量（`PREFIX`），且不可缓存（`NOCACHEABLE`）。

**注册函数 `ngx_http_add_variable`**（[src/http/ngx_http_variables.c:L424-L500](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_variables.c#L424-L500)）：先在 `variables_keys` 里查同名变量，若已存在且带 `CHANGEABLE` 就直接返回已存在的那一个（并按 `WEAK` 规则清理标志），否则 `ngx_palloc` 新建一个并 `ngx_hash_add_key` 登记。注意它把名字小写化（`ngx_strlow`）——nginx 变量名是大小写不敏感的。

**索引函数 `ngx_http_get_variable_index`**（[src/http/ngx_http_variables.c:L558-L615](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_variables.c#L558-L615)）：线性扫描 `cmcf->variables` 数组，找到同名就返回下标；找不到就 `ngx_array_push` 追加一个新项，`index = cmcf->variables.nelts - 1`。这里用线性扫描而非哈希，是因为「被引用的变量」数量通常很少（几十个量级），且每个变量只在配置期查一次，线性扫描的开销可以忽略。

这两张表的填充时机不同：`variables_keys` 在 `preconfiguration` 里由 `ngx_http_variables_add_core_vars`（[src/http/ngx_http_variables.c:L2745-L2785](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_variables.c#L2745-L2785)）一次性填好内置变量，它由 `ngx_http_core_preconfiguration` 调用（[src/http/ngx_http_core_module.c:L3452](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L3452)）；而 `cmcf->variables` 是在配置解析过程中，每遇到一个 `$name` 才追加一项。

**`$name` 如何变成 index**：配置里含变量的字符串会被 `ngx_http_compile_complex_value` 预编译。编译器遇到 `$name` 时调用 `ngx_http_script_add_var_code`（[src/http/ngx_http_script.c:L891-L932](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_script.c#L891-L932)），其核心一行是：

```c
index = ngx_http_get_variable_index(sc->cf, name);   /* script.c:896 */
...
code->index = (uintptr_t) index;                      /* script.c:919, 929 */
```

即把名字解析成 index 并写进脚本字节码。运行期执行脚本时，`ngx_http_script_copy_var_len_code`（[src/http/ngx_http_script.c:L935-L957](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_script.c#L935-L957)）用这个 index 调 `ngx_http_get_indexed_variable`（或 flushed 版本）取值。这样「名字字符串」在配置期就消失了，运行期只剩一个整数 index。

**收尾函数 `ngx_http_variables_init_vars`**（[src/http/ngx_http_variables.c:L2788-L2894](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_variables.c#L2788-L2894)）：在 `postconfiguration` 阶段由 `ngx_http_block` 调用（[src/http/ngx_http.c:L317](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L317)）。它做三件事：①给每个 indexed 变量关联 `get_handler`（先在 keys 里精确查，再退而查前缀，都没有就报 `unknown "..." variable` 致命错）；②把带 `NOHASH` 的变量从哈希里剔除；③用 `variables_keys` 构建运行期 `variables_hash`，然后 `cmcf->variables_keys = NULL` 释放配置期结构。

最后，每个请求在 `ngx_http_create_request` 里分配自己的值数组（[src/http/ngx_http_request.c:L632-L633](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L632-L633)）：

```c
r->variables = ngx_pcalloc(r->pool, cmcf->variables.nelts
                                    * sizeof(ngx_http_variable_value_t));
```

`pcalloc` 把所有 `valid`/`not_found` 位清零，表示「本请求还没求过任何变量」。

#### 4.1.4 代码实践

1. **实践目标**：验证 `$request_uri` 与 `$remote_addr` 能被正确求值，并定位它们的取值路径。
2. **操作步骤**：
   - 在 `nginx.conf` 的 `http{}` 里加一个 server，监听 8080，根目录指向任意空目录；
   - 加一个 location：
     ```nginx
     location /v {
         return 200 "uri=[$request_uri] addr=[$remote_addr]\n";
     }
     ```
   - `nginx -t` 校验后启动，执行 `curl -i http://127.0.0.1:8080/v?name=hello`。
3. **需要观察的现象**：响应体应形如 `uri=[/v?name=hello] addr=[127.0.0.1]`。
4. **预期结果**：`$request_uri` 取到的是未归一化的原始 URI（含查询串），对应源码里 `offsetof(unparsed_uri)`；`$remote_addr` 取到客户端地址。
5. **源码追踪**：在 `ngx_http_core_variables[]` 里找到 `request_uri` 与 `remote_addr` 两项，确认它们的 `get_handler` 分别是 `ngx_http_variable_request` 和 `ngx_http_variable_remote_addr`；再追到这两个 handler 的实现（见 4.3.3）。
6. 若本地无法编译运行，标注「待本地验证」并仅完成源码追踪部分。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ngx_http_add_variable` 用哈希表登记，而 `ngx_http_get_variable_index` 用线性数组查找？

**参考答案**：`variables_keys` 登记的是「所有可能存在的变量」（内置 + 模块自定义 + `set`/`map`），数量可达上百，且 `add_variable` 在配置期会被各模块反复调用查重，哈希表 O(1) 更合适；`cmcf->variables` 只装「本配置里实际被 `$name` 引用过的变量」，通常很少（几十个以内），且每个名字只在配置期查一次，线性扫描实现简单、常数小，足够用。

**练习 2**：`NOHASH` 变量为什么无法通过「按名字查找」访问？举一个例子。

**参考答案**：`init_vars` 在构建 `variables_hash` 前会把带 `NOHASH` 的变量项的 `key.data` 置 NULL（[src/http/ngx_http_variables.c:L2870-L2872](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_variables.c#L2870-L2872)），`ngx_hash_init` 会跳过这些项，于是运行期 `ngx_http_get_variable` 在哈希里查不到它们。它们只能通过配置期 `$name` 编译出的 index 访问。例如 `ngx_http_gzip_filter_module` 注册的 `$gzip_ratio` 带了 `NOHASH`（[src/http/modules/ngx_http_gzip_filter_module.c:L1009](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_gzip_filter_module.c#L1009)），只有显式写 `$gzip_ratio` 才会触发求值，避免无意义的哈希膨胀与计算。

### 4.2 按名查找与求值：ngx_http_get_variable

#### 4.2.1 概念说明

按 index 查找的前提是「配置期就知道变量名」。但有些场景变量名是运行期才确定的——例如 `map` 的键本身可能是任意字符串、perl 模块用任意名字取变量、或模块需要按动态名字探测。这时走 **`ngx_http_get_variable(r, name, key)`**：用名字去运行期哈希 `variables_hash` 里查。

它有三种结果：

1. **命中一个 indexed 变量**（带 `INDEXED` 标志）：转发到 `ngx_http_get_flushed_variable(r, v->index)`，复用 `r->variables[index]` 里的缓存，保证「同一个变量无论按名还是按 index 取，都拿到同一份值」。
2. **命中一个普通变量**：直接调它的 `get_handler`，结果存一个临时 `ngx_http_variable_value_t`（从请求池分配）。
3. **没命中精确名字**：退到 `prefix_variables` 做**最长前缀匹配**，命中则把「完整变量名」作为 `data` 传给该前缀 handler；再没有就返回 `not_found`。

另外有一个全局递归计数器 `ngx_http_variable_depth = 100`，防止变量 handler 递归求值自身导致死循环。

#### 4.2.2 核心流程

```
ngx_http_get_variable(r, name, key)
  v = ngx_hash_find(variables_hash, key, name)        // 精确查
  if v:
      if v 带 INDEXED:
          return get_flushed_variable(r, v->index)     // 复用缓存
      depth--
      vv = palloc(value)
      if v->get_handler(r, vv, v->data) == OK:
          depth++; return vv                           // 直接求值
      depth++; return NULL
  // 精确没命中，查前缀
  len = 0
  for 每个 prefix_variable pv[i]:
      if name 以 pv[i].name 为前缀 且 pv[i].name.len > len:
          len = pv[i].name.len; n = i                  // 记最长前缀
  if 找到前缀:
      pv[n].get_handler(r, vv, (uintptr_t) name)       // 把完整 name 当 data
      return vv
  vv->not_found = 1; return vv
```

前缀匹配用「最长前缀」规则：若同时有 `http_` 和更具体的前缀，选更长的。注意传给前缀 handler 的 `data` 是**完整的变量名字符串指针**（`(uintptr_t) name`），而不是注册时的 `data`——因为前缀 handler 需要知道「用户实际写的是哪个具体名字」（例如 `$arg_name` 里的 `name` 部分）才能取值。

#### 4.2.3 源码精读

`ngx_http_get_variable` 完整实现见 [src/http/ngx_http_variables.c:L688-L755](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_variables.c#L688-L755)。精确命中后的 `INDEXED` 转发在 702-704 行：

```c
if (v->flags & NGX_HTTP_VAR_INDEXED) {
    return ngx_http_get_flushed_variable(r, v->index);
}
```

前缀最长匹配段在 732-742 行：

```c
for (i = 0; i < cmcf->prefix_variables.nelts; i++) {
    if (name->len >= v[i].name.len && name->len > len
        && ngx_strncmp(name->data, v[i].name.data, v[i].name.len) == 0)
    {
        len = v[i].name.len;
        n = i;
    }
}
```

递归保护 `ngx_http_variable_depth` 在 638-645 行（`get_indexed_variable`）与 706-712 行（`get_variable`）各有一份：进入 handler 前 `depth--`，出来后 `depth++`，到 0 就报 `cycle while evaluating variable` 并返回 NULL。

前缀变量的典型 handler 是 `ngx_http_variable_argument`（[src/http/ngx_http_variables.c:L1108-L1133](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_variables.c#L1108-L1133)），它演示了「从完整变量名剥离前缀」的套路：

```c
ngx_str_t *name = (ngx_str_t *) data;            /* 完整变量名 $arg_xxx */
len = name->len - (sizeof("arg_") - 1);
arg = name->data + sizeof("arg_") - 1;           /* 跳过 "arg_" 前缀 */
if (len == 0 || ngx_http_arg(r, arg, len, &value) != NGX_OK) {
    v->not_found = 1;
    return NGX_OK;
}
v->data = value.data;  v->len = value.len;  v->valid = 1;
```

即 `$arg_name` 会去掉 `arg_` 前缀，拿 `name` 去 `r->args` 里查对应的查询参数。`$http_x_foo`、`$cookie_sid`、`$sent_http_bar` 都是同一个模式，只是查的列表不同（请求头 / Cookie / 响应头）。

#### 4.2.4 代码实践

1. **实践目标**：验证前缀变量 `$arg_name` 的运行期求值，并理解它走的是「按名查找 → 前缀匹配」路径。
2. **操作步骤**：
   - 用 4.1.4 的 server，把 location 改为：
     ```nginx
     location /v {
         return 200 "name=[$arg_name] city=[$arg_city]\n";
     }
     ```
   - `curl 'http://127.0.0.1:8080/v?name=alice&city=sh'`。
3. **需要观察的现象**：响应体应为 `name=[alice] city=[sh]`；不带某参数时对应值为空。
4. **预期结果**：`$arg_name` 在配置期被编译成 index（因为它是显式 `$name` 引用），运行期走 `get_indexed_variable` → `init_vars` 里关联到前缀 handler `ngx_http_variable_argument` → 剥离 `arg_` 后调 `ngx_http_arg` 查询参数。
5. **源码追踪**：在 `ngx_http_variables_init_vars`（[src/http/ngx_http_variables.c:L2837-L2853](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_variables.c#L2837-L2853)）里确认：indexed 变量 `arg_name` 精确名字查不到（因为注册的只是前缀 `arg_`），于是落到前缀循环，匹配到 `arg_`，把 `v[i].data` 设为 `&v[i].name`（即变量名字符串），`get_handler` 设为前缀 handler。
6. 若本地无法运行，标注「待本地验证」并完成源码追踪。

#### 4.2.5 小练习与答案

**练习 1**：请求头 `X-Forwarded-For` 对应哪个 nginx 变量？它是怎么被找到的？

**参考答案**：对应 `$http_x_forwarded_for`。命名规则是 `http_` 前缀 + 头名小写、连字符替换为下划线。它由前缀变量 `http_`（`ngx_http_variable_unknown_header_in`）处理：运行时去掉 `http_` 前缀、把剩余部分还原成头名，再去 `r->headers_in.headers` 列表里匹配。注意 `$http_x_forwarded_for` 有一个特例：因为它太常用，nginx 为它在 `ngx_http_core_variables[]` 里单独登记了一个专属项（用 `ngx_http_variable_header` 直接定位 `headers_in.x_forwarded_for`），跳过通用的「遍历未知头」路径以提升性能（见源码文件顶部 161-166 行的注释）。

**练习 2**：为什么 `ngx_http_get_variable` 命中 `INDEXED` 后要转发到 `get_flushed_variable`，而不是直接调 `get_handler`？

**参考答案**：因为同一个变量可能既被配置期 `$name` 引用（有 index、值缓存在 `r->variables[index]`），又被运行期按名查找。若按名查找时直接调 `get_handler` 重新求值，就会和 `r->variables[index]` 里的缓存产生两份不一致的值（尤其对 `NOCACHEABLE` 变量，缓存语义会被破坏）。转发到 `get_flushed_variable(r, v->index)` 让两条路径汇合到同一个 `r->variables[index]` 槽位，保证一个请求内同名同值。

### 4.3 get_handler、值结构与惰性缓存

#### 4.3.1 概念说明

变量取值的核心是**惰性求值（lazy evaluation）**：变量只在第一次被访问时才调用 `get_handler` 计算，结果记进 `r->variables[index]`；同一请求内再次访问直接返回缓存。这把「计算成本」摊到「真正用到时」，未引用的变量完全不产生开销。

缓存由 `ngx_variable_value_t` 的三个位共同表达：

- `valid = 1`：已成功求值，`data/len` 是有效值；
- `not_found = 1`：求过了但不存在（如 `$arg_missing`）；
- 两者都为 0：还没求过，需要调 `get_handler`。

`NOCACHEABLE` 标志改变这个缓存行为：求值成功后仍把 `no_cacheable` 位置 1，下次访问时 `ngx_http_get_flushed_variable` 会先清掉 `valid/not_found` 再重新调 handler。适合值会随请求进展变化的变量，如 `$uri`（会被 rewrite 改写）、`$msec`（每次都不同）、`$status`（响应阶段才确定）。

`get_handler` 有三种典型写法，对应三种取值来源：

1. **直接指向 `r` 内字段**：用 `offsetof` 把字段偏移存进 `data`，handler 里 `(char *) r + data` 取出 `ngx_str_t*` 直接用，零拷贝、零分配。代表是 `ngx_http_variable_request`（服务 `$request_uri`/`$uri`/`$args`/`$query_string`）。
2. **计算后写请求池**：值需要格式化或系统调用，handler 里 `ngx_pnalloc` 一块内存写进去。代表是 `$msec`、`$status`、`$remote_port`。
3. **前缀驱动**：handler 收到的 `data` 是完整变量名，自己剥离前缀再去查列表。代表是 `$arg_`、`$http_`。

#### 4.3.2 核心流程

```
ngx_http_get_indexed_variable(r, index):
  if r->variables[index].not_found or r->variables[index].valid:
      return &r->variables[index]              // 缓存命中（含 not_found）
  depth--
  if v[index].get_handler(r, &r->variables[index], v[index].data) == OK:
      depth++
      if v[index].flags & NOCACHEABLE:
          r->variables[index].no_cacheable = 1
      return &r->variables[index]
  depth++
  r->variables[index].valid = 0
  r->variables[index].not_found = 1            // 求值失败，记 not_found
  return NULL

ngx_http_get_flushed_variable(r, index):
  v = &r->variables[index]
  if v->valid or v->not_found:
      if !v->no_cacheable: return v            // 可缓存，直接返回
      v->valid = 0; v->not_found = 0           // 不可缓存，清掉重算
  return ngx_http_get_indexed_variable(r, index)
```

关键点：①`not_found` 也会被缓存，所以 `$arg_missing` 第二次访问不会再去查参数；②`get_handler` 返回非 `NGX_OK` 时被当作「不存在」记 `not_found`；③`get_flushed_variable` 是 `get_indexed_variable` 的「可重算」包装，被按名查找路径与前缀路径使用，确保 `NOCACHEABLE` 变量每次都拿到最新值。

#### 4.3.3 源码精读

`ngx_http_get_indexed_variable`（[src/http/ngx_http_variables.c:L618-L665](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_variables.c#L618-L665)），缓存判定与求值：

```c
if (r->variables[index].not_found || r->variables[index].valid) {
    return &r->variables[index];           /* 缓存命中 */
}
...
if (v[index].get_handler(r, &r->variables[index], v[index].data) == NGX_OK) {
    ngx_http_variable_depth++;
    if (v[index].flags & NGX_HTTP_VAR_NOCACHEABLE) {
        r->variables[index].no_cacheable = 1;
    }
    return &r->variables[index];
}
r->variables[index].valid = 0;
r->variables[index].not_found = 1;          /* 失败也缓存 */
```

`ngx_http_get_flushed_variable`（[src/http/ngx_http_variables.c:L668-L685](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_variables.c#L668-L685)）：若已求值且 `no_cacheable`，先清 `valid/not_found` 再回到 `get_indexed_variable`。

「直接指向字段」型 handler 的代表 `ngx_http_variable_request`（[src/http/ngx_http_variables.c:L758-L778](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_variables.c#L758-L778)）：

```c
ngx_str_t  *s;
s = (ngx_str_t *) ((char *) r + data);       /* data 是 offsetof */
if (s->data) {
    v->len = s->len;  v->valid = 1;  v->data = s->data;
} else {
    v->not_found = 1;
}
return NGX_OK;
```

`$request_uri` 的 `data = offsetof(unparsed_uri)`，所以这一行 `s = (char*)r + offsetof(...)` 就定位到了 `r->unparsed_uri`，直接复用请求里已有的字符串，不分配任何内存。`$uri`、`$args`、`$query_string`、`$server_protocol` 都复用这一个 handler，只是 `offsetof` 不同。

「直接指向字段」的另一个代表 `ngx_http_variable_remote_addr`（[src/http/ngx_http_variables.c:L1309-L1320](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_variables.c#L1309-L1320)），它没有用通用 handler 而是单独写，因为字段在 `r->connection` 而非 `r` 上：

```c
v->len = r->connection->addr_text.len;
v->valid = 1;
v->data = r->connection->addr_text.data;
return NGX_OK;
```

同样是零拷贝——直接把 `ngx_str_t` 的 `len/data` 抄进值结构。

「计算后写池」型 handler 的代表是 `$msec`（[src/http/ngx_http_variables.c:L2458-L2479](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_variables.c#L2458-L2479)），它调 `ngx_timeofday()` 取缓存时间，`ngx_sprintf` 格式化成 `"秒.毫秒"` 写进 `ngx_pnalloc` 的内存。它在 `ngx_http_core_variables[]` 里标了 `NOCACHEABLE`（[src/http/ngx_http_variables.c:L373-L374](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_variables.c#L373-L374)），所以每次访问都重算。

「前缀驱动」型见 4.2.3 的 `ngx_http_variable_argument`，此处不重复。

#### 4.3.4 代码实践

1. **实践目标**：观察 `NOCACHEABLE` 变量（`$msec`）与可缓存变量（`$request_uri`）在同一请求里被多次引用时的行为差异。
2. **操作步骤**：
   - 配置：
     ```nginx
     log_format trace '$msec|$msec|$request_uri|$request_uri';
     access_log /tmp/trace.log trace;
     location /v { return 200 ok; }
     ```
   - `curl http://127.0.0.1:8080/v`，查看 `/tmp/trace.log` 的一行。
3. **需要观察的现象**：日志一行里两个 `$msec` 值**可能不同**（毫秒位递增），两个 `$request_uri` 完全相同。
4. **预期结果**：`$msec` 标了 `NOCACHEABLE`，第二次访问会清缓存重算，故可能得到更新的毫秒；`$request_uri` 可缓存，第二次直接返回 `r->variables[index]` 里的旧值。
5. **源码追踪**：在 `ngx_http_core_variables[]` 确认 `msec` 带 `NOCACHEABLE`（L373）、`request_uri` 不带（L244）；在 `ngx_http_get_flushed_variable` 确认 `no_cacheable` 触发清缓存重算。
6. 若本地无法运行，标注「待本地验证」并完成源码追踪。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `$uri` 标了 `NOCACHEABLE`，而 `$request_uri` 没有？

**参考答案**：`$request_uri` 对应 `r->unparsed_uri`，是客户端发来的原始 URI，整个请求生命周期内不变，所以可以缓存。`$uri` 对应 `r->uri`，它会被 `rewrite` 指令在 `REWRITE` 阶段改写，请求处理过程中可能多次变化（每经过一次 rewrite 都不同），因此必须标 `NOCACHEABLE`，保证每次读取都拿到当前最新的 `r->uri`。同理 `$args`、`$query_string`、`$document_root`、`$status`、`$request_time` 都因「值会变」而标 `NOCACHEABLE`。

**练习 2**：`valid` 和 `not_found` 都是「已求值」状态，为什么要把它们分开存？

**参考答案**：因为「不存在」也是一个需要被记住的结果。如果不存 `not_found`，每次访问 `$arg_missing`（一个不存在的参数）都会触发 `get_handler` 去遍历查询串，浪费开销；记下 `not_found` 后，第二次访问直接命中缓存返回空值。同时区分两者能让消费方判断「这是一个空字符串的合法值」还是「这个变量根本不存在」，例如 `if ($arg_x)` 在 `not_found` 时为假。

### 4.4 复杂变量：map 如何复用变量系统

#### 4.4.1 概念说明

`map`、`geo`、`split_clients`、`referer`、`browser` 这些指令都遵循同一个模式：**定义一个新变量，它的值由另一个变量经一张映射表计算得来**。它们没有走「内置 handler 表」，而是复用变量系统的注册接口，把自定义 `get_handler` 挂上去。

以 `map` 为例：

```nginx
map $http_host $backend {
    default        "a.example.com";
    "~*^foo"       "b.example.com";
    "bar.com"      "c.example.com";
}
```

它的本质是：注册一个名为 `backend` 的变量，其 `get_handler` 是 `ngx_http_map_variable`。运行期取 `$backend` 时，handler 先求出输入变量 `$http_host` 的值，再在配置期建好的哈希/正则映射表里查，把命中条目的值填进 `$backend`。

一个精巧之处：映射表的「值」本身可以是含变量的复杂字符串（如 `"${backend_prefix}.com"`）。`map` 模块在配置期把纯字符串值标 `valid=1`、把含变量的值标 `valid=0` 并附带一个 `ngx_http_complex_value_t`，运行期取到条目后再决定是否对值做第二次求值。这就是「变量值为变量」的延迟求值。

#### 4.4.2 核心流程

```
【配置期 ngx_http_map_block】
  1. 编译输入源 value[1]（如 $http_host）为 complex_value
  2. 从 value[2] 取变量名（去掉前导 $）→ ngx_http_add_variable(CHANGEABLE)
  3. var->get_handler = ngx_http_map_variable; var->data = map_ctx
  4. 递归 ngx_conf_parse 解析块体，把每个 "pattern value" 存进临时哈希 keys
  5. 用 keys 构建 map->map.hash（含通配）与 map->map.regex（PCRE）

【运行期 ngx_http_map_variable(r, v, data)】
  map = (ngx_http_map_ctx_t *) data
  ngx_http_complex_value(r, &map->value, &val)   // 求输入变量
  value = ngx_http_map_find(r, &map->map, &val)  // 查映射表
  if value == NULL: value = map->default_value
  if !value->valid:
      // 值是复杂字符串，再求一次
      cv = (ngx_http_complex_value_t *) value->data
      ngx_http_complex_value(r, cv, &str)
      v->data = str.data; v->len = str.len; v->valid = 1
  else:
      *v = *value                                  // 纯字符串，直接抄
```

`ngx_http_map_find` 内部先小写化输入、用 `ngx_hash_find_combined` 查精确/通配哈希，命中即返回；未命中且有正则条目时，按顺序逐个 `ngx_http_regex_exec`，首个匹配返回。都没命中返回 NULL，由调用方回落到 `default`。

#### 4.4.3 源码精读

`ngx_http_map_block` 是 `map` 指令的 set 回调（[src/http/modules/ngx_http_map_module.c:L175-L365](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_map_module.c#L175-L365)）。注册变量的关键三步：

```c
/* map.c:230 —— 注册目标变量（CHANGEABLE 允许同名覆盖） */
var = ngx_http_add_variable(cf, &name, NGX_HTTP_VAR_CHANGEABLE);

/* map.c:235-236 —— 装上自定义 get_handler，data 指向 map 上下文 */
var->get_handler = ngx_http_map_variable;
var->data = (uintptr_t) map;
```

注意它用 `NGX_HTTP_VAR_CHANGEABLE` 注册——因为 `map` 指令允许同名变量被多次 `map`（覆盖语义），也允许与其它模块共享同名变量。

运行期 handler `ngx_http_map_variable`（[src/http/modules/ngx_http_map_module.c:L107-L155](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_map_module.c#L107-L155)）：

```c
ngx_http_complex_value(r, &map->value, &val);          /* 求输入变量 */
value = ngx_http_map_find(r, &map->map, &val);         /* 查映射表 */
if (value == NULL) {
    value = map->default_value;                        /* 回落 default */
}
if (!value->valid) {
    /* 值本身是复杂字符串，二次求值 */
    cv = (ngx_http_complex_value_t *) value->data;
    ngx_http_complex_value(r, cv, &str);
    v->len = str.len;  v->data = str.data;  v->valid = 1;
} else {
    *v = *value;                                       /* 纯字符串直接抄 */
}
```

映射表查找 `ngx_http_map_find`（[src/http/ngx_http_variables.c:L2529-L2586](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_variables.c#L2529-L2586)）：`ngx_hash_strlow` 小写化输入并算哈希键，`ngx_hash_find_combined` 一次查精确 + 头部通配 + 尾部通配；未命中且 `NGX_PCRE` 启用时，遍历 `map->regex` 数组逐个正则匹配。

单条 `pattern value` 的解析在 `ngx_http_map`（[src/http/modules/ngx_http_map_module.c:L380-L589](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_map_module.c#L380-L589)），它区分纯值与复杂值：

```c
/* map.c:484-500 —— 值含变量时 lengths!=NULL，存 complex_value，valid=0 */
if (cv.lengths != NULL) {
    cvp = ngx_palloc(ctx->keys.pool, sizeof(ngx_http_complex_value_t));
    *cvp = cv;
    var->data = (u_char *) cvp;
    var->valid = 0;                  /* 标记：需要运行期二次求值 */
} else {
    var->len = v.len;
    var->data = v.data;
    var->valid = 1;                  /* 纯字符串，可直接用 */
}
```

这就是 4.4.1 说的「值为变量」的延迟求值机制：配置期只编译不展开，运行期取到条目后再调 `ngx_http_complex_value` 求出最终字符串。

#### 4.4.4 代码实践

1. **实践目标**：用 `map` 定义一个变量，验证它能在请求处理时被正确求值，并追踪求值链。
2. **操作步骤**：
   - 配置：
     ```nginx
     map $http_host $target {
         default       "default-host";
         "~*^foo"      "foo-host";
         "bar.example" "bar-host";
     }
     server {
         listen 8080;
         location /v { return 200 "target=[$target]\n"; }
     }
     ```
   - 分别执行：
     - `curl -H 'Host: foodomain' http://127.0.0.1:8080/v`
     - `curl -H 'Host: bar.example' http://127.0.0.1:8080/v`
     - `curl -H 'Host: other' http://127.0.0.1:8080/v`
3. **需要观察的现象**：三次响应分别是 `target=[foo-host]`、`target=[bar-host]`、`target=[default-host]`。
4. **预期结果**：`$target` 是 `map` 注册的变量，其 `get_handler` 为 `ngx_http_map_variable`；运行期先求 `$http_host`，再 `ngx_http_map_find` 查表，正则 `~*^foo` 命中 `foodomain`，精确匹配命中 `bar.example`，其余回落 `default`。
5. **源码追踪**：在 `ngx_http_map_block` 确认 `var->get_handler = ngx_http_map_variable`（map.c:235）；在 `ngx_http_map_variable` 确认 `ngx_http_map_find` 调用（map.c:128）。
6. 若本地无法运行，标注「待本地验证」并完成源码追踪。

#### 4.4.5 小练习与答案

**练习 1**：当 `map` 的某个值是含变量的字符串（如 `"${prefix}.x"`）时，为什么要延迟到运行期求值，而不是配置期直接展开？

**参考答案**：因为值里引用的变量（如 `$prefix`）本身的值是每请求不同的，配置期根本无法展开。`map` 在配置期只把它编译成 `ngx_http_complex_value_t` 存进条目（标 `valid=0`），运行期取到该条目后再调 `ngx_http_complex_value` 对其求值，拿到当次请求下的最终字符串。这也是为什么 `ngx_http_map_variable` 里有一个 `if (!value->valid)` 分支做二次求值。

**练习 2**：`map` 注册目标变量时为什么用 `NGX_HTTP_VAR_CHANGEABLE`？

**参考答案**：因为 `map` 的目标变量可能已被其它 `map` 或模块注册过（例如多个 `map` 指令写入同一个变量名，或与 `set` 共用），`CHANGEABLE` 允许 `ngx_http_add_variable` 在同名时返回已存在的变量对象而不是报 `the duplicate variable` 错误，从而让后注册的 `map` 覆盖 `get_handler`。同时这也与 `set $x` 用 `CHANGEABLE|WEAK` 的机制兼容，避免冲突。

## 5. 综合实践

把本讲四个模块串起来，设计一个综合任务：

**配置**（在一个最小 `nginx.conf` 里）：

```nginx
http {
    map $request_uri $route {
        default        "slow";
        ~^/api/        "api";
        =/health       "health";
    }

    log_format varlog '$remote_addr $request_uri $arg_id $route $status';
    access_log /tmp/varlog.log varlog;

    server {
        listen 8080;
        location / {
            return 200 "route=$route id=$arg_id\n";
        }
    }
}
```

**任务**：

1. 用 `curl 'http://127.0.0.1:8080/api/x?id=42'`、`curl http://127.0.0.1:8080/health`、`curl http://127.0.0.1:8080/other` 分别请求，观察响应体与 `/tmp/varlog.log` 中五个变量的值。
2. 对响应体里出现的 `$route` 与 `$arg_id`，分别说明它们走的是「按 index」还是「按名」路径，以及各自的 `get_handler` 是哪一个。
3. 在源码里画出从「`return` 指令的参数字符串被编译」到「运行期这几个变量被求值」的完整链路，标注关键函数与行号：
   - 配置期：`ngx_http_compile_complex_value` → `ngx_http_script_add_var_code`（[src/http/ngx_http_script.c:L891-L932](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_script.c#L891-L932)）→ `ngx_http_get_variable_index`（[src/http/ngx_http_variables.c:L558-L615](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_variables.c#L558-L615)）；
   - 运行期：`ngx_http_script_copy_var_len_code`（[src/http/ngx_http_script.c:L935-L957](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_script.c#L935-L957)）→ `ngx_http_get_indexed_variable`（[src/http/ngx_http_variables.c:L618-L665](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_variables.c#L618-L665)）→ 各自的 `get_handler`（`ngx_http_map_variable`、`ngx_http_variable_argument`、`ngx_http_variable_request`）。
4. 把 `$route` 的值改成含变量的映射（如 `api  "svc-$arg_id";`），重新请求，验证「值为变量」的二次求值路径（`ngx_http_map_variable` 的 `!value->valid` 分支）。

**预期结果**：`$route` 命中 `map` 的不同分支；`$arg_id` 取查询参数；日志里五个变量按 `log_format` 顺序被批量求值。第 4 步改配置后，`/api/x?id=42` 的响应里 `route` 应为 `svc-42`。

若本地无法编译运行，标注「待本地验证」，重点完成第 2、3 步的源码链路梳理。

## 6. 本讲小结

- 变量系统有**三张表**：配置期 `variables_keys`（名字 → 变量，含全部已注册变量）、`variables`（被引用变量的索引数组）、`prefix_variables`（前缀变量）；运行期由 `variables_keys` 转建出 `variables_hash`。
- 取值有**两条路径**：按 index（`ngx_http_get_indexed_variable`，配置期已编译好，最常用）与按名（`ngx_http_get_variable`，运行期哈希查找，用于动态名字）。命中 `INDEXED` 标志时按名查找会转发到按 index 查找以共享缓存。
- 变量值存在每个请求的 `r->variables[index]` 里，靠 `valid`/`not_found` 两个位实现**惰性求值与缓存**（含 `not_found` 缓存）；`NOCACHEABLE` 让每次访问强制重算，适合值会变的变量。
- `get_handler` 有三种套路：`offsetof` 直接指向 `r` 内字段（零拷贝）、计算后写请求池、前缀驱动剥离前缀再查列表。
- 标志位决定行为：`CHANGEABLE` 允许同名覆盖、`INDEXED` 让两条路径汇合、`NOHASH` 把变量限制为仅 index 可达、`PREFIX` 走前缀表、`WEAK` 是 `set` 的软注册。
- `map`/`geo` 等「复杂变量」复用同一套注册接口：注册一个目标变量、装上自定义 `get_handler`，求值时再去查映射表；映射值若含变量则用 `valid=0` 标记延迟到运行期二次求值。

## 7. 下一步学习建议

- **u6-l6 过滤器链**：响应头/体过滤器（如 `ngx_http_header_filter_module`）会用到 `$sent_http_*` 系列变量，可对照本讲理解这些「响应侧变量」如何被注册与求值。
- **u10-l3 访问日志与 syslog 输出**：`log_format` 本质是把一串含变量的字符串编译成脚本，请求结束时批量求值——那是本讲「`$name` → index → get_handler」链路的最大规模应用场景。
- **u7-l3 proxy 模块详解**：`proxy_set_header`、`proxy_pass` 的参数都用 `ngx_http_complex_value` 编译，且 proxy 注册了一批 `NOHASH` 变量（`$proxy_host`、`$upstream_addr` 等），是巩固 `NOHASH` 与按 index 查找的好材料。
- **u10-l4 编写自定义 HTTP 模块**：动手在一个自定义模块里用 `ngx_http_add_variable` 注册一个带 `get_handler` 的变量，是检验本讲理解的最佳实践。
- 若想深入「值为变量」的编译细节，可继续阅读 `src/http/ngx_http_script.c` 中 `ngx_http_script_compile` 与 `ngx_http_complex_value` 的实现，那是本讲多次提及但未展开的脚本引擎全貌。
