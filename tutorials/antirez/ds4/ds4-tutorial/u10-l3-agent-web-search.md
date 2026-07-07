# Agent web 搜索

> 讲义 id：`u10-l3` ｜ 依赖：`u10-l2`（Agent 工具系统）｜ 阶段：advanced

## 1. 本讲目标

学完本讲后，你应该能够：

- 读懂 `ds4_web.h` 暴露的三个回调（`confirm` / `log` / `cancel`），并能说清它们各自的触发时机与用途。
- 把握 `ds4_web.c` 里 `google_search` 与 `visit_page` 的完整实现：它如何拉起一个**可见的** Chrome、用 CDP（Chrome DevTools Protocol）驱动它、再把页面抽成 Markdown。
- 看懂 `ds4_agent.c` 如何把这套「浏览器子系统」接进 Agent：注册工具、回填系统提示、处理首次启动浏览器的「人在回路」确认、以及截断超大网页输出。

本讲不重新讲 Agent 的工具解析与可视化（那是 u10-l2 的内容），也不讲推理内核，只讲「Agent 想上网」这条端到端链路。

## 2. 前置知识

在进入源码前，先用大白话讲清三个概念：

1. **CDP（Chrome DevTools Protocol）**：Chrome / Chromium 自带的一套调试协议。程序可以用 HTTP 拿到调试端点列表，再用 WebSocket 给 Chrome 发 JSON 命令（如 `Page.navigate`、`Runtime.evaluate`），从而在真实浏览器里导航、执行 JavaScript、读取页面内容。`ds4_web.c` 没有 link 任何第三方浏览器库，而是**自己手写**了 HTTP 客户端、WebSocket 客户端与 CDP 命令收发——这正是 antirez 的风格。
2. **可见浏览器（visible browser）**：ds4 故意**不用无头（headless）模式**，而是拉起一个有界面的、真实的 Chrome 窗口，使用一个持久化的用户配置目录（profile）。这点很重要：它既是「为什么需要用户确认」的根因，也是 `ds4_web_free` 注释里「less suspicious」（更不易被识别为爬虫）的来源。
3. **回调（callback）**：`ds4_web.c` 是一个**与上层无关的子系统**，它不知道自己被 CLI 用、被 Agent 用、还是被测试用。需要「问用户」「打日志」「被中断」时，它不直接做这些事，而是调用上层塞进来的函数指针。这三个函数指针就是本讲的「web 回调接口」。

如果你还记得 u10-l1 讲的「Agent 是单进程双线程（UI 线程 + worker 线程），推理与工具调用都在 worker 线程内进行」，本讲的确认机制就建立在它之上：worker 线程不能直接读键盘，必须通过条件变量去问 UI 线程。

## 3. 本讲源码地图

| 文件 | 行数量级 | 在本讲的职责 |
| --- | --- | --- |
| [`ds4_web.h`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.h) | 约 33 行 | 子系统的公共头：三个回调类型、`ds4_web_config`、`create/free/google_search/visit_page` 四个公共函数。 |
| [`ds4_web.c`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c) | 约 1385 行 | 浏览器子系统的全部实现：TCP/HTTP/WebSocket 客户端、CDP 命令、拉起 Chrome、跑 JS 抽取 Markdown。 |
| [`ds4_agent.c`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c) | 约 1 万行（本讲只涉及若干片段） | 把浏览器子系统接进 Agent：回调实现、工具 schema、工具分发、输出截断、生命周期。 |

一句话定位：`ds4_web.c` 是「会开 Chrome、会读网页」的独立积木；`ds4_agent.c` 是把它装进 Agent 并加上「人工确认 / 日志 / 可中断」三件套的胶水。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **web 回调接口**（`ds4_web.h`）——子系统对外暴露的「三个口子」。
2. **搜索与访问实现**（`ds4_web.c`）——`google_search` / `visit_page` 怎么真正干活。
3. **agent 集成**（`ds4_agent.c`）——回调怎么实现、工具怎么注册、输出怎么截断。

### 4.1 web 回调接口

#### 4.1.1 概念说明

`ds4_web.c` 被设计成一个「无主见」的子系统：它知道怎么开浏览器、怎么搜索、怎么抓页面，但它**不知道**：

- 出问题时该把日志写到哪里（终端？文件？某个 trace？）；
- 启动一个可见浏览器这么「重」的动作，该不该先问一下人；
- 当前这个长时间的网络等待，是不是该被用户的 Ctrl+C 打断。

这三件事，子系统全部交给调用方决定，方式就是在创建 `ds4_web` 对象时，传入三个函数指针：`confirm`、`log`、`cancel`。这是一种典型的**依赖反转**：底层不依赖上层，而是上层把行为「注入」进来。

