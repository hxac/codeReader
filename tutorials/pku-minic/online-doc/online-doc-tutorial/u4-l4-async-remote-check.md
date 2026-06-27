# u4-l4 远程链接的异步校验与片段验证

## 1. 本讲目标

本讲是链接检查器四连讲的第三篇，承接 [u4-l2 链接提取子系统](u4-l2-link-extraction.md)：上一讲我们已经能把文档里的链接一条条提取出来（带行列号的 `Link`），并分清了「本地链接」和「远程链接」。

本讲只解决一个问题：**对远程链接（`http://`、`https://`、`//` 开头），怎么高效又可靠地判断它是否有效，甚至验证它指向的页面内锚点是否存在？**

学完后你应该能够：

1. 读懂检查器如何用 `asyncio` + `httpx` 并发校验成百上千条远程链接，并用信号量（Semaphore）限流。
2. 解释「先 HEAD 后 GET 回退」这一请求策略的动机与实现。
3. 看懂 `evaluate_remote_response` 的判定顺序，并回答一个反直觉问题：**为什么返回 `403` 的链接会被判为通过**。
4. 理解远程锚点（fragment）验证的逻辑：什么情况下抓页面、什么情况下跳过、什么情况下报「锚点不存在」。

---

## 2. 前置知识

在进入源码前，先用通俗语言过一遍本讲依赖的几个概念。

### 2.1 同步 vs 异步

- **同步**：发一个请求，死等返回，再发下一个。100 条链接要排队，慢。
- **异步**：发出去不等，先去发下一个，等谁先回来就先处理谁。一份网络等待时间可以「重叠」起来，吞吐量高得多。Python 里用 `asyncio` 框架，函数用 `async def`，调用时用 `await`。

### 2.2 HTTP 的 HEAD 与 GET

- `GET`：请求资源**本体**，服务器会把正文（HTML、图片等）发回来。
- `HEAD`：只请求资源的**元信息**，正文不发。常用于「我只想知道这个链接还在不在、有多大、什么类型」，省带宽。

很多服务器对 `HEAD` 和 `GET` 的处理不完全一致：有的禁用 `HEAD`，有的对 `HEAD` 返回 405，有的反爬策略对二者反应不同。所以本检查器**先用 HEAD 试探，必要时再退回 GET**。

### 2.3 状态码与 fragment

- **HTTP 状态码**：3 位数。`2xx` 成功、`3xx` 重定向、`4xx` 客户端错误（`404` 不存在、`403` 禁止访问、`401` 需认证、`429` 限流）、`5xx` 服务器错误。
- **fragment（片段/锚点）**：URL 中 `#` 后面的部分，如 `https://example.com/page#section`。浏览器用它跳到页面内某个 `id="section"` 的元素。注意：远程链接的锚点是 `#` 后缀，而本仓库**本地 Docsify 链接**用 `?id=xxx`（见 u4-l3）。本检查器用 `urldefrag` 拆 `#`，所以只认 `#` 形式的远程锚点。

### 2.4 信号量（Semaphore）

可以理解成一个「同时最多放 N 个人进去」的计数器：每个任务进入前 `acquire`（计数 -1），出来 `release`（计数 +1）；满了就排队等。在异步并发里，它用来**限制同时在飞的请求数**，避免一次性轰垮目标服务器或耗尽本机连接。

---

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 作用 |
|------|------|
| [scripts/check_links.py](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py) | 仓库唯一的程序代码；本讲关注其中远程校验相关的一组函数 |

配置文件和依赖提供参数依据：

| 文件 | 作用 |
|------|------|
| [check-links.toml](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/check-links.toml) | `[http]` 段给出超时、并发数、可接受状态码等参数 |
| [requirements.txt](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/requirements.txt) | 声明 `httpx`，本讲的全部网络能力都由它提供 |

本讲涉及的函数清单（均在 `scripts/check_links.py` 内）：

| 函数 | 行号 | 职责 |
|------|------|------|
| `check_remote_links` | L613–L646 | 并发编排：建客户端、限流、并发跑所有远程链接、汇总 Issue |
| `check_remote_link` | L594–L610 | 单条链接的 HEAD/GET 回退调度 |
| `check_remote_by_get` | L580–L591 | GET 回退分支的封装 |
| `request_with_retries` | L534–L545 | 给单次请求加重试 |
| `request_once` | L515–L531 | 真正发起一次流式请求，控制是否读正文 |
| `status_is_accepted` | L507–L512 | 状态码是否在可接受集合内 |
| `is_html_content` | L548–L550 | 判断响应是否是 HTML（要不要抓锚点） |
| `parse_remote_anchors` | L553–L559 | 从远程页面正文解析出所有锚点 |
| `evaluate_remote_response` | L562–L573 | 综合判定：状态码 + 锚点，返回 `RemoteResult` |

