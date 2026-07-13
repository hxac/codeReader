# 请求处理阶段 phases 机制

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 nginx 把一次 HTTP 请求的处理切分成了哪些阶段（phase）、它们的固定顺序是什么。
- 理解配置期容器 `cmcf->phases[]` 与运行期引擎 `cmcf->phase_engine` 的区别，以及后者如何被「拍平」成一维数组。
- 读懂 `ngx_http_core_run_phases` 主循环，并掌握 `checker` 函数对返回值 `NGX_OK / NGX_DECLINED / NGX_AGAIN` 的不同语义。
- 解释 `find_config` 阶段如何选出 `location`、`content` 阶段如何决定由谁来真正生成响应。
- 能够对照源码列出每个阶段都挂了哪些内置模块的 handler。

## 2. 前置知识

本讲建立在 u6-l1（HTTP 模块框架与三层配置）与 u6-l2（HTTP 请求生命周期）之上。开始前请确认你已经了解：

- **`ngx_http_request_t`**：贯穿一次请求的「请求对象」，本讲里它的两个关键字段是 `phase_handler`（当前执行到引擎数组第几项）和 `content_handler`（真正产出响应的处理函数）。
- **`ngx_http_core_main_conf_t`（cmcf）**：HTTP 框架的总仓库，全局唯一。本讲的主角 `phases[]` 数组和 `phase_engine` 都挂在它上面。
- **模块的 `postconfiguration` 回调**：每个 HTTP 模块在 `http{}` 块解析完成后会被调一次这个回调，模块就是利用这个时机把自己的 handler 挂进某个 phase 的（见 u6-l1 中讲过的 `ngx_http_module_t` 八回调）。
- **`ngx_http_handler` 与 `ngx_http_finalize_request`**：请求的「启动器」与「收尾器」（见 u6-l2）。本讲讲的是夹在两者之间、真正干活的阶段流水线。