#### 4.1.2 核心流程

```text
调用方（如 Agent）
   │  1. 填好 ds4_web_config，挂上三个回调 + privdata
   │  2. ds4_web_create(&cfg)  → 得到一个 ds4_web*
   │
   ├── ds4_web_google_search(web, query)
   │       └─ 内部需要确认/打日志/可中断时，回调上层
   │
   └── ds4_web_free(web)   （注意：不会杀 Chrome，见 4.1.3）
```

三个回调的签名与语义（参见 [ds4_web.h:7-10](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.h#L7-L10)）：

```c
typedef int  (*ds4_web_confirm_fn)(void *privdata, const char *message, char *err, size_t err_len); // 返回 1=同意, 0=拒绝
typedef void (*ds4_web_log_fn)   (void *privdata, const char *message);                              // 纯日志，无返回值
typedef bool (*ds4_web_cancel_fn)(void *privdata);                                                  // 返回 true=请求中断
```

配置结构体把「三个回调」与「三个 privdata」配对存放，外加 `home_dir` 与 `port` 两个非回调参数（[ds4_web.h:12-21](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.h#L12-L21)）。每个回调都配一个独立的 `privdata`，是为了让同一份回调代码能服务不同的「宿主对象」——本讲里 `privdata` 一律是 Agent 的 `agent_worker*`。

#### 4.1.3 源码精读

`ds4_web.h` 整个头文件只有 33 行，是本讲最该逐字读的文件。公共 API 一共四个函数（[ds4_web.h:25-31](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.h#L25-L31)）：

- `ds4_web_create(cfg)`：按配置创建子系统，**不会**立即开浏览器（懒启动，见 4.2）。
- `ds4_web_free(web)`：释放 `ds4_web` 结构体本身。
- `ds4_web_google_search(web, query, err, err_len)`：搜索，成功返回一段 Markdown 字符串（调用方负责 `free`），失败返回 `NULL` 并把原因写进 `err`。
- `ds4_web_visit_page(web, url, err, err_len)`：访问指定 URL，同样返回 Markdown 或 `NULL`。

`ds4_web` 本身是不透明类型（`typedef struct ds4_web ds4_web;`，[ds4_web.h:23](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.h#L23)），调用方看不到内部字段，只能用上面四个函数——这是「窄头」设计，和 u2-l1 讲的 engine/session 不透明指针是同一套路。

进到 `ds4_web.c`，结构体内部把配置原样存下来（[ds4_web.c:40-53](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L40-L53)）：

```c
struct ds4_web {
    char home[PATH_MAX];
    char profile_dir[PATH_MAX];   // Chrome 的 --user-data-dir
    int port;                      // CDP 端口，默认 9333
    pid_t chrome_pid;
    bool browser_allowed;          // 一次性「已获许可」闩
    ds4_web_confirm_fn confirm;  void *confirm_privdata;
    ds4_web_log_fn log;          void *log_privdata;
    ds4_web_cancel_fn cancel;    void *cancel_privdata;
    int next_cdp_id;
};
```

三个回调在子系统内部各有一个薄封装，统一加 NULL 保护：

- `web_log`（[ds4_web.c:121-123](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L121-L123)）：`log` 回调存在才调用。
- `web_cancelled`（[ds4_web.c:131-133](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L131-L133)）：`cancel` 回调存在才询问。
- `web_set_cancel_err`（[ds4_web.c:135-139](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L135-L139)）：若已被取消，就把错误串写成 `"interrupted"` 并返回 true，让调用处的阻塞循环立刻退出。

一个值得专门拎出来的工程细节是**取消的「粒度」**。`web_sleep_ms`（[ds4_web.c:141-150](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L141-L150)）把任意长度的睡眠切成 50ms 一格，每格都查一次 `cancel`：

```c
while (left > 0) {
    if (web_cancelled(web)) return false;
    int step = left < 50 ? left : 50;
    usleep((useconds_t)step * 1000u);
    left -= step;
}
```

这保证了：即使子系统正在 `usleep` 等待页面加载，用户的 Ctrl+C 也能在 50ms 内被感知。这和 u2-l2 讲的「协作式中断」是同一个思路——把「是否该停」的判断点铺满所有可能阻塞的地方。

#### 4.1.4 代码实践

> 实践任务（对应规格里的 practice_task）：阅读 `ds4_web.h` 的回调接口，解释 `confirm` / `log` / `cancel` 三个回调分别用于什么场景，以及为何 web 访问需要用户确认。

1. **实践目标**：不读实现、只读头文件，仅凭签名与字段名推断三个回调的语义，然后到 `ds4_web.c` 里找证据验证你的推断。
2. **操作步骤**：
   1. 打开 [ds4_web.h:7-21](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.h#L7-L21)，看清三个 `typedef` 与 `ds4_web_config`。
   2. 在 `ds4_web.c` 里搜索 `confirm(`、`log(`、`cancel(` 三个调用点，记录每个被调用的**上下文函数**。
   3. 重点看 [web_ensure_browser](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L1105-L1128)（确认），[web_log](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L121-L123) 与它的调用处（日志），[web_set_cancel_err](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L135-L139) / [web_sleep_ms](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L141-L150)（取消）。
3. **需要观察的现象**：
   - `confirm` **只在**「即将首次启动可见 Chrome」时被调用一次，搜索/抓页面本身不触发它。
   - `log` 被用于「Chrome 已就绪」「点了 Google 同意按钮」「关 tab 失败」这类**诊断信息**。
   - `cancel` 在**每一次**可能长时间阻塞的操作前被轮询。
4. **预期结果**（参考答案）：

   | 回调 | 触发场景 | 上层语义（Agent 里） |
   | --- | --- | --- |
   | `confirm` | 子系统即将 spawn 一个**可见** Chrome（`web_ensure_browser` 里 `browser_allowed` 为假时） | 弹给 UI 线程，让用户 y/n 决定是否允许开浏览器 |
   | `log` | 状态/诊断信息，如浏览器就绪、点了 cookie 同意、关 tab 出错 | 转发到 `agent_trace`，写到 trace 文件 |
   | `cancel` | 每个阻塞循环（睡眠、WebSocket 读、等待导航）的开头与切片 | 读 worker 的 `interrupt/stop` 标志，Ctrl+C 时变 true |

5. **为何 web 访问需要用户确认**：把 [web_ensure_browser](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L1105-L1128) 与 [ds4_web_free](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L1346-L1351) 的注释放在一起看，原因有三：
   - **它是真实世界的副作用**：开的是**有界面**的浏览器（macOS 上甚至用 `/usr/bin/open` 拉起真正的 GUI 应用），会用一个持久 profile 目录（`$HOME/.ds4/browser`），并从**用户本人的机器和 IP** 出网。这和「读一个本地文件」完全不同，模型无权擅自发起。
   - **反爬虫风险**：`ds4_web_free` 的注释明说保留 profile「makes repeated web tool calls cheaper and **less suspicious**」——自动化的、由模型触发的真实浏览行为有被目标站点风控的风险，必须由人背书。
   - **不可逆 / 不可静默**：在非交互模式（管道、脚本）下根本没有键盘可问，`agent_web_confirm` 直接返回拒绝（见 4.3），把「能不能上网」牢牢锁在「有人在终端前点头」之后。

> 说明：本实践是「源码阅读型」，不需要真的下载模型或开浏览器即可完成；若要观察运行时行为，需在本机装好 Chrome/Chromium 并跑 `./ds4-agent`，属于「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `ds4_web_config.confirm` 设成 `NULL`，调用 `google_search` 会发生什么？
**答案**：当子系统需要首次开浏览器时，[web_ensure_browser](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L1112-L1117) 检测到 `!web->confirm`，直接把 `err` 设成 `"starting a visible Chrome browser requires interactive approval"` 并返回失败；`google_search` 因此返回 `NULL`，工具报 `Tool error: google_search failed: ...`。换句话说，不挂 `confirm` 回调 ⇒ web 工具永远用不了。

**练习 2**：为什么三个回调各自带一个 `privdata`，而不是共用一个？
**答案**：为了解耦与复用。三个回调可能由不同的代码模块提供、或服务于不同的宿主对象；独立 `privdata` 让每个回调都能拿到自己需要的上下文，而不必强行塞进同一个结构体。本讲里三者 `privdata` 恰好都指向同一个 `agent_worker*`，但接口不强制如此。

---

### 4.2 搜索与访问实现

#### 4.2.1 概念说明

`google_search` 与 `visit_page` 的实现几乎共用同一条管线，差别只在「构造的 URL」和「跑哪段抽取 JS」。这条管线的核心是：**把 Chrome 当成一个「远程的、能执行 JavaScript 的网页渲染器」**，通过 CDP 给它下命令，再把它渲染好的 DOM 抽成 Markdown 文本喂回模型。

为什么不直接 `curl` 抓 HTML？因为现代网页（尤其是 Google 搜索结果页）大量内容是 JS 动态渲染的，纯 HTML 抓下来是一堆空壳；而且反爬机制会针对非浏览器流量。用一个**真实可见的 Chrome** 是对「能不能拿到有用内容」和「不被风控」的折中。

#### 4.2.2 核心流程

一次 `visit_page(url)` 的完整时序（`google_search` 与之同构）：

```text
ds4_web_visit_page(url)
  └─ web_run_page_js(url, extract_page_js, dynamic_scroll=true)
       ├─ web_ensure_browser()        ← 必要时 confirm → spawn Chrome（仅首次）
       ├─ web_open_tab("about:blank") ← 经 /json/version 拿 browser WS，Target.createTarget 建新 tab
       ├─ web_ws_connect(tab.ws_url)  ← 连到这个 page tab 的 WebSocket
       ├─ web_cdp_prepare_page()      ← Page.enable / Runtime.enable / 设视口 / 等就绪
       ├─ web_cdp_navigate(url)       ← Page.navigate
       ├─ web_wait_navigated_ready()  ← 轮询 document.readyState + 文本长度稳定
       ├─ web_cdp_eval_string(click_google_consent_js)  ← 自动点掉 cookie 同意弹窗
       ├─ web_scroll_dynamic_page()   ← visit_page 专属：滚动触发懒加载
       ├─ web_cdp_eval_string(extract_page_js)         ← 把 DOM 抽成 Markdown
       ├─ web_ws_close + web_close_tab                 ← 关连接、关 tab
       └─ return Markdown 字符串
```

关键常量定义在文件开头（[ds4_web.c:29-32](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L29-L32)）：默认端口 `9333`、连接超时 3s、CDP 单步超时 20s、单次结果上限 1MiB。

#### 4.2.3 源码精读

**两个公共函数都极薄**，只负责参数校验与「选 URL / 选 JS」，真正干活的是共享的 `web_run_page_js`：

`google_search`（[ds4_web.c:1353-1372](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L1353-L1372)）：把 `query` 做 URL 编码，拼成 `https://www.google.com/search?q=...`，调用 `web_run_page_js(url, web_extract_search_js, dynamic_scroll=false)`。注意 `dynamic_scroll=false`——搜索结果页不滚动。

`visit_page`（[ds4_web.c:1374-1385](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L1374-L1385)）：直接用调用方给的 `url`，跑 `web_extract_page_js`，且 `dynamic_scroll=true`——访问目标页时会滚动以触发懒加载。

**确认门：`web_ensure_browser`**（[ds4_web.c:1105-1128](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L1105-L1128)）是整条链的第一个分叉：

```c
if (web_cdp_alive(web)) return true;          // 已有可用的 Chrome，直接复用
...
if (!web->browser_allowed) {
    if (!web->confirm) { ...报错 return false; }
    if (!web->confirm(privdata,
          "The web tool wants to start a visible Chrome browser. Allow? (y/n) ", err, err_len))
        return false;                          // 用户拒绝
    web->browser_allowed = true;               // 一次性闩：本次进程内不再问
}
return web_spawn_chrome(web, err, err_len);
```

这里体现了两个设计：① **懒启动**——`ds4_web_create` 不开浏览器，第一次真正要用时才开；② **一次性许可**——`browser_allowed` 一旦置真，后续搜索/访问都不再打扰用户，复用同一个常驻 Chrome。

**拉起 Chrome：`web_spawn_chrome`**（[ds4_web.c:1023-1103](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L1023-L1103)）用 `fork` + `execlp` 启动 Chrome，关键参数是 `--remote-debugging-port`（开 CDP）、`--user-data-dir`（隔离 profile）、`--remote-allow-origins=*`（允许本地连 CDP）。它先 `web_chrome_executable`（[ds4_web.c:963-1010](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L963-L1010)）在常见路径里找 chrome/chromium，找不到才回退到 `google-chrome`，并支持 `DS4_CHROME` 环境变量覆盖。spawn 之后轮询 `web_cdp_alive`（[ds4_web.c:294-301](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L294-L301)）——它打 `GET /json/version`，看到响应里有 `webSocketDebuggerUrl` 就认定浏览器就绪。

**CDP 命令收发：`web_cdp_call`**（[ds4_web.c:627-656](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L627-L656)）是和 Chrome 说话的核心原语：自增一个 `id`，把 `{"id":..,"method":..,"params":..}` 用 `web_ws_send_text` 发出去，然后循环读消息直到 `id` 匹配（`web_json_id_matches`）。所有超时与可中断性都靠 `web_set_cancel_err` + `web_ws_read_message`（[ds4_web.c:556-615](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L556-L615)）里对 `cancel` 的轮询来保证。

**在页面里跑 JS：`web_cdp_eval_string`**（[ds4_web.c:764-785](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L764-L785)）封装 `Runtime.evaluate`，带 `returnByValue` / `awaitPromise`，能把一段返回字符串的异步 JS 的结果取回来。三段抽取逻辑都是用它的：

- **点掉 Google cookie 同意弹窗**：`web_click_google_consent_js`（[ds4_web.c:1207-1215](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L1207-L1215)）——匹配多语言的「Accept all / 同意」按钮并点击，否则搜索结果会被同意墙挡住。
- **抽取搜索结果**：`web_extract_search_js`（[ds4_web.c:1217-1232](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L1217-L1232)）——收集可见链接（过滤 google 自身域名、解析 `/url?q=` 跳转），最多 20 条，再加 1200 字正文快照。
- **抽取正文**：`web_extract_page_js`（[ds4_web.c:1234-1259](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L1234-L1259)）——把 `h1-h6/p/li/pre/blockquote/td` 等块抽成 Markdown，内联 `<a>` 转成 `[text](href)`，`<code>` 转成反引号，末尾附最多 80 条可见链接，正文超 900KB 截断。

整个管线在 `web_run_page_js`（[ds4_web.c:1261-1322](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L1261-L1322)）里串起来，且**任何一步失败都会走统一的 `web_ws_close + web_close_tab + web_tab_free` 清理**，避免留下孤儿 tab。

最后，`ds4_web_free`（[ds4_web.c:1346-1351](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L1346-L1351)）**故意不杀 Chrome**——注释写得很明白：profile 是用户可见状态，留着它能让下次调用更快、也更不易被风控。这把「浏览器生命周期」与「`ds4_web` 对象生命周期」解耦了。

#### 4.2.4 代码实践

1. **实践目标**：在不开浏览器的前提下，理解「同一套 `web_run_page_js` 如何同时服务搜索与访问」。
2. **操作步骤**：
   1. 对照 [ds4_web_google_search](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L1353-L1372) 与 [ds4_web_visit_page](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L1374-L1385)，列出两者传给 `web_run_page_js` 的三参数（`url`、`js`、`dynamic_scroll`）分别是什么。
   2. 在 [web_run_page_js](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L1261-L1322) 里找到 `dynamic_scroll` 这个形参被用在哪一行，解释为什么搜索传 `false`、访问传 `true`。
3. **需要观察的现象**：你会看到 `dynamic_scroll` 只控制是否调用 `web_scroll_dynamic_page`；搜索结果页本身不需要懒加载触发，而很多正文页（评论、无限滚动）需要滚动才会加载内容。
4. **预期结果**：能口述「`google_search = 拼 Google URL + search 抽取 JS + 不滚动`」「`visit_page = 直接给 URL + page 抽取 JS + 滚动`」，并指出二者共用 `web_ensure_browser → open_tab → navigate → 抽取 → close_tab` 这条骨架。
5. 若想真正运行：需本机有 Chrome/Chromium，且 `ds4-agent` 在交互模式下由你确认开浏览器——「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`web_cdp_alive` 为什么用「响应里是否包含 `webSocketDebuggerUrl`」来判断浏览器就绪，而不是看 HTTP 状态码？
**答案**：见 [web_cdp_alive](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L294-L301)。`/json/version` 这个端点在 Chrome 启动早期可能就能连上，但只有当 CDP 真正可用、浏览器对象初始化完成时，响应里才会带 `webSocketDebuggerUrl`。用这个字段做判据比状态码更可靠，能避免「端口已开但还不能用」的假就绪。

**练习 2**：`ds4_web_free` 不杀 Chrome，会不会造成资源泄漏？
**答案**：不会。`ds4_web_free` 只释放 `ds4_web` 这个 C 结构体（[ds4_web.c:1346-1351](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L1346-L1351)）；Chrome 进程是用户的桌面应用，由用户自己关。这是有意为之：常驻 Chrome 让重复调用更快、profile 持久、风控更轻。代价是 Agent 退出后浏览器窗口仍开着——属于可接受的取舍。

---

### 4.3 agent 集成

#### 4.3.1 概念说明

`ds4_web.c` 是「能力」，`ds4_agent.c` 是「把它变成 Agent 工具」。集成要做四件事：

1. **接线**：在 worker 启动时，把三个回调（及其 `privdata`）填进 `ds4_web_config`，调用 `ds4_web_create`。
2. **实现回调**：`confirm` 要跨线程问 UI；`log` 要接到 trace；`cancel` 要接 worker 的中断标志。
3. **注册工具**：在系统提示里写明 `google_search` / `visit_page` 的 schema，并在工具分发表里挂上对应处理函数。
4. **裁剪输出**：网页可能很大，直接塞进对话会爆上下文，需要「头部摘要 + 落临时文件」的处理。

#### 4.3.2 核心流程

```text
worker 启动 (agent_worker_init)
  └─ 填 web_cfg { confirm=agent_web_confirm, log=agent_web_log, cancel=agent_web_cancel, privdata=w }
     └─ w->web = ds4_web_create(&web_cfg)

模型生成工具调用（DSML，见 u10-l2）
  └─ 分发：name=="google_search" → agent_tool_google_search(w, call)
            name=="visit_page"    → agent_tool_visit_page(w, call)
       └─ 调 ds4_web_google_search / ds4_web_visit_page
            └─ 子系统内部回调上层：
                 confirm → agent_web_confirm →（跨线程）问 UI 线程 → 用户 y/n
                 log     → agent_web_log → agent_trace
                 cancel  → agent_web_cancel → worker_should_interrupt

worker 退出 (agent_worker_free)
  └─ ds4_web_free(w->web)
```

#### 4.3.3 源码精读

**接线点**：worker 初始化里构造配置并创建子系统（[ds4_agent.c:9444-9454](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L9444-L9454)）：

```c
ds4_web_config web_cfg = {
    .home_dir = getenv("HOME"),
    .port = 9333,
    .confirm = agent_web_confirm,  .confirm_privdata = w,
    .log     = agent_web_log,      .log_privdata     = w,
    .cancel  = agent_web_cancel,   .cancel_privdata  = w,
};
w->web = ds4_web_create(&web_cfg);
```

`agent_worker_free` 里对称地 `ds4_web_free(w->web)`（[ds4_agent.c:9474](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L9474)）。`w->web` 字段定义在 worker 结构体里（[ds4_agent.c:132](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L132)）。

**cancel 回调最简单**：直接转发给「是否该中断」的统一谓词（[ds4_agent.c:4074-4076](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L4074-L4076)）：

```c
static bool agent_web_cancel(void *privdata) {
    return worker_should_interrupt(privdata);
}
```

`worker_should_interrupt`（[ds4_agent.c:3522-3527](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L3522-L3527)）在锁保护下读 `w->interrupt || w->stop`。于是用户按 Ctrl+C（u10-l1 讲的中断 latch）就能穿透到子系统内部的所有阻塞点。

**log 回调**：把消息前缀加 `web:` 后写进 trace（[ds4_agent.c:4068-4072](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L4068-L4072)）：

```c
static void agent_web_log(void *privdata, const char *message) {
    agent_worker *w = privdata;
    if (!w || !message || !message[0]) return;
    agent_trace(w, "web: %s", message);
}
```

这样「Chrome browser session is ready」之类的子系统日志会和 Agent 自己的 trace 混在一起，便于排查。

**confirm 回调是最有意思的部分**——它必须**跨线程**。回忆 u10-l1：推理与工具在 worker 线程，而键盘输入在 UI 线程。worker 线程不能自己读键盘，于是用一组带锁的字段把「请求」递给 UI 线程，然后 `pthread_cond_wait` 睡等回答（[ds4_agent.c:4033-4066](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L4033-L4066)）：

```c
static int agent_web_confirm(void *privdata, const char *message, char *err, size_t err_len) {
    agent_worker *w = privdata;
    if (!w || w->cfg->non_interactive) {           // 非交互模式直接拒
        snprintf(err, err_len, "visible Chrome browser startup requires interactive approval");
        return 0;
    }
    pthread_mutex_lock(&w->mu);
    w->web_approval_pending = true;                 // 把问题挂出去
    w->web_approval_answered = false;
    ...
    agent_wake_locked(w);                           // 唤醒 UI 线程来看
    while (!w->stop && !w->interrupt && !w->web_approval_answered)
        pthread_cond_wait(&w->cond, &w->mu);        // 等用户回答
    ...
    pthread_mutex_unlock(&w->mu);
    return ok ? 1 : 0;
}
```

UI 线程这一侧用 `worker_take_web_approval_request`（[ds4_agent.c:4078-4088](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L4078-L4088)）取走问题、`worker_answer_web_approval`（[ds4_agent.c:4090-4102](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L4090-L4102)）把答案写回并 `pthread_cond_signal` 唤醒 worker。注意 confirm 也会响应 `interrupt`——用户在等待期间按 Ctrl+C 会被视作拒绝。

**工具 schema**：系统提示里用 JSON 写明两个工具（[ds4_agent.c:761-791](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L761-L791)），并有一句关键说明（[ds4_agent.c:761-762](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L761-L762)）：

> *"The first web call may ask the user for permission to start Chrome."*

这让模型对「第一次调用可能卡在确认」有预期。两个工具的参数都极简：`google_search` 只要 `query`，`visit_page` 只要 `url`。

**工具分发**：和其它工具一起挂在线性的 `strcmp` 链上（[ds4_agent.c:7153-7154](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L7153-L7154)）。`google_search` 的处理函数（[ds4_agent.c:6558-6572](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L6558-L6572)）先发一条系统状态「Searching Google for ...」（走 `agent_publishf_system_status`，在非交互模式下自动静默），再把 Markdown 原样返回。

**`visit_page` 的输出裁剪**是这里最值得学的工程点（[ds4_agent.c:6574-6630](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L6574-L6630)）。它和 bash 工具用同一套「头部 + 临时文件」形状（注释见 [ds4_agent.c:6486-6493](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L6486-L6493)）：

```c
#define AGENT_WEB_HEAD_BYTES (8*1024)   // 头部最多 8KiB
#define AGENT_WEB_HEAD_LINES 100        // 头部最多 100 行
```

流程是：把整页 Markdown 写进 `/tmp/ds4_agent_web_XXXXXX`（`agent_write_temp_text`）→ 只取头 100 行 / 8KiB（`agent_string_head`）→ 若被截断，返回 `output_path=...` + `<head>...</head>`，并提示模型「用 `read path=... raw=true` 查看更多」；若没截断，则用 `<markdown>...</markdown>` 整段返回。这样既给模型足够的上下文起步，又避免一个巨型网页直接吃光 KV 缓存。

**可视化**：和 u10-l2 讲的工具可视化一致，`google_search` 显示前缀是 `google `、`visit_page` 是 `visit `（[ds4_agent.c:2768-2776](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L2768-L2776)），只影响终端画法，不影响 transcript 或 KV。

最后要强调一个**承接 u10-l1/u10-l2 的关键结论**：模型生成的工具调用本身就是 KV 里的 token，工具结果以 `tool` 角色消息追加进 transcript。所以 web 工具的结果（哪怕是从网上抓来的几千字）一旦被采纳，就和普通对话一样进入「唯一真相」transcript 与活 KV，无需 u7 服务器那套精确 DSML 回放——这正是 Agent 垂直架构的红利。

#### 4.3.4 代码实践

1. **实践目标**：跟踪「用户第一次让 Agent 上网」时，跨线程确认是如何完成的。
2. **操作步骤**：
   1. 读 [agent_web_confirm](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L4033-L4066)，标出它①在哪一行把问题挂出、②在哪一行睡等、③`non_interactive` 时直接返回什么。
   2. 读 [worker_take_web_approval_request](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L4078-L4088) 与 [worker_answer_web_approval](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L4090-L4102)，看清 UI 线程如何取问题、送答案。
   3. 用 `grep -n "web_approval" ds4_agent.c` 找出 UI 线程是在哪个事件循环里轮询这个 pending 标志的。
3. **需要观察的现象**：你会看到这是一对「生产者（worker）—消费者（UI）」的握手，`pthread_cond_wait` 与 `pthread_cond_signal` 配对，且 `stop`/`interrupt` 都能打破等待。
4. **预期结果**：能画出时序图——worker 置 `pending=true` 并 `wait` → UI 取问题、读键盘 → UI 置 `answered=true`、`result=允许/拒绝` 并 `signal` → worker 醒来返回。
5. 若运行 `./ds4-agent --non-interactive`（见 [ds4_agent.c:67](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L67) 与 [run_agent_non_interactive](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L9638)），模型一旦调用 `google_search`，confirm 会立刻返回拒绝——「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`visit_page` 为什么不把整页 Markdown 直接塞进对话，而要先落临时文件再只回头部？
**答案**：网页正文可能几十万字，直接进对话会瞬间吃光上下文与 KV 缓存。落临时文件后只回 100 行 / 8KiB 头部（[ds4_agent.c:6495-6496](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L6495-L6496)），模型若需要更多，可以像读普通文件一样用 `read path=... raw=true` 分段查看。这与 bash 工具的输出处理同构，是 Agent 控制「上下文预算」的通用手法。

**练习 2**：在非交互模式下，`google_search` 与 `visit_page` 还能用吗？
**答案**：不能。`agent_web_confirm` 在 `non_interactive` 时直接返回 0（[ds4_agent.c:4036-4040](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L4036-L4040)），`web_ensure_browser` 拿不到许可就不开浏览器，两个工具都会返回 `Tool error: ... failed: visible Chrome browser startup requires interactive approval`。这是把「能上网」严格绑定到「有人在终端前」的设计选择。

---

## 5. 综合实践

**任务：画出「Agent 联网回答一个问题」的完整端到端时序，并标注每一处用到回调的位置。**

假设用户问「请查一下 Rust 1.xx 的发布日期」，模型决定调用 `google_search`，看到结果后又 `visit_page` 某个链接。请完成：

1. **时序图**：从「worker 线程拿到工具调用」到「网页头部 Markdown 进入 transcript」，标出以下每一步发生在哪个文件、哪个函数：
   - 工具分发（`ds4_agent.c`）→ 子系统入口（`ds4_web.c`）→ 确认门 → spawn Chrome → CDP 导航 → JS 抽取 → 输出裁剪。
2. **回调标注**：在这条链上，用三种颜色（或记号）分别标出 `confirm`、`log`、`cancel` 各被调用了几次、在什么条件下。
3. **失败推演**：分别写出以下三种情况下，链路在哪一步、以什么 `err` 文本中止：
   - 非交互模式；
   - 用户在确认时按了 `n`；
   - 用户在等待页面加载时按了 Ctrl+C。
4. **上下文预算**：解释为什么 `visit_page` 的结果进入 transcript 后，不会像「读了一个 10MB 文件」那样撑爆 KV——它与 4.3 的裁剪逻辑、以及 u10-l1 的 KV-as-session 哲学如何配合。

**参考思路**（自己先做再对照）：

- 第 1、2 问的关键节点：分发 [ds4_agent.c:7153-7154](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L7153-L7154) → `agent_tool_google_search` [6558](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L6558) → `ds4_web_google_search` [1353](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L1353) → `web_run_page_js` [1261](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L1261) → `web_ensure_browser` [1105](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L1105)。`confirm` 仅在首次 `web_ensure_browser` 调一次；`log` 在「Chrome 就绪」「点了同意」「关 tab 失败」时各可能一次；`cancel` 在几乎每个阻塞循环里被高频轮询。
- 第 3 问：① 非交互 → confirm 直接返回 0，`err="visible Chrome browser startup requires interactive approval"`；② 拒绝 → 同样在 `web_ensure_browser`，`err="user denied Chrome browser start"`（或自定义拒绝理由）；③ Ctrl+C → `cancel` 返回 true，被 `web_set_cancel_err` 翻译成 `err="interrupted"`，可能发生在导航等待或 WebSocket 读。
- 第 4 问：`visit_page` 只把头部回进 transcript（[6574-6630](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L6574-L6630)），全文躺在临时文件里，模型按需 `read`；而 u10-l1 保证每个进 transcript 的 token 都同步进活 KV，所以「进 KV 的量」就是「头部摘要的量」，可控。

## 6. 本讲小结

- `ds4_web.h` 用三个回调（`confirm` / `log` / `cancel`）把浏览器子系统做成「无主见」的积木：是否开浏览器、日志去哪、能否被打断，全部由调用方注入。
- `confirm` 是**一次性许可门**，仅在首次拉起**可见** Chrome 时触发；`log` 转发诊断信息；`cancel` 在每个阻塞点被高频轮询，靠 50ms 切片实现快速响应 Ctrl+C。
- `google_search` 与 `visit_page` 共用同一条 CDP 管线（`web_run_page_js`），差别只在 URL、抽取 JS、是否滚动；子系统手写了 HTTP/WebSocket/CDP 客户端，不依赖第三方库。
- Agent 侧用「跨线程条件变量握手」实现 `confirm`（worker 问、UI 答），用 trace 实现 `log`，用统一中断谓词实现 `cancel`。
- `visit_page` 用「头部摘要 + 临时文件」控制上下文预算，避免大网页撑爆 KV；web 工具结果作为 `tool` 消息进 transcript，天然享受 KV-as-session，无需服务器的精确回放机制。
- web 工具被严格绑定到「交互模式 + 人工确认」：非交互或被拒绝时整条链直接中止，把「让模型上网」这一高风险动作牢牢锁在人的批准之后。

## 7. 下一步学习建议

- 若你想看「Agent 还有哪些内建工具、DSML 解析与可视化如何统一」，回看 u10-l2；本讲的 `google`/`visit` 前缀正是那里的可视化表的一项。
- 若你对「工具结果如何成为 KV 里的 token、为何不需要精确回放」感兴趣，可结合 u10-l1（KV-as-session）与 u7-l4（服务器的 DSML 精确回放）做对比阅读，体会两种架构在工具调用上的根本差异。
- 若你想继续完善这套手册，下一个自然的主题是「Agent 的会话持久化与 `/save` `/list` `/switch`」——web 工具结果落临时文件、而会话本身落 `.kv` 文件，两者都是「把易变状态外置到磁盘」的同源思想。
- 源码延伸阅读：手写 WebSocket 帧编解码见 [ds4_web.c:510-615](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L510-L615)；动态滚动抓懒加载内容的 JS 见 [web_scroll_dynamic_page](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_web.c#L911-L961)，是理解「为什么用真浏览器而非 curl」的最佳佐证。