---

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块，按「数据怎么流」的顺序讲解：先看并发骨架（4.1），再看单条链接的请求策略（4.2），然后是判断正反两件事——是不是 HTML（4.3）、锚点在不在（4.4），最后是贯穿始终的状态码规则（4.5）。

### 4.1 异步并发与信号量

#### 4.1.1 概念说明

文档站里远程链接很多（GitHub、各种规范、工具官网……），如果一条条同步去 `requests.get`，全部跑完可能要几分钟。本模块解决：**如何把所有远程链接并发地、但受控地校验完**。

「并发」靠 `asyncio`：用一个事件循环同时挂起多个网络等待。「受控」靠两层限制：

- **信号量 `asyncio.Semaphore`**：限制同时在执行的校验任务数。
- **`httpx.Limits`**：限制底层连接池的连接数。

两者在本检查器里都设成同一个值 `workers`，属于「双保险」。

#### 4.1.2 核心流程

`check_remote_links` 的整体编排：

1. 从 `http_config` 读 `workers`（并发数，默认 8，本仓库 toml 里是 16）、`timeout`、`user_agent`。
2. 构造公共请求头（`Accept`、`User-Agent`）和连接限制 `httpx.Limits`。
3. 创建信号量 `semaphore = asyncio.Semaphore(workers)`。
4. `async with` 打开一个 `httpx.AsyncClient`（开 `follow_redirects=True`，自动跟随 3xx 重定向）。
5. 定义内部协程 `one(url)`：`async with semaphore` 限流后调用 `check_remote_link`。
6. `asyncio.gather` 把所有 URL 的 `one` 协程**一次性并发启动**，等全部完成。
7. 遍历结果，把失败的（`result.ok is False`）按每个出现位置生成 `Issue`。

#### 4.1.3 源码精读

并发骨架：[scripts/check_links.py:L613-L646](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L613-L646) —— 这是本讲的入口，建客户端、限流、并发、汇总。

关键的几行：

```python
concurrency = int(http_config.get('workers', 8))
limits = httpx.Limits(max_connections=concurrency,
                      max_keepalive_connections=concurrency)
semaphore = asyncio.Semaphore(concurrency)

async with httpx.AsyncClient(follow_redirects=True, timeout=timeout,
                             headers=headers, limits=limits) as client:
    async def one(url: str) -> tuple[str, RemoteResult]:
        async with semaphore:                      # 拿到「通行证」才发请求
            ...
            result = await check_remote_link(client, url, http_config)
            ...
            return url, result

    results = await asyncio.gather(
        *(one(url) for url in sorted(remote_links)))
```

几个要点：

- **`async with semaphore`** 是限流的核心：信号量初始值就是 `concurrency`，每进入一个 `one` 减 1，出去加 1；当已有 `concurrency` 个任务在跑时，第 `concurrency+1` 个会卡在 `async with semaphore` 这里排队，直到有人退出。
- **`sorted(remote_links)`**：URL 按字典序排序，保证每次运行顺序确定（可复现），不依赖字典插入顺序。
- **双层限制**：`Semaphore` 限制「逻辑任务数」，`httpx.Limits` 限制「物理连接数」。两者同值时，起作用的主要是信号量；保留 `Limits` 是稳妥的连接池上限。
- **去重已在上游完成**：`remote_links` 是 `dict[url, list[Link]]`，同一个 URL 只校验一次，但失败时会为「所有引用它的位置」各生成一个 Issue（见末尾的 `for link in remote_links[url]` 循环）。

注意：调用方 `check_links` 用 `asyncio.run(...)` 启动这个协程（[scripts/check_links.py:L685-L687](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L685-L687)），因为检查器主体是同步代码，只在远程校验这一段切到异步世界。

#### 4.1.4 代码实践

**目标**：直观感受「并发数」对远程校验耗时的影响。

1. 安装依赖：`pip install -r requirements.txt`。
2. 先用默认配置（`workers=16`）跑一次并计时：
   ```
   time python scripts/check_links.py --config check-links.toml --verbose
   ```
3. 临时把 `check-links.toml` 里 `[http]` 的 `workers` 改成 `1`，再跑一次并计时。
4. 把 `workers` 改回 16。

**需要观察的现象**：`--verbose` 会逐条打印 `Checking remote link: <url> ...`，`workers=1` 时这些行近似串行出现；计时的「真实时间（real）」在并发时明显更短。