一句话直觉：**nginx 不让某个模块从头到尾处理请求，而是把请求放到一条固定的「流水线」上，每个模块只在线上属于自己的工位（phase）上做一段事，做完把请求交还流水线。** 这条流水线就是 phase 机制。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/http/ngx_http_core_module.h` | 定义阶段枚举 `ngx_http_phases`、引擎结构 `ngx_http_phase_handler_t` / `ngx_http_phase_engine_t`、以及 cmcf 里的 `phases[]` 数组 |
| `src/http/ngx_http_core_module.c` | 实现运行期主循环 `ngx_http_core_run_phases`、各阶段的 `checker` 函数、请求入口 `ngx_http_handler`、`ngx_http_update_location_config` |
| `src/http/ngx_http.c` | 配置期构建：`ngx_http_block` 调度、`ngx_http_init_phases` 建空数组、`ngx_http_init_phase_handlers` 把二维数组拍平成引擎 |
| `src/http/ngx_http_request.c` | `ngx_http_log_request` 单独驱动 LOG 阶段（它不在主循环里） |
| `src/http/modules/ngx_http_access_module.c` / `ngx_http_rewrite_module.c` / `ngx_http_log_module.c` | 典型模块示例，演示如何在 `postconfiguration` 里把自己挂进某个 phase |

## 4. 核心概念与源码讲解

### 4.1 阶段枚举与配置期容器：11 个阶段从何而来

#### 4.1.1 概念说明

nginx 把一次请求的处理过程，硬编码地切成了 **11 个有序阶段**。阶段的「数量」「名字」「先后顺序」是 nginx 在源码里写死的，用户无法通过配置增删或调换阶段顺序，只能决定每个阶段里「挂哪些模块的 handler」。

这 11 个阶段用枚举 `ngx_http_phases` 表示，从 0 开始递增。注意枚举值在源码里并不是连续排列的——作者用空行把语义相关的阶段分组，方便阅读，但它们仍是连续整数。

有两类阶段需要从一开始就分清：

- **「业务」阶段**：模块可以往里挂自己的 handler，例如 `ACCESS_PHASE`（鉴权）、`CONTENT_PHASE`（生成响应）。
- **「框架」阶段**：没有模块 handler，只有 nginx 自己的 `checker`，用来做「结构性」工作，例如 `FIND_CONFIG_PHASE`（根据 URI 选 location）、`POST_REWRITE_PHASE`（检测 rewrite 死循环）。它们的存在是为了让流水线正确运转，而不是让模块插手。

#### 4.1.2 核心流程

11 个阶段的执行顺序（也是枚举顺序）如下：

```
POST_READ          读完整请求头后的第一站（如改写真实客户端 IP）
SERVER_REWRITE     server 级 rewrite（在选 location 之前）
FIND_CONFIG        根据 URI 匹配 location          ← 框架阶段，无模块 handler
REWRITE            location 级 rewrite
POST_REWRITE       检查是否发生 rewrite 循环        ← 框架阶段
PREACCESS          预限制（如 limit_conn / limit_req）
ACCESS             访问控制（allow/deny、auth_basic、auth_request）
POST_ACCESS        汇总 access 阶段的判定结果        ← 框架阶段
PRECONTENT         内容前置处理（try_files、mirror）
CONTENT            真正生成响应内容（static / index / proxy …）
LOG                记录访问日志（在请求结束时单独触发，不在主循环里）
```

两个要点：

1. `FIND_CONFIG` 排在 `REWRITE` 之前——必须先选出 location，才能执行属于该 location 的 rewrite 规则。
2. `LOG` 虽然是最后一个枚举值，但它**不在 `ngx_http_core_run_phases` 的主循环里执行**，而是在请求即将被销毁时由 `ngx_http_log_request` 单独遍历（见 4.3.2）。这是因为日志必须在响应已经发给客户端、状态码已经确定之后才记。

#### 4.1.3 源码精读

阶段枚举定义在这里，注释与空行体现了阶段分组：

[src/http/ngx_http_core_module.h:110-129](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.h#L110-L129) — 定义 `ngx_http_phases` 枚举，这是全部 11 个阶段的「身份证」。

配置期，这些阶段被组织成 cmcf 里的一个数组，每个元素是一个 `ngx_http_phase_t`，里面装着「本阶段挂了哪些 handler」的动态数组：

[src/http/ngx_http_core_module.h:150-152](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.h#L150-L152) — `ngx_http_phase_t` 只有一个字段 `handlers`（一个 `ngx_array_t`）。

[src/http/ngx_http_core_module.h:178](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.h#L178) — cmcf 里的 `phases[NGX_HTTP_LOG_PHASE + 1]`，即 11 个槽位的数组，下标就是上面的枚举值。

这 11 个数组在 `ngx_http_init_phases` 里被逐个 `ngx_array_init`，每个数组预分配的初始容量不同（例如 access/precontent 给 2，content 给 4），反映作者对该阶段典型 handler 数量的经验估计：

[src/http/ngx_http.c:351-410](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L351-L410) — `ngx_http_init_phases`，给 8 个「会挂 handler」的阶段建空数组（注意它没有为 FIND_CONFIG/POST_REWRITE/POST_ACCESS 这三个框架阶段建数组，因为它们不收模块 handler）。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：在源码中确认「11 个阶段」与「配置期数组」。
2. **步骤**：打开 `ngx_http_core_module.h` 第 110–129 行数枚举成员个数；再打开 `ngx_http.c` 的 `ngx_http_init_phases`，数它对几个阶段调了 `ngx_array_init`。
3. **观察**：枚举有 11 个成员，但 `ngx_http_init_phases` 只对 8 个建了数组。
4. **预期**：少了 `FIND_CONFIG_PHASE`、`POST_REWRITE_PHASE`、`POST_ACCESS_PHASE` 这三个框架阶段——这正好印证「它们不收模块 handler」。
5. 待本地验证：你也可以写一段配置里同时出现 `limit_req`、`allow/deny`、`auth_basic`，再用 `nginx -T` 确认它们能共存，说明它们挂在不同阶段、互不冲突。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `FIND_CONFIG` 必须排在 `REWRITE` 之前？
  - **答案**：location 级的 rewrite 规则属于某个具体 location，必须先用 URI 匹配出 location，才能拿到并执行属于该 location 的 rewrite 指令。
- **练习 2**：用户能不能通过配置新增一个自定义阶段？
  - **答案**：不能。阶段数量与顺序在 `ngx_http_phases` 枚举里写死，用户只能往已有阶段里挂 handler；要新增阶段必须改源码并重新编译。

---

### 4.2 phase_engine 的拍平：`ngx_http_init_phase_handlers`

#### 4.2.1 概念说明

上一节讲的 `cmcf->phases[]` 是一个「二维」结构：11 个阶段，每个阶段一个 handler 数组。但运行期如果每次推进都要先「找当前阶段、再找阶段内第几个 handler」，会很啰嗦。

于是 nginx 在配置解析全部结束后（也就是所有模块都挂完 handler 之后），把这个二维结构**拍平成一维数组** `cmcf->phase_engine.handlers`，数组的每一项是一个三元组：

```c
struct ngx_http_phase_handler_s {
    ngx_http_phase_handler_pt  checker;   // 框架为这一项准备的「检查函数」
    ngx_http_handler_pt        handler;   // 模块真正的处理函数（框架阶段为 NULL）
    ngx_uint_t                 next;      // 「跳到下一个阶段」的目标下标
};
```

关键设计：**每个 handler 项都附带一个 `checker`**。`checker` 是框架按「这一项属于哪个阶段」自动配上的一段胶水代码，它负责调用 `handler` 并解释 `handler` 的返回值（见 4.3）。同一个阶段的所有项共用同一种 `checker`。

[src/http/ngx_http_core_module.h:136-140](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.h#L136-L140) — `ngx_http_phase_handler_t` 结构体定义。

[src/http/ngx_http_core_module.h:143-147](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.h#L143-L147) — `ngx_http_phase_engine_t`，就是「指向拍平数组首元素的指针 + 两个索引」。

`phase_engine` 里还存了两个特殊下标：

- `server_rewrite_index`：`SERVER_REWRITE` 阶段在拍平数组里的起始下标，用于内部重定向时让请求从 server rewrite 重新开始。
- `location_rewrite_index`：`REWRITE` 阶段的起始下标，用于 location 级 rewrite 后跳回 `FIND_CONFIG` 重新选 location。

#### 4.2.2 核心流程

`ngx_http_init_phase_handlers` 的拍平算法（伪代码）：

```
统计总项数 n = 各阶段 handler 数之和 + 框架占位项(find_config/post_rewrite/post_access)
分配长度为 n 的 phase_handler_t 数组
for 每个阶段 i（不含 LOG）:
    根据 i 选定这一项要用的 checker:
        SERVER_REWRITE / REWRITE        -> ngx_http_core_rewrite_phase
        ACCESS                          -> ngx_http_core_access_phase
        CONTENT                         -> ngx_http_core_content_phase
        其它业务阶段(POST_READ/PREACCESS/PRECONTENT) -> ngx_http_core_generic_phase
        FIND_CONFIG / POST_REWRITE / POST_ACCESS     -> 各自专门的 checker（且不挂模块 handler）
    把该阶段注册的每个 handler 写成一项 (checker, handler, next)
    所有项的 next 都指向「下一个阶段」的起始下标
```

两个实现细节，读源码时值得留意：

1. **`next` 指向下一阶段**：同阶段内多项的 `next` 都指向「本阶段之后第一个项」的下标。这样 `checker` 在收到「本阶段已搞定」的信号时，直接 `r->phase_handler = ph->next` 就能跳过本阶段剩余 handler、进入下一阶段。
2. **写入顺序是逆序**：内层循环用 `j` 从 `nelts-1` 递减到 0 写入，因此同一阶段里**后注册的 handler 在数组里反而排在前面、运行时先执行**。这是 nginx 的实现细节，读代码时不必纠结，知道「同阶段内 handler 顺序由构建循环决定」即可。

此外，`use_rewrite` 与 `use_access` 这两个布尔量分别检测「有没有模块挂进 REWRITE / ACCESS 阶段」。只有真的有人挂了，才会插入对应的 `POST_REWRITE` / `POST_ACCESS` 框架项——没人 rewrite 就不需要死循环检测，省一项是一项。

#### 4.2.3 源码精读

`ngx_http_init_phase_handlers` 是本讲最关键的配置期函数：

[src/http/ngx_http.c:454-560](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L454-L560) — 拍平主函数。重点看三处：

- [L467-L476](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L467-L476)：根据 `use_rewrite` / `use_access` 算出总项数 `n`，体现「框架项按需插入」。
- [L490-L547](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L490-L547)：`switch (i)` 为每个阶段选定 `checker`；`FIND_CONFIG` / `POST_REWRITE` / `POST_ACCESS` 三个 `case` 各自直接写一个 checker 项并 `continue`（不进入下面的 handler 写入循环）。
- [L549-L556](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L549-L556)：把阶段的 handler 一项项写入，`ph->next = n` 统一指向下一阶段，循环变量 `j` 逆序递减。

它在 `ngx_http_block` 里的调用位置很关键——必须排在「所有模块 `postconfiguration` 都跑完」之后，否则 handler 还没挂进来，拍平出来是空的：

[src/http/ngx_http.c:294-331](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L294-L331) — 注意顺序：`ngx_http_init_phases`（建空数组，L294）→ 遍历模块调 `postconfiguration`（模块在此往数组里 push handler，L303-L315）→ `ngx_http_init_phase_handlers`（拍平，L329）。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：理解「框架项按需插入」。
2. **步骤**：在 `ngx_http.c` 第 467–476 行，把 `use_rewrite` 与 `use_access` 的取值条件抄下来；再到 517–539 行看 `POST_REWRITE` / `POST_ACCESS` 的 `case` 是如何被 `if (use_rewrite)` / `if (use_access)` 包住的。
3. **观察**：如果你编译的 nginx 完全不启用 rewrite 与 access 相关指令，`use_rewrite=0`、`use_access=0`，引擎数组里就不会有 `post_rewrite` / `post_access` 这两项。
4. **预期**：理解「阶段数量看似固定 11 个，实际引擎数组长度随配置而变」。

#### 4.2.5 小练习与答案

- **练习 1**：拍平数组的每一项里，`checker` 和 `handler` 各承担什么职责？
  - **答案**：`handler` 是模块写的业务函数；`checker` 是框架按阶段配的胶水函数，负责调用 `handler` 并把 `handler` 的返回值翻译成「继续/跳过/结束」等引擎动作。主循环只认 `checker`。
- **练习 2**：为什么 `ngx_http_init_phase_handlers` 必须在所有模块 `postconfiguration` 之后调用？
  - **答案**：模块是在自己的 `postconfiguration` 里把 handler push 进 `phases[].handlers` 的；拍平必须等所有 push 都完成，否则会漏掉 handler。

---

### 4.3 运行时主循环 `ngx_http_core_run_phases` 与 checker 语义

#### 4.3.1 概念说明

配置期把阶段拍平成一维数组后，运行期就简单了：请求带着一个游标 `r->phase_handler`，从数组第一项开始，**逐项调用该项的 `checker`**。`checker` 内部会决定游标如何移动——`+1` 表示「继续本阶段下一个」，跳到 `next` 表示「本阶段结束、进下一阶段」，返回 `NGX_OK` 表示「请求已交出（比如转去异步等待、或已 finalize），主循环退出」。

入口在 `ngx_http_handler`：它把游标初始化好，再把请求的写事件 handler 设成 `ngx_http_core_run_phases`，然后调用一次主循环。注意普通请求从 0 开始；**内部重定向来的请求从 `server_rewrite_index` 开始**，这样会重新跑一遍 server 级 rewrite 与 find_config。

[src/http/ngx_http_core_module.c:840-880](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L840-L880) — `ngx_http_handler`：普通请求 `phase_handler = 0`（L864），内部请求 `phase_handler = server_rewrite_index`（L868），最后 `r->write_event_handler = ngx_http_core_run_phases` 并调用它（L878-L879）。

#### 4.3.2 核心流程

主循环本身极短，精髓在 `checker` 的返回值约定：

```
ngx_http_core_run_phases(r):
    ph = cmcf->phase_engine.handlers
    while ph[r->phase_handler].checker != NULL:
        rc = ph[r->phase_handler].checker(r, &ph[r->phase_handler])
        if rc == NGX_OK:
            return              # 请求已被交出（异步或已收尾），退出本循环
        # 否则 checker 已经自行改好了 r->phase_handler，循环继续