**预期结果**：并发版（16）的真实时间显著小于串行版（1）。具体数值取决于网络与本仓库远程链接数量，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果只保留 `httpx.Limits` 而删掉 `asyncio.Semaphore`，并发数还会被限制吗？

> **答案**：会，但限制点不同。`Limits` 卡在底层连接池；没有信号量时，逻辑上会有远多于 `workers` 个 `one` 协程同时进入并排队等连接，连接池仍是 `workers`。两者保留是「双保险」，信号量更早地在任务入口处节流，减少无谓的排队协程。

**练习 2**：为什么 URL 要先 `sorted` 再 `gather`，而不是直接遍历字典？

> **答案**：字典的迭代顺序在 Python 3.7+ 是插入序，但插入序取决于文档扫描顺序，不可控；排序后每次运行、每台机器上的校验顺序一致，便于复现问题、对比两次运行的输出。

---

### 4.2 HEAD/GET 回退策略

#### 4.2.1 概念说明

校验一条远程链接，最省事的做法是发 `HEAD`：只要状态码 OK 就算通过，不下载正文。但现实中很多服务器对 `HEAD` 不友好（直接报错、返回 405、或反爬直接拦截）。本模块解决：**怎么在「尽量省」和「尽量准」之间做回退**。

策略是：

- 默认发 **HEAD**（不读正文）。
- 如果「需要验证锚点」或「HEAD 的状态码不被接受」，就升级为 **GET**。
- 如果 HEAD 直接抛网络异常（服务器拒收 HEAD），整体退到 GET 分支。
- GET 也有重试。

另外，GET 在不需要读正文时，会带上 `Range: bytes=0-0` 请求头，只下载 **1 个字节**，进一步省流量（见 4.2.3）。

#### 4.2.2 核心流程

`check_remote_link` 的决策树（伪代码）：

```
base_url, fragment = urldefrag(url)            # 拆掉 #fragment
needs_fragment = 有 fragment 且 配置开了 check_fragments

try:
    head = 发 HEAD(不读正文)
except 网络异常:
    return GET 回退分支                          # 服务器拒收 HEAD

if needs_fragment 或 HEAD状态码不被接受:
    try:
        get = 发 GET(按 needs_fragment 决定读不读正文)
        return evaluate(fragment, get, needs_fragment)
    except 网络异常:
        if HEAD状态码可接受:
            return 通过("HEAD 成功，锚点无法验证")
        else:
            return 失败
else:
    return evaluate(fragment, head, 不需要锚点)
```

重试包裹在最外层 `request_with_retries`：失败后 `sleep` 一小段再试，重试次数由配置 `retries` 控制。

#### 4.2.3 源码精读

单条链接调度：[scripts/check_links.py:L594-L610](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L594-L610) —— HEAD/GET 回退的全部决策都在这里。

精简后的主干：

```python
async def check_remote_link(client, url, http_config) -> RemoteResult:
  base_url, fragment = urldefrag(url)
  needs_fragment = bool(fragment) and bool(http_config.get('check_fragments', True))
  try:
    head = await request_with_retries(client, base_url, 'HEAD', http_config, False)
  except httpx.HTTPError:
    return await check_remote_by_get(client, base_url, fragment, http_config, needs_fragment)
  if needs_fragment or not status_is_accepted(head.status, http_config):
    try:
      get = await request_with_retries(client, base_url, 'GET', http_config, needs_fragment)
      return evaluate_remote_response(fragment, get, http_config, needs_fragment)
    except httpx.HTTPError as error:
      if status_is_accepted(head.status, http_config):
        return RemoteResult(True, 'HEAD succeeded; fragment could not be verified', head.status)
      return RemoteResult(False, f'request failed after HTTP {head.status}: ...', head.status)
  return evaluate_remote_response(fragment, head, http_config, False)
```

要点：

- **`urldefrag`** 把 `#fragment` 切出来；`base_url` 才是真正去请求的地址。fragment 永远不发给服务器（HTTP 规范如此），只在本地比对。
- **触发 GET 的两个条件**（`if needs_fragment or not status_is_accepted(...)`）：要么「要验证锚点」必须拿到正文，要么「HEAD 状态码不被接受」想用 GET 再确认一次（有些站点 HEAD 返回 405 但 GET 正常）。
- **GET 失败时的兜底**：若 HEAD 本身状态码可接受，就乐观地认为链接还在（「HEAD 已成功，只是锚点没法验证」），避免因 GET 阶段的瞬时网络抖动误杀一条本可通过的链接。

单次请求与重试：[scripts/check_links.py:L515-L545](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L515-L545) —— `request_once` 用流式读取控制正文下载量，`request_with_retries` 包重试。

```python
async def request_once(client, url, method, http_config, read_body) -> RemoteResponse:
  headers = {'Range': 'bytes=0-0'} if method == 'GET' and not read_body else None
  max_bytes = int(http_config.get('max_fragment_page_bytes', 2_000_000))
  async with client.stream(method, url, headers=headers) as response:
    body = b''
    if read_body:
      async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > max_bytes:        # 超过上限就截断，防巨型页面撑爆内存
          body = body[:max_bytes]
          break
    return RemoteResponse(response.status_code,
                          response.headers.get('content-type', ''),
                          body, str(response.url))
```

要点：

- **`Range: bytes=0-0`**：当是 GET 且 `read_body=False`（只确认存在性、不验证锚点）时，只请求第 1 个字节，把「确认链接存活」的代价压到最低。
- **`client.stream(...)`**：流式响应，配合 `aiter_bytes()` 边收边截断，避免把一个超大 HTML 全读进内存。上限 `max_fragment_page_bytes` 默认 2MB。
- **重试退避**：`request_with_retries` 里失败后 `await asyncio.sleep(0.5 * (attempt + 1))`，即第 1 次重试前等约 0.5s、第 2 次等约 1.0s，简单线性退避。

#### 4.2.4 代码实践

**目标**：观察「HEAD 不被接受时退回 GET」的效果。

1. 阅读函数 [check_remote_link](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L594-L610)。
2. 在 `check_remote_link` 的 `if needs_fragment or not status_is_accepted(...)` 分支入口处，**临时**加一行调试打印（本地实验，勿提交）：`print(f'[debug] HEAD={head.status} -> falling back to GET for {base_url}')`。
3. 运行 `python scripts/check_links.py --config check-links.toml --verbose 2>&1 | grep '\[debug\]'`。
4. 实验后还原该行。

**需要观察的现象**：哪些链接触发了 GET 回退。带 `#fragment` 的远程链接一定会触发（因为 `needs_fragment` 为真）；状态码非 2xx/3xx 的 HEAD 也会触发。

**预期结果**：带锚点的远程链接会出现在回退日志里；不带锚点且 HEAD 返回 2xx/3xx 的不会出现。**待本地验证**（取决于你本地的网络与具体链接）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `request_once` 在 GET 但不读正文时要发 `Range: bytes=0-0`，而 HEAD 时却不需要？

> **答案**：HEAD 按定义就不返回正文，服务器天然不发 body，无需 Range。GET 默认会返回完整正文，所以用一个只取 1 字节的 Range 头，把 GET「降级」成近似 HEAD 的代价，同时绕过那些「只对 HEAD 报错、对 GET 正常」的服务器。

**练习 2**：GET 阶段抛了网络异常，但 HEAD 阶段状态码是 200，最终结果是「通过」还是「失败」？

> **答案**：通过。代码走 `if status_is_accepted(head.status, http_config)` 为真分支，返回 `RemoteResult(True, 'HEAD succeeded; fragment could not be verified', ...)`。设计意图是：HEAD 已证明链接存活，GET 的失败大概率是瞬时网络问题，不该误杀。

---

### 4.3 HTML 内容判定

#### 4.3.1 概念说明

只有当响应是 **HTML 页面**时，抓取正文、在里面找锚点才有意义。如果链接指向一张图片、一个 PDF、一个 JSON 接口，正文里根本没有 HTML 锚点，强行抓只会误报。本模块解决：**怎么判断一个远程响应是不是 HTML**。

判定有三条线索：响应头的 `content-type`、URL 路径后缀。满足任一即视为 HTML。

#### 4.3.2 核心流程

```
lowered = content_type 转小写
是 HTML 当且仅当：
    lowered 含 'text/html'
    或 lowered 含 'application/xhtml+xml'
    或 URL 路径以 .html / .htm / / 结尾
```

第三条是「兜底」：有些服务器返回的 `content-type` 不规范（比如 `application/octet-stream`），但 URL 明显是个 `.html` 文件或目录根 `/`，仍当作 HTML 处理。

#### 4.3.3 源码精读

[scripts/check_links.py:L548-L550](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L548-L550) —— 三条线索的合取判断：

```python
def is_html_content(content_type: str, url: str) -> bool:
  lowered = content_type.lower()
  return 'text/html' in lowered \
      or 'application/xhtml+xml' in lowered \
      or urlparse(url).path.endswith(('.html', '.htm', '/'))
```

注意第二个参数用的是 `response.final_url`（重定向后的最终地址，见 4.4.3 的调用），这样即使原链接被 301 到一个 `.html`，也能正确识别。