```

数组的末尾被 `ngx_pcalloc` 多留了一个全零项作为哨兵（见 4.2.3 里 `+ sizeof(void *)`），`checker == NULL` 就是结束标志。

不同 `checker` 对 handler 返回值的解释略有不同，但可以归纳为三种典型语义：

| handler 返回 | 含义 | generic/rewrite/access checker 的典型处理 |
| --- | --- | --- |
| `NGX_DECLINED` | 「我不管，交给下一个」 | `phase_handler++`，继续 |
| `NGX_OK` | 「本阶段我已拍板」 | `phase_handler = ph->next`，跳到下一阶段 |
| `NGX_AGAIN` / `NGX_DONE` | 「我要异步等待，先挂起」 | 向主循环返回 `NGX_OK`，让出控制权（等事件就绪后再回来） |
| `NGX_ERROR` / `NGX_HTTP_...` | 「出错了 / 直接给个状态码」 | 调 `ngx_http_finalize_request(r, rc)` 收尾 |

注意 `NGX_OK` 在这里有两层含义，容易混：

- **handler 返回 `NGX_OK`**：表示「本阶段通过，跳到下一阶段」（语义由具体 checker 解释）。
- **checker 返回 `NGX_OK` 给主循环**：表示「本次推进到此为止，请求交出去了」。

主循环只看 checker 的返回值，checker 返回非 `NGX_OK` 就会继续循环；为了让循环继续，checker 在自增/改写 `phase_handler` 后通常返回 `NGX_AGAIN`（主循环把它当作「再来一项」）。

> 关于 LOG 阶段：它**不**经过 `ngx_http_core_run_phases`。拍平时循环上限是 `i < NGX_HTTP_LOG_PHASE`，LOG 阶段的 handler 只挂在 `phases[]` 里、没被拍进引擎数组。它在请求结束时由 `ngx_http_log_request` 单独遍历触发：

[src/http/ngx_http_request.c:4001-4015](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L4001-L4015) — `ngx_http_log_request`：直接遍历 `phases[NGX_HTTP_LOG_PHASE].handlers`，逐个调用，不看返回值（日志模块无权中止请求）。

#### 4.3.3 源码精读

主循环本体只有 10 行：

[src/http/ngx_http_core_module.c:883-902](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L883-L902) — `ngx_http_core_run_phases`：`while (ph[r->phase_handler].checker)` 一行就是整个调度核心。

看一个最典型的 checker——`generic_phase`（被 POST_READ / PREACCESS / PRECONTENT 使用），它完整展示了 `DECLINED → 下一项`、`OK → 跳 next`、`AGAIN/DONE → 交出`、`错误 → finalize` 四种分支：

[src/http/ngx_http_core_module.c:905-939](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L905-L939) — `ngx_http_core_generic_phase`。注意它向主循环返回 `NGX_OK`（L932/L938）表示「交出请求」，返回 `NGX_AGAIN`（L922/L927）表示「请继续下一项」。

对比另一个 checker——`access_phase`，它的语义更复杂，引入了 `satisfy all/any`（全部满足 / 任一满足）两种鉴权模式：

[src/http/ngx_http_core_module.c:1108-1182](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L1108-L1182) — `ngx_http_core_access_phase`：`satisfy all` 下任一 handler 失败即记 `access_code` 并继续（L1158-L1170）；`satisfy any` 下任一 handler 成功即 `phase_handler = ph->next` 跳过本阶段（L1144-L1156）。子请求（`r != r->main`）直接跳过整个 access 阶段（L1115-L1118）。

#### 4.3.4 代码实践（调试日志型）

1. **目标**：亲眼看到请求在各阶段之间流转。
2. **步骤**：用 `--with-debug` 编译 nginx，在配置里加 `error_log /tmp/e.log debug_http;`，发起一次普通 GET 请求。
3. **观察**：在日志里搜索 `generic phase:`、`rewrite phase:`、`access phase:`、`content phase:` 这些由各 checker 打出的 `ngx_log_debug1` 行（对应 L916、L948、L1121、L1306）。
4. **预期**：你会看到 `phase_handler` 这个数字递增的序列，直观感受「请求在引擎数组里逐项前进」。如果某阶段没人挂 handler，对应行不会出现。
5. 待本地验证：具体日志条数取决于你启用的模块；若没看到 `access phase` 行，说明你的 location 没有触发任何 access 模块。

#### 4.3.5 小练习与答案

- **练习 1**：主循环里 `checker` 返回 `NGX_OK` 和 handler 返回 `NGX_OK`，含义相同吗？
  - **答案**：不同。handler 返回 `NGX_OK` 一般表示「本阶段我通过了，请跳到下一阶段」（由 checker 解释）；checker 返回 `NGX_OK` 给主循环表示「请求已交出，本轮推进结束」。
- **练习 2**：为什么 LOG 阶段不走主循环？
  - **答案**：主循环在生成响应、确定最终状态码之前就会结束（content 阶段 finalize 之后）；而日志必须在响应发出、状态码确定之后才能记录，所以 LOG 由 `ngx_http_log_request` 在请求销毁前单独触发，且日志 handler 无权中止流程。

---

### 4.4 content 阶段如何选择处理者

#### 4.4.1 概念说明

前面三个阶段（postread/preaccess/access）的 handler 都是「过客」——做完检查就让请求继续往下走。而 `CONTENT_PHASE` 是特殊的一个：**它要选出「谁来真正生成响应」**。这里有一个容易混淆的区分：

- **「content 阶段的 handler」**：通过 `postconfiguration` 挂进 `phases[NGX_HTTP_CONTENT_PHASE]` 的模块，如 `static`、`index`、`autoindex`、`gzip_static`。它们是「按顺序轮流尝试」的候选者，每个返回 `NGX_DECLINED` 表示「这个文件/情况不归我管」，直到有一个接管。
- **`clcf->handler`（location 的内容处理函数）**：像 `proxy_pass`、`fastcgi_pass`、自定义模块的 content handler，它们不是挂在 content 阶段数组里，而是在配置时直接赋值给当前 location 的 `clcf->handler` 字段。一旦某个 location 设了 `clcf->handler`，它就**独占**这个 location 的响应生成，content 阶段的其它候选者都不会再跑。

桥梁是 `find_config` 阶段：它选出 location 后调用 `ngx_http_update_location_config`，把 `clcf->handler` 拷到请求的 `r->content_handler` 上。随后 content 阶段的 checker `ngx_http_core_content_phase` 第一件事就是检查 `r->content_handler` 是否非空——非空就直接调它，根本不碰阶段数组里的候选者。

#### 4.4.2 核心流程

content 阶段的选择逻辑（伪代码）：

```
# 进入 CONTENT_PHASE 的 checker: ngx_http_core_content_phase
if r->content_handler != NULL:        # location 设了 proxy_pass / 自定义 handler 等
    直接调用 r->content_handler(r)
    finalize 并结束