这个函数只在 `evaluate_remote_response` 里被调用一次（见 4.4），用来决定「要不要跳过锚点验证」。

#### 4.3.4 代码实践

**目标**：用 Python 交互式验证三条判定线索。

打开 `python`，导入并手动测试（这是「示例代码」，非项目原有）：

```python
# 示例代码：手动验证 is_html_content 的判定
from urllib.parse import urlparse
def is_html_content(content_type, url):
    lowered = content_type.lower()
    return ('text/html' in lowered
            or 'application/xhtml+xml' in lowered
            or urlparse(url).path.endswith(('.html', '.htm', '/')))

print(is_html_content('text/html; charset=utf-8', 'https://a.com/p'))   # True（content-type）
print(is_html_content('application/pdf', 'https://a.com/x.pdf'))        # False
print(is_html_content('application/octet-stream', 'https://a.com/x.html'))  # True（后缀兜底）
print(is_html_content('', 'https://a.com/dir/'))                        # True（路径以 / 结尾）
```

**需要观察的现象**：四条断言的 True/False 是否符合预期。

**预期结果**：`True / False / True / True`，与代码逻辑一致。

#### 4.3.5 小练习与答案

**练习 1**：一个链接返回 `content-type: image/png`，URL 是 `https://a.com/banner`（无后缀），`is_html_content` 返回什么？

> **答案**：`False`。content-type 不含 html/xhtml，路径后缀也不是 `.html/.htm//`。于是锚点验证会被「跳过」（见 4.4），图片链接只要状态码 OK 即通过。

**练习 2**：为什么判断后缀时要带上 `/` 结尾这一项？

> **答案**：目录根 URL（如 `https://a.com/section/`）通常由服务器返回目录首页（HTML），但其 content-type 不一定规范。把「以 `/` 结尾」也认作 HTML，能让这类常见链接的锚点验证继续进行，而非被草率跳过。

---

### 4.4 远程锚点验证

#### 4.4.1 概念说明

当远程链接带 `#fragment` 时，光知道「页面能打开」还不够，最好还能确认页面里**真有这个锚点**（比如 `#install` 确实对应某个 `id="install"` 的标题）。本模块解决：**怎么在远程 HTML 页面里查找锚点，以及什么时候查、什么时候不查**。

和本地侧（u4-l3）一样，远程侧也复用了两个工具函数：

- `parse_html_anchors`：用 `selectolax` 选出所有带 `id` 或 `name` 的元素，收集它们的值。
- `expand_anchor_forms`：把一个锚点字符串扩展成多种等价形式（NFC 归一、小写、URL 编解码），应对「标题大小写、空格转连字符、中文锚点」等差异。

这两个函数在 u4-l3 已详述，这里只在远程语境下使用。

#### 4.4.2 核心流程

`evaluate_remote_response` 是本模块与 4.5 的交汇点，它的判定按**优先级从高到低**短路返回：

```
1. 状态码不被接受             → 失败 "HTTP {status}"
2. 不需要验证锚点             → 通过 "ok"
3. 状态码在 accepted_statuses → 通过 "fragment not verified"   ← 403/401/429 走这里
4. fragment 是 :~:text= 或非HTML → 通过 "fragment skipped"
5. 锚点不在页面里             → 失败 "fragment not found"
6. 否则                       → 通过 "ok"
```

注意第 3 条和第 4 条都是「通过但没真验证锚点」，只是原因不同。只有第 5 条会因「锚点缺失」而失败。

#### 4.4.3 源码精读

综合判定函数：[scripts/check_links.py:L562-L573](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L562-L573) —— 状态码、锚点、HTML 三类判定的总开关：

```python
def evaluate_remote_response(fragment, response, http_config, needs_fragment) -> RemoteResult:
  if not status_is_accepted(response.status, http_config):
    return RemoteResult(False, f'HTTP {response.status}', response.status)
  if not needs_fragment:
    return RemoteResult(True, 'ok', response.status)
  if response.status in set(http_config.get('accepted_statuses', [])):
    return RemoteResult(True, f'HTTP {response.status}; fragment not verified', response.status)
  if fragment.startswith(':~:text=') or not is_html_content(response.content_type, response.final_url):
    return RemoteResult(True, 'fragment skipped', response.status)
  if parse_remote_anchors(response).isdisjoint(expand_anchor_forms({fragment})):
    return RemoteResult(False, f'HTTP {response.status}, but fragment "{unquote(fragment)}" was not found', response.status)
  return RemoteResult(True, 'ok', response.status)
```

锚点解析：[scripts/check_links.py:L553-L559](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L553-L559) —— 从响应正文解码、抽锚点、扩形式：

```python
def parse_remote_anchors(response: RemoteResponse) -> set[str]:
  charset = 'utf-8'
  match = re.search(r'charset=([^;\s]+)', response.content_type, flags=re.IGNORECASE)
  if match:
    charset = match.group(1).strip('"\'')
  return expand_anchor_forms(parse_html_anchors(response.body.decode(charset, errors='ignore')))
```

要点：

- **`fragment.startswith(':~:text=')`**：这是 W3C 的「文本片段（Text Fragments）」语法（如 `#:~:text=安装`），它匹配的是页面正文文本而非某个 `id`，无法用锚点集合验证，故跳过。
- **`isdisjoint`**：两个集合不相交即「锚点不存在」。`expand_anchor_forms({fragment})` 把链接里的锚点扩成多种形式，`parse_remote_anchors` 把页面里所有 `id/name` 也扩成多种形式，两边只要有一种能对上就算命中（与本地侧 u4-l3 的「超集匹配」思路一致）。
- **`charset` 解码**：从 content-type 头里抠出字符集（如 `text/html; charset=gbk`），用对应编码解码 body；抠不到就默认 utf-8，且 `errors='ignore'` 容错。
- **`response.final_url`**：传给 `is_html_content` 的是重定向后的最终 URL，确保后缀判定准确。

GET 回退封装：[scripts/check_links.py:L580-L591](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L580-L591) —— HEAD 异常时走这里，发 GET 后同样交给 `evaluate_remote_response` 判定，保证两条路径的结论口径一致。

#### 4.4.4 代码实践

**目标**：体会「带 fragment 的远程链接会被实际抓页验证锚点」。

1. 在 `docs/` 下**临时**新建一个测试 `.md`（如 `docs/_tmp_remote.md`，实验后删除），写入一条指向真实页面且带锚点的远程链接，例如：
   ```markdown
   测试锚点：<https://docs.python.org/3/tutorial/controlflow.html#defining-functions>
   ```
   再写一条故意写错锚点的：
   ```markdown
   错误锚点：<https://docs.python.org/3/tutorial/controlflow.html#this-anchor-does-not-exist>
   ```
2. 运行：
   ```
   python scripts/check_links.py --config check-links.toml --root . --verbose
   ```
3. 观察输出中这两条链接的判定。
4. 实验后删除 `docs/_tmp_remote.md`。

**需要观察的现象**：第一条应通过；第二条应报 `remote-http: HTTP 200, but fragment "..." was not found`。

**预期结果**：锚点命中→通过；锚点缺失→失败，并给出 `HTTP 200, but fragment ... was not found`。若目标站点对脚本返回非 2xx 或拦截，可能落到 4.5 的「状态码/fragment skipped」分支，**以本地实际输出为准**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `#:~:text=...` 这种 fragment 要跳过，而不是去找锚点？

> **答案**：文本片段匹配的是页面里的一段**可见文字**，不是某个元素的 `id`/`name`。本检查器的锚点集合只收集 `id`/`name` 属性，无法验证文本片段，强行比对必然「查无」，会误报，所以直接跳过判通过。

**练习 2**：`evaluate_remote_response` 在第 5 步用 `isdisjoint` 判断「锚点不存在」。如果改用 `<=`（子集）会有什么问题？

> **答案**：语义不同。`isdisjoint` 是「两边没有任何一个共同元素」，等价于「页面的锚点集合和链接锚点形式集合完全对不上」→ 报缺失。用 `<=` 会要求「链接锚点的所有形式都得出现在页面里」，方向错了，且只要页面缺其中一种扩形式就误报。正确口径是「至少有一种形式能对上」，即 `not isdisjoint`。

---

### 4.5 状态码接受规则

#### 4.5.1 概念说明

「这个 HTTP 状态码算不算通过」是贯穿 4.2~4.4 的底层问题。本模块集中讲清楚规则，并回答本讲标题里那个反直觉的问题：**为什么返回 `403` 的链接会被判为通过**。

核心是一份「可接受状态码」清单，由两段配置拼成：

- `accepted_statuses`：逐个枚举，默认 `[401, 403, 429]`。
- `accepted_status_ranges`：闭区间列表，默认 `[[200, 399]]`。

只要状态码**命中其中任一段**，就算「可接受」。

#### 4.5.2 核心流程

```
status_is_accepted(status):
    若 status is None           → False
    若 status ∈ accepted_statuses → True
    若 status 落在任一区间 [lo,hi] → True
    否则                          → False
```

为何要把 `401/403/429` 也算「可接受」？因为这三种状态**通常不代表「链接坏了」**：