else:
    按顺序调用 content 阶段数组里的候选 handler（static / index / ...）
    哪个不返回 DECLINED，就用哪个的结果 finalize
    若全都 DECLINED:
        URI 以 '/' 结尾 -> 403 Forbidden（目录禁止列出）
        否则 -> 404（"no handler found"）
```

`r->content_handler` 的来源链：

```
proxy_pass 等指令(配置期) -> clcf->handler = ngx_http_proxy_handler
find_config 阶段(运行期) -> ngx_http_update_location_config
                          -> r->content_handler = clcf->handler
content 阶段(运行期)    -> ngx_http_core_content_phase 优先调用 r->content_handler
```

#### 4.4.3 源码精读

`find_config` 阶段的 checker——它做完 location 匹配后调用 `ngx_http_update_location_config`，这是「把 location 配置同步到请求」的关键一步：

[src/http/ngx_http_core_module.c:969-1061](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L969-L1061) — `ngx_http_core_find_config_phase`：L981 调 `ngx_http_core_find_location` 选 location，L1000 调 `ngx_http_update_location_config` 应用配置。它还顺便做了请求体过大（L1006-L1019）和目录自动重定向（L1021-L1057）的处理。

`ngx_http_update_location_config` 的尾部把 `clcf->handler` 拷给请求：

[src/http/ngx_http_core_module.c:1421-1423](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L1421-L1423) — `if (clcf->handler) { r->content_handler = clcf->handler; }`。

content 阶段的 checker，开头就是「优先用 content_handler」的短路逻辑：

[src/http/ngx_http_core_module.c:1291-1338](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L1291-L1338) — `ngx_http_core_content_phase`：L1299-L1303 若 `r->content_handler` 非空则直接调用并结束；否则遍历候选 handler（L1308），全 DECLINED 时按 URI 是否以 `/` 结尾返回 403（L1326-L1334）或记 "no handler found"。

对照一个把 handler 挂进 content 阶段的模块——`static` 模块：

[src/http/modules/ngx_http_static_module.c:289](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_static_module.c#L289) — 在 `postconfiguration` 里 `ngx_array_push(&cmcf->phases[NGX_HTTP_CONTENT_PHASE].handlers)`，把自己的 handler 加进候选队列。

而对比一个走 `clcf->handler` 路线的模块——`proxy` 模块（解析 `proxy_pass` 指令时直接赋值，不进阶段数组）：

[src/http/modules/ngx_http_proxy_module.c:4314](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_proxy_module.c#L4314) — `clcf->handler = ngx_http_proxy_handler;`。这就是为什么同一个 location 里写了 `proxy_pass` 后，`index`、`static` 等 content 候选都不会再生效——`r->content_handler` 已被独占。

#### 4.4.4 代码实践（配置对照型）

1. **目标**：体会「`clcf->handler` 独占」与「content 阶段候选者轮流尝试」两种模式的差别。
2. **步骤**：准备两个 location——A 只放 `root /usr/share/nginx/html;`（依赖 static/index 候选），B 放 `proxy_pass http://127.0.0.1:8080;`。分别访问，观察返回内容来源。
3. **观察**：A 的请求由 static 模块读磁盘文件返回；B 的请求被 proxy 模块转发给后端，static 模块根本没机会运行。
4. **预期**：B location 里即使存在 `index.html` 文件也不会被 static 返回，因为 `clcf->handler` 已被 `proxy_pass` 设为 `ngx_http_proxy_handler`，`r->content_handler` 非空，content checker 直接短路。
5. 待本地验证：若你在 B 里同时写 `proxy_pass` 与 `try_files`，nginx 会报 `try_files` 不允许在这里出现——因为 `try_files` 属于 PRECONTENT 阶段，而 `proxy_pass` 已独占 content，配置层面就冲突。