| 状态码 | 含义 | 为什么判通过 |
|--------|------|--------------|
| 401 Unauthorized | 需要登录 | 页面存在，只是要登录才能看 |
| 403 Forbidden | 拒绝访问 | 常是反爬/地域限制，页面其实存在（如 GitHub 对脚本返回 403） |
| 429 Too Many Requests | 限流 | 页面存在，只是当前请求太频繁 |

把链接检查器当作「**它还在不在**」的探测，这三种都说明「资源在，只是不给我们看」，所以放行。

#### 4.5.3 源码精读

状态码判定：[scripts/check_links.py:L507-L512](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L507-L512) —— 两段配置的合取判定：

```python
def status_is_accepted(status: int | None, http_config: dict[str, Any]) -> bool:
  if status is None:
    return False
  if status in set(http_config.get('accepted_statuses', [])):
    return True
  return any(int(start) <= status <= int(end)
             for start, end in http_config.get('accepted_status_ranges', []))
```

配置来源：[check-links.toml:L8-L16](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/check-links.toml#L8-L16) —— 本仓库实际用的 `[http]` 参数：

```toml
[http]
timeout = 15
retries = 1
workers = 16
user_agent = "pku-minic-online-doc-link-checker/1.0"
accepted_statuses = [401, 403, 429]
accepted_status_ranges = [[200, 399]]
check_fragments = true
max_fragment_page_bytes = 2000000
```

**现在回答「为什么 403 会被判为通过」**：跟踪一条返回 403、且**带 fragment** 的链接在 `evaluate_remote_response`（[L562-L573](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L562-L573)）里的走向：

1. `status_is_accepted(403, ...)`：403 命中 `accepted_statuses` → 返回 `True`，第 1 步不触发失败。
2. `needs_fragment` 为真（带 fragment），不进第 2 步的早退通过。
3. **关键**：第 3 步 `response.status in accepted_statuses` → 403 命中 → 直接返回 `RemoteResult(True, 'HTTP 403; fragment not verified', 403)`。

也就是说：**即便我们已经为了验证锚点而抓回了页面，只要状态码是 401/403/429，就直接放行、不再比对锚点**。因为这种状态下的响应正文多半是「拒绝访问」的错误页（不是真实内容），上面根本没有有效锚点，强行比对只会误报。检查器的态度是：「能拿到 403 说明资源在，至于锚点，放过」。

对**不带 fragment** 的 403 链接则更简单：走到第 2 步 `if not needs_fragment` 直接返回 `ok`。

> 注意区分：`accepted_statuses` 让 403「状态层面」通过；而「不验证锚点」是 `evaluate_remote_response` 第 3 步的额外优待。两者叠加，才有了「403 一定通过、且 fragment 标注为 not verified」的现象。

#### 4.5.4 代码实践

**目标**：亲手验证「把 403 移出 accepted_statuses 后，原本通过的链接会变失败」。

1. 运行一次基线：`python scripts/check_links.py --config check-links.toml --verbose`，记下 `issues` 数量。
2. 临时把 `check-links.toml` 里 `accepted_statuses = [401, 403, 429]` 改成 `accepted_statuses = [401, 429]`（去掉 403）。
3. 再跑一次，对比 `issues` 数量与失败明细。
4. 把配置还原。

**需要观察的现象**：去掉 403 后，那些原本因 403（常见于 GitHub 类站点对脚本的反爬）而通过的远程链接，现在会出现在失败列表里，类别为 `remote-http`，消息形如 `HTTP 403`。

**预期结果**：第二次运行的 `issues` 数量 ≥ 第一次；新增的失败项状态码为 403。是否一定能复现取决于本仓库当前远程链接里是否有返回 403 的站点，**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：状态码 `451`（Legal Reasons，法律原因下不可用）会被判通过还是失败？

> **答案**：失败。`451` 不在 `accepted_statuses`（401/403/429）里，也不落在 `[200, 399]` 区间。若实际希望放行，可在 toml 的 `accepted_statuses` 追加 `451`。

**练习 2**：`status_is_accepted(None, ...)` 为什么返回 `False`？

> **答案**：`None` 表示「根本没拿到状态码」（比如请求异常、连不上）。连状态都没有，无法判定为通过；这种情形在 `check_remote_link`/`check_remote_by_get` 里会被 `except httpx.HTTPError` 捕获，转成 `RemoteResult(False, 'request failed: ...')`，而非走到 `evaluate_remote_response`。`None` 分支是防御性兜底。

---

## 5. 综合实践

把本讲的并发、HEAD/GET 回退、HTML 判定、锚点验证、状态码规则串起来，做一个端到端跟踪任务。

**任务**：选一条本仓库真实存在的、带 `#fragment` 的远程链接，完整追踪它从「被并发派出」到「得出结论」的全过程，并解释它命中的是 `evaluate_remote_response` 的哪一条分支。

操作步骤：

1. 在仓库里挑一条带锚点的远程链接，例如用只读命令查找：
   ```
   git grep -nE 'https?://[^ )]+#' -- 'docs/*.md' | head
   ```
   选定其中一条 URL 记下。
2. 临时在 [check_remote_link](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L594-L610) 的关键节点加调试打印（本地实验，勿提交）：
   - HEAD 之后：打印 `head.status`、`needs_fragment`、`status_is_accepted(head.status, ...)`。
   - 进入 GET 分支后：打印 `get.status`、`get.content_type`。
   - 在 [evaluate_remote_response](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/scripts/check_links.py#L562-L573) 每条 `return` 前打印一句标记（如 `branch-2 ok`、`branch-3 not-verified`、`branch-5 fragment-missing`）。
3. 运行 `python scripts/check_links.py --config check-links.toml --verbose`，在大量输出里 `grep` 你那条 URL。
4. 根据打印的节点链，画出该链接的决策路径，并回答：
   - 它走了 HEAD 还是 GET？为什么？
   - 它最终命中 `evaluate_remote_response` 的第几条分支？
   - 它的 fragment 有没有被真正验证？为什么？
5. 实验后**还原所有调试打印**，确认 `git diff scripts/check_links.py` 为空。

**预期结果**：你应该能写出一条形如「HEAD 200 → 因 needs_fragment 升级 GET → 拿到 text/html → 第 6 条分支 ok，锚点验证通过」或「HEAD 403 → 升级 GET → 第 3 条分支 not-verified」的路径描述。具体走哪条取决于你选的链接与当时的网络，**以本地实际跟踪为准**。

> 这个练习同时检验了你对 5 个最小模块的理解：并发派出（4.1）、回退策略（4.2）、HTML 判定（4.3）、锚点验证（4.4）、状态码规则（4.5）。

---

## 6. 本讲小结

- 远程校验由 `check_remote_links` 用 `asyncio.gather` 把所有去重后的远程 URL 并发派出，用 `asyncio.Semaphore` 与 `httpx.Limits` 双重限流，`workers` 控制并发度（本仓库为 16）。
- 单条链接走「先 HEAD 后 GET」：HEAD 不读正文最省；当「需要验证锚点」或「HEAD 状态码不被接受」时升级 GET；HEAD 直接异常则整体退回 GET 分支。GET 在不读正文时用 `Range: bytes=0-0` 只取 1 字节。
- 只有响应被判定为 HTML（`is_html_content`：content-type 含 html/xhtml，或 URL 以 `.html/.htm//` 结尾）时，才会抓正文比对锚点；图片/PDF/JSON 等直接跳过锚点验证。
- 锚点验证复用本地侧的 `parse_html_anchors` + `expand_anchor_forms`，做「多形式集合相交」匹配；命中即通过，完全对不上才报 `fragment not found`。文本片段 `#:~:text=` 不验证。
- 状态码规则由 `accepted_statuses`（401/403/429）与 `accepted_status_ranges`（[200,399]）合取；`evaluate_remote_response` 按优先级短路判定。**403 之所以通过**：状态码层面被 `accepted_statuses` 放行，且即便带 fragment 也走第 3 分支 `fragment not verified` 不再比对锚点——因为 403 的正文多半是错误页。

---

## 7. 下一步学习建议

本讲把「单条远程链接」的校验讲透了。下一篇 [u4-l5 忽略规则、主流程编排与 CI 集成](u4-l5-ignore-rules-and-ci.md) 会收口整个第四单元：

- 讲清 `IgnoreRule` 的多字段**合取匹配**（`url`/`url_regex`/`path`/`path_glob`/`line`/`category`/`status`/`statuses`），以及它如何同时作用于「链接层」和「问题层」两次过滤。
- 把 `check_links` 主流程的编排（扫描→提取→过滤→本地/远程校验→统计）和退出码（0/1/2）串成一张完整大图。
- 看懂 `.github/workflows/check-links.yml` 如何在 PR/push 时自动跑这套检查器，以及 CI 在什么条件下因链接问题失败（这正是退出码 1 的归宿）。

建议阅读时带着一个问题：本讲产生的 `remote-http` Issue，最终是怎样被「忽略规则」和「CI 判定」两头接住的？这将把第 4 单元的四个最小子系统（提取、本地、远程、忽略+CI）闭合成环。