#### 4.4.5 小练习与答案

- **练习 1**：`proxy_pass` 的 handler 是挂在 `phases[NGX_HTTP_CONTENT_PHASE].handlers` 数组里的吗？
  - **答案**：不是。它是在解析 `proxy_pass` 指令时直接赋值给 `clcf->handler`，运行期经 `ngx_http_update_location_config` 变成 `r->content_handler`，由 content checker 优先短路调用，根本不进阶段候选数组。
- **练习 2**：一个请求 URI 以 `/` 结尾、且没有 index/autoindex/proxy 等任何内容处理者，最终返回什么？
  - **答案**：当 content 阶段所有候选都 `DECLINED`、且 `r->content_handler` 为空时，`ngx_http_core_content_phase` 检测到 URI 以 `/` 结尾，返回 `403 Forbidden` 并记 "directory index of ... is forbidden"。

---

## 5. 综合实践

**任务**：列出全部 11 个 HTTP 阶段，标注每个阶段由哪些内置模块的 handler 参与处理，并说明 4 个重点阶段（access / rewrite / find_config / content）的工作方式。

**操作步骤**：

1. 在 `src/http/modules/` 下用 grep 搜索 `phases[NGX_HTTP_` 开头的 `ngx_array_push` 调用，统计每个模块挂进了哪个阶段（这正是讲义里用来生成下表的依据）。
2. 补充「框架阶段」：在 `ngx_http.c` 的 `ngx_http_init_phase_handlers` 里确认 `find_config` / `post_rewrite` / `post_access` 这三个阶段对应的 checker 函数名。
3. 对照下表自查。

**参考答案表**（基于当前 HEAD 的源码统计）：

| 阶段 | 类型 | 参与的内置模块 / checker |
| --- | --- | --- |
| POST_READ | 业务 | realip |
| SERVER_REWRITE | 业务 | rewrite（server 级） |
| FIND_CONFIG | 框架 | checker = `ngx_http_core_find_config_phase`（选 location，无模块 handler） |
| REWRITE | 业务 | rewrite（location 级） |
| POST_REWRITE | 框架 | checker = `ngx_http_core_post_rewrite_phase`（rewrite 循环检测，无模块 handler） |
| PREACCESS | 业务 | limit_conn、limit_req、degradation、realip |
| ACCESS | 业务 | access（allow/deny）、auth_basic、auth_request |
| POST_ACCESS | 框架 | checker = `ngx_http_core_post_access_phase`（汇总 satisfy 判定，无模块 handler） |
| PRECONTENT | 业务 | try_files、mirror |
| CONTENT | 业务 | static、gzip_static、index、autoindex、random_index、dav（候选者）；以及经 `clcf->handler` 独占的 proxy / fastcgi / scgi / uwsgi / grpc / memcached / empty_gif / stub_status / flv / mp4 / perl |
| LOG | 业务（特殊） | log（access_log），由 `ngx_http_log_request` 在请求结束时单独触发 |

**重点阶段说明**：

- **find_config**：框架阶段，运行 `ngx_http_core_find_location` 选出 location，再调 `ngx_http_update_location_config` 把 location 配置（含 `clcf->handler`）同步到请求上。location 匹配的细节规则见下一讲 u6-l5。
- **rewrite**：分两处——`SERVER_REWRITE`（选 location 前，处理 server 块里的 rewrite）与 `REWRITE`（选 location 后，处理 location 块里的 rewrite）。两者 checker 都是 `ngx_http_core_rewrite_phase`，rewrite 改写 URI 后由随后的 `POST_REWRITE` 跳回 `FIND_CONFIG` 重新匹配 location（受 `uri_changes` 上限保护，防死循环）。
- **access**：checker 为 `ngx_http_core_access_phase`，支持 `satisfy all`（默认，所有鉴权都过才行）与 `satisfy any`（任一过即可）。鉴权未通过的状态码先存进 `r->access_code`，由 `POST_ACCESS` 统一收尾，并可经 `auth_delay` 做恒定时间延迟以防计时侧信道。
- **content**：checker 为 `ngx_http_core_content_phase`。优先用 `r->content_handler`（被 `proxy_pass` 等独占）；否则让 static/index 等候选者轮流尝试，全部 DECLINED 则按 URI 形态返回 403/404。

## 6. 本讲小结

- nginx 把请求处理硬编码成 **11 个有序阶段**（`ngx_http_phases` 枚举），用户不能增删阶段，只能往业务阶段里挂 handler。
- 配置期有两层结构：`cmcf->phases[]` 是「每阶段一个 handler 数组」的二维容器；解析结束后由 `ngx_http_init_phase_handlers` **拍平**成一维的 `cmcf->phase_engine.handlers`，每项是 `(checker, handler, next)` 三元组。
- 框架为每个阶段配一种 `checker`（generic/rewrite/access/content 等），`checker` 调用模块 handler 并解释其返回值：`DECLINED`→下一项、`OK`→跳 `next` 进下一阶段、`AGAIN/DONE`→异步交出、`ERROR/HTTP_*`→finalize。
- 运行期主循环 `ngx_http_core_run_phases` 极简：靠游标 `r->phase_handler` 在引擎数组里逐项推进，直到 `checker == NULL` 哨兵；普通请求从 0 开始，内部重定向请求从 `server_rewrite_index` 重新开始。
- `FIND_CONFIG` / `POST_REWRITE` / `POST_ACCESS` 是**框架阶段**，没有模块 handler，只做选 location、防循环、汇总鉴权等结构性工作，且仅在对应业务阶段「有人挂 handler」时才插入。
- `content` 阶段特殊：`r->content_handler`（来自 `clcf->handler`，如 `proxy_pass`）非空时独占响应生成；否则让 static/index 等候选者轮流尝试。`LOG` 阶段则完全在主循环之外，由 `ngx_http_log_request` 在请求结束时单独触发。

## 7. 下一步学习建议

- **u6-l5 location 匹配与配置合并**：本讲的 `FIND_CONFIG` 阶段调用了 `ngx_http_core_find_location`，下一讲会详细拆解前缀/正则/精确 location 的匹配优先级与 `merge_loc_conf` 的配置继承机制。
- **u6-l6 过滤器链 header/body filter**：content 阶段生成响应后，响应数据要经过一条「过滤器链」才写到 socket，那是与 phase 流水线正交的另一条流水线。
- **u7-l1 upstream 框架**：`proxy_pass` 设的 `clcf->handler`（本讲的 content_handler）进入的是 upstream 子系统，upstream 有自己的一套状态机，值得作为进阶。
- **延伸阅读**：在 `src/http/modules/` 下任意挑一个模块（如 `ngx_http_limit_req_module.c`），看它的 `postconfiguration` 把 handler 挂进哪个阶段、handler 返回什么值，把本讲的语义用在一个真实模块上验证一遍。
