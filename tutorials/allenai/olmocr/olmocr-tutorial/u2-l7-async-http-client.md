# 手写异步 HTTP 客户端 apost

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 olmOCR 为什么在每页推理的热路径上**不用** httpx/aiohttp，而是用约 95 行手写的 `apost` 直连 socket，以及这个「反直觉」决定背后的规模原因。
2. 读懂 `apost` 的完整实现：用 `asyncio.open_connection` 建立连接、手工拼 HTTP/1.1 请求报文、解析状态行与响应头、按 `Content-Length` / `Transfer-Encoding: chunked` / `Connection: close` 三种方式读取响应体。
3. 说明两个全局 `BoundedSemaphore`（渲染闸门与请求闸门）分别保护什么资源、`main()` 为什么要在运行时重建请求闸门。
4. 描述 `try_single_page_with_backoff` 的指数退避曲线 \( t_n = 10 \times 2^n \) 秒、哪些异常会触发退避、退避 10 次耗尽后为什么是 `sys.exit(1)` 而不是 pdftotext 兜底。
5. 在本地写一个自包含的 asyncio「echo 服务器 + apost 客户端」脚本，亲手验证以上全部行为。

## 2. 前置知识

### 2.1 HTTP/1.1 报文长什么样

HTTP 是**文本协议**：请求和响应都是「起始行 + 若干头字段 + 空行 + 可选体」的字节流。一次 POST 请求在线路上是这样的（`\r\n` 是回车换行）：

```text
POST /chat/completions HTTP/1.1\r\n      ← 请求行：方法 路径 版本
Host: localhost:8341\r\n                 ← 头字段：名字: 值
Content-Type: application/json\r\n
Content-Length: 1234\r\n                 ← 告诉服务器体有多长
Connection: close\r\n                    ← 请求完成后关闭连接
\r\n                                     ← 空行 = 头结束
{...JSON 请求体...}                       ← 长度恰为 1234 字节
```

响应则是：

```text
HTTP/1.1 200 OK\r\n                      ← 状态行：版本 状态码 原因短语
Content-Type: application/json\r\n
Content-Length: 567\r\n
\r\n
{...JSON 响应体...}
```

关键问题：**读响应体时怎么知道它有多长？** HTTP/1.1 给出三种约定，本讲的 `apost` 三种都要处理：

1. **`Content-Length`**：体恰好 N 字节，读满 N 即可。
2. **`Transfer-Encoding: chunked`**（分块传输）：长度未知，服务器把体切成若干块，每块前写十六进制的块大小行，最后以 `0` 块收尾：

   ```text
   5\r\nhello\r\n     ← 「接下来 5 字节」，然后是 5 字节数据，再一个 CRLF
   6\r\n world\r\n
   0\r\n\r\n          ← 大小为 0 的结束块
   ```

3. **`Connection: close`**：既没长度也没分块时，读到对端关闭连接为止（体 = 关闭前收到的全部字节）。

### 2.2 asyncio 的网络原语

- **`asyncio.open_connection(host, port)`** 返回一对 `(reader, writer)`：`reader` 是 `StreamReader`，`writer` 是 `StreamWriter`。它底层就是一条 TCP socket，`use_ssl=True` 时在socket上加一层 TLS。默认每行的读取上限是 64KiB，对正常 HTTP 头绰绰有余。
- **`reader.readline()`** 读到下一个 `\n` 为止（含换行符）；**`reader.readexactly(n)`** 恰好读 n 字节，不足则抛 `IncompleteReadError`；**`reader.read()`** 读到 EOF。
- **`writer.write(bytes)`** 只是把字节放进发送缓冲，**`await writer.drain()`** 才真正等待对端接收、给反压。
- **`BoundedSemaphore`**：异步信号量，`async with sem:` 表示「拿到名额→做事→归还名额」，用于给并发数量封顶；它和普通 `Semaphore` 的区别是归还次数超过获取次数时会立刻抛 `ValueError`，能在开发期暴露配平 bug。

### 2.3 指数退避

当依赖的服务暂时不可用（重启中、网络抖动），立刻重试只会火上浇油。**指数退避**（exponential backoff）的做法是每失败一次就把等待时间翻倍：

\[ t_n = 10 \times 2^n \ \text{秒}, \quad n = 0, 1, 2, \ldots \]

好处：前期快速重试抓住「眨眼就好」的故障，后期长等待容忍「要修一会儿」的故障，同时天然压低重试流量本身。

### 2.4 本讲在整条链路中的位置

回顾 u2-l6：每页的处理链是「渲染 → 组请求 → HTTP 调用 → 解析」。`apost` 就是其中「HTTP 调用」这一步的全部实现；请求闸门在它外面包了一圈；连接级错误的退避包装再往外一圈。三者的层次关系：

```text
process_page                ← 温度阶梯（u2-l6，模型输出层失败）
 └─ try_single_page_with_backoff   ← 指数退避（本讲 4.3，连接层失败）
     └─ try_single_page            ← 异常分类：连接错误向上抛（本讲）
         └─ async with 请求信号量:   ← 并发闸门（本讲 4.2）
             └─ apost              ← 手写 HTTP（本讲 4.1）
```

## 3. 本讲源码地图

本讲全部核心代码集中在 `olmocr/pipeline.py`：

| 位置 | 作用 |
|---|---|
| [pipeline.py:L84-L88](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L84-L88) | 温度表与两个全局信号量（渲染闸门、请求闸门）的定义 |
| [pipeline.py:L106-L146](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L106-L146) | `build_page_query`：渲染受 CPU 闸门保护，组装 OpenAI 格式请求体 |
| [pipeline.py:L149-L231](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L149-L231) | `try_single_page`：`apost` 的唯一调用点 + 三类异常的分流 |
| [pipeline.py:L253-L278](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L253-L278) | `try_single_page_with_backoff`：指数退避包装 |
| [pipeline.py:L378-L476](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L378-L476) | `apost` 本体：本讲主角 |
| [pipeline.py:L929-L940](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L929-L940) | `vllm_server_ready`：对照点——低频探活仍然用 httpx |
| [pipeline.py:L1221-L1247](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1221-L1247) | argparse：`--max_concurrent_requests` 默认 1600、`--api_key` |
| [pipeline.py:L1287-L1289](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1287-L1289) | `main()` 中用真实参数值重建请求闸门 |

辅助阅读（非必读）：`olmocr/bench/runners/run_server.py` 等 bench 脚本仍用 httpx 发请求，是理解「热路径 vs 低频路径」取舍的好对照。

## 4. 核心概念与源码讲解

### 4.1 apost：手写 HTTP 客户端

#### 4.1.1 概念说明

`apost(url, json_data, api_key)` 的功能等价于「发一个 JSON POST、拿回 `(status_code, body)`」。这件事 httpx 两行就能做，作者却手写了 95 行裸 socket 实现，并在代码注释里直接给出了理由：

```python
# Manual simple implementation of HTTP Post
# It feels strange perhaps, but httpx and aiohttp are very complex beasts
# Ex. the sessionpool in httpcore has 4 different locks in it, and I've noticed
# that at the scale of 100M+ requests, that they deadlock in different strange ways
```

翻译过来：httpx/aiohttp 为了通用性（连接池、keep-alive、HTTP/2、重定向、cookie、代理……）内置了大量状态机与锁。httpcore 的 sessionpool 里有 4 把不同的锁；在**亿级请求**的规模下，这些锁偶发的死锁比任何性能问题都致命——死锁意味着 worker 永久挂起，而挂起的任务不会报任何错。

olmOCR 的热路径需求其实窄得多：

- 目标端点只有一个（本地 vLLM 或某个 OpenAI 兼容服务），不需要重定向/cookie/代理；
- 每个请求独立完整，**可以接受一请求一连接**（`Connection: close`），彻底不搞连接池——没有池就没有池的锁和死锁；
- 失败以普通异常形式抛出，交给上层（4.3 的退避包装）统一处理。

这是典型的**用功能面换确定性**：砍掉 99% 用不到的 HTTP 特性，换来一段一个人一下午能完整读懂、行为完全可预测的代码。注意项目并没有全面禁用 httpx——启动时低频的服务器探活 `vllm_server_ready` 仍在用它（[pipeline.py:L929-L940](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L929-L940)）。替换只发生在每页一次、总量上亿的热路径上。

#### 4.1.2 核心流程

`apost` 的执行分五步：

```text
1. 解析 URL        → host / port(默认 443 或 80) / path / 是否 ssl
2. 建立连接        → asyncio.open_connection(host, port [, ssl])
3. 发送请求        → 手工拼「请求行 + 头 + 空行 + JSON 体」，write + drain
4. 读响应          → 状态行 → 逐行读头到空行 → 按三种方式之一读体
5. 收尾            → finally 里关闭 socket（无论成败）
```

读响应体的三分支决策：

```text
有 content-length 头?          → readexactly(N)
有 transfer-encoding: chunked? → 循环「十六进制大小行 → readexactly → 尾随 CRLF」
                                    直到 0 块出现
有 connection: close 头?       → read() 读到对端关连接
三者皆无?                      → raise ConnectionError（无法定界，宁可失败不可猜）
```

注意请求头里主动写了 `Connection: close`（[pipeline.py:L414](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L414)），所以正常情况下服务器处理完会主动断开，第三分支是兜底。

#### 4.1.3 源码精读

**① URL 解析与连接建立**（[pipeline.py:L382-L400](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L382-L400)）：

```python
async def apost(url, json_data, api_key=None):
    parsed_url = urlparse(url)
    host = parsed_url.hostname
    if parsed_url.scheme == "https":
        port = parsed_url.port or 443
        use_ssl = True
    else:
        port = parsed_url.port or 80
        use_ssl = False
    path = parsed_url.path or "/"

    writer = None
    try:
        if use_ssl:
            ssl_context = ssl.create_default_context()
            reader, writer = await asyncio.open_connection(host, port, ssl=ssl_context)
        else:
            reader, writer = await asyncio.open_connection(host, port)
```

这段用 `urlparse` 拆出主机、端口（https 默认 443、http 默认 80）、路径，然后按协议决定是否套 TLS。`writer = None` + `try` 的组合是为了让最后的 `finally` 能安全关socket——即使连接还没建立就抛异常，`writer` 仍是 `None`，不会误关。对本地 vLLM（`http://localhost:port`）走的是无 TLS 的裸 TCP 分支。

**② 拼请求并发送**（[pipeline.py:L402-L418](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L402-L418)）：

```python
    json_payload = json.dumps(json_data)

    headers = [
        f"POST {path} HTTP/1.1",
        f"Host: {host}",
        f"Content-Type: application/json",
        f"Content-Length: {len(json_payload)}",
    ]

    if api_key:
        headers.append(f"Authorization: Bearer {api_key}")

    headers.append("Connection: close")

    request = "\r\n".join(headers) + "\r\n\r\n" + json_payload
    writer.write(request.encode())
    await writer.drain()
```

就是把 2.1 节的报文格式逐行写出来：列表拼头、`\r\n` 连接、空行后接 JSON 体。两个细节值得咀嚼：

- `Content-Length` 用的是 `len(json_payload)`——**字符数**而非字节数。这里安全是因为 `json.dumps` 默认 `ensure_ascii=True`，非 ASCII 字符全部转义成 `\uXXXX`，输出是纯 ASCII，字符数恒等于 UTF-8 编码后的字节数。若改成 `ensure_ascii=False` 且请求体含中文，这个头就会低估长度，服务器会按短一截的长度去读体（见练习 1）。
- `Authorization: Bearer` 只在传入 `api_key` 时追加——调用点 `try_single_page` 里只有使用外部计费服务时才传（[pipeline.py:L167](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L167)）。

**③ 状态行与响应头解析**（[pipeline.py:L420-L435](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L420-L435)）：

```python
    status_line = await reader.readline()
    if not status_line:
        raise ConnectionError("No response from server")
    status_parts = status_line.decode().strip().split(" ", 2)
    if len(status_parts) < 2:
        raise ValueError(f"Malformed status line: {status_line.decode().strip()}")
    status_code = int(status_parts[1])

    headers = {}
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        key, _, value = line.decode().partition(":")
        headers[key.strip().lower()] = value.strip()
```

`"HTTP/1.1 200 OK"` 按 `split(" ", 2)` 拆成三段，取第 2 段转 int 得到状态码。空响应（对端直接断开）抛 `ConnectionError`；格式不对的状态行抛 `ValueError`——注意这两个异常的**类别**不同，后面 4.3 会看到它们在重试体系里走向完全不同的分支。头循环逐行读到空行为止，键统一转小写，所以后面查找 `content-length` 时不用关心服务器返回的是 `Content-Length` 还是 `content-length`。

**④ 响应体三分支**（[pipeline.py:L437-L463](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L437-L463)）：

```python
    if "content-length" in headers:
        body_length = int(headers["content-length"])
        response_body = await reader.readexactly(body_length)
    elif headers.get("transfer-encoding", "") == "chunked":
        chunks = []
        while True:
            size_line = await reader.readline()
            chunk_size = int(size_line.strip(), 16)   # 十六进制

            if chunk_size == 0:
                await reader.readline()  # 结束块后的 CRLF
                break

            chunk_data = await reader.readexactly(chunk_size)
            chunks.append(chunk_data)
            await reader.readline()      # 每块数据后的 CRLF

        response_body = b"".join(chunks)
    elif headers.get("connection", "") == "close":
        response_body = await reader.read()
    else:
        raise ConnectionError("Cannot determine response body length")
```

- `Content-Length` 分支最常见（vLLM 通常返回定长 JSON）；
- chunked 分支严格按协议逐块读：大小行是**十六进制**（`int(s, 16)`），`0` 块表示结束，每块数据后还有一个 CRLF 要消费掉，否则残留在缓冲区里会污染下一次读取——虽然这里一请求一连接，但正确消费分隔符仍是必须的；
- 第三分支 `read()` 读到 EOF；三者皆无时抛 `ConnectionError`——**无法定界就读下去是危险的**，显式失败好过静默读错。

**⑤ 收尾：异常透传 + 必关socket**（[pipeline.py:L465-L476](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L465-L476)）：

```python
    return status_code, response_body
    except Exception as e:
        # Pass through errors
        raise e
    finally:
        # But just make sure to close the socket on your way out
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except:
                pass
```

`except: raise e` 看似多余，实则是作者刻意的文档化写法：**`apost` 不吞任何异常**，全部原样上抛，由调用方决定策略；`finally` 则保证无论成功、失败还是中途取消，socket 一定被关闭——每个连接泄漏 1 个文件描述符，百万页规模下就是灾难。关闭本身的失败被裸 `except: pass` 吞掉，因为此时连接多半已经断了。

#### 4.1.4 代码实践

不用启动任何外部服务——我们用 `asyncio.start_server` 在**同一个事件循环**里起一个极简 HTTP echo 服务器，再用 `apost` 打它。整个脚本自包含、无需 GPU。

1. **实践目标**：亲手验证 `apost` 的「发送定长请求 → 解析状态行/头 → 按 Content-Length 读体」全流程，再切换到 chunked 响应验证第二分支。

2. **操作步骤**：把下面的脚本保存为 `practice_apost.py`（示例代码）：

   ```python
   import asyncio
   import json

   from olmocr.pipeline import apost  # 轻量安装即可，无 torch/vllm 依赖


   async def handle(reader, writer):
       """极简 HTTP/1.1 服务器：读头 + 按 Content-Length 读体，回一个定长 JSON。"""
       headers = {}
       while True:
           line = await reader.readline()
           if line in (b"\r\n", b"\n", b""):
               break
           key, _, value = line.decode().partition(":")
           headers[key.strip().lower()] = value.strip()

       body_length = int(headers.get("content-length", 0))
       payload = json.loads(await reader.readexactly(body_length))

       data = json.dumps({"echo": payload, "ok": True}).encode()
       writer.write(
           b"HTTP/1.1 200 OK\r\n"
           b"Content-Type: application/json\r\n"
           + f"Content-Length: {len(data)}\r\n".encode()
           + b"Connection: close\r\n\r\n"
           + data
       )
       await writer.drain()
       writer.close()
       await writer.wait_closed()


   async def main():
       server = await asyncio.start_server(handle, "127.0.0.1", 8765)

       status, body = await apost(
           "http://127.0.0.1:8765/chat/completions",
           json_data={"model": "olmocr", "messages": [{"role": "user", "content": "hello"}]},
       )
       print("status:", status)
       print("body:  ", body.decode())

       server.close()
       await server.wait_closed()


   asyncio.run(main())
   ```

   运行 `python practice_apost.py`。

3. **需要观察的现象**：输出 `status: 200`；`body` 是服务器回的 JSON，`echo` 字段原样带着你发的请求体。

4. **预期结果**：服务器端按 `Content-Length` 精确读到了完整请求体（说明 apost 发送的定长头是对的）；客户端拿到的 `body` 是 `bytes`，与 `try_single_page` 里直接 `json.loads(response_body)` 的用法一致。

5. **进阶变体（chunked 分支）**：把 `handle` 中响应部分换成下面的写法（示例代码），重新运行，客户端输出应完全不变——这就是 4.1.3 第④段 chunked 分支在工作：

   ```python
   data = json.dumps({"echo": payload, "ok": True}).encode()
   writer.write(
       b"HTTP/1.1 200 OK\r\n"
       b"Content-Type: application/json\r\n"
       b"Transfer-Encoding: chunked\r\n"
       b"Connection: close\r\n\r\n"
   )
   writer.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")  # 一块：大小(十六进制)+数据+CRLF
   writer.write(b"0\r\n\r\n")                                     # 结束块
   await writer.drain()
   ```

   若运行环境不可用，以上结果标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果第②段改成 `json.dumps(json_data, ensure_ascii=False)` 且请求体含中文，会发生什么？
**答案**：`len(json_payload)` 统计的是字符数，而 `.encode()` 后中文占 3 字节，`Content-Length` 会小于真实字节数。服务器按这个偏小的长度读体会把 JSON 截断在中间，`json.loads` 失败或字段缺失。默认 `ensure_ascii=True` 恰好保证了「字符数 == UTF-8 字节数」，这是这段代码正确性的隐含前提。

**练习 2**：服务器返回 200，但响应头既没有 `Content-Length`、没有 `Transfer-Encoding`，也不断开连接（keep-alive），`apost` 会怎样？这个异常最终流向哪里？
**答案**：走第④段最后的 `else`，抛 `ConnectionError("Cannot determine response body length")`。`ConnectionError` 是 `OSError` 的子类，会被 `try_single_page` 重抛（见 4.3），进入指数退避——同一连接上的下一次请求大概率还是同样结果。

**练习 3**：请求头里的 `Host: {host}` 没有带端口号。什么时候这在协议上是错的？
**答案**：按 RFC 7230，目标端口不是协议默认端口（80/443）时 `Host` 应写成 `host:port`。本实现省略了端口——对本地 vLLM 这类宽容的实现几乎总能工作，但若某个前端代理严格校验 `Host` 就会被拒。这是「按实际需求最小实现」的又一处取舍。（具体某服务是否容忍，待本地验证。）

### 4.2 BoundedSemaphore 并发限制

#### 4.2.1 概念说明

olmOCR 一次批处理可能在 20 个 worker 上同时推进成百上千个页面任务，每个页面任务都要（a）跑一个 pdftoppm 子进程渲染图片、（b）发一个推理 HTTP 请求。这两件事不加限制就会出事：

- 同时起几千个 pdftoppm 进程会把 CPU 打爆（fork 风暴）；
- 同时挂几千个在途请求会把推理服务的排队与显存压垮（对计费 API 则是烧钱）。

所以 pipeline.py 在模块级定义了两个闸门（[pipeline.py:L84-L88](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L84-L88)）：

```python
TEMPERATURE_BY_ATTEMPT = [0.1, 0.1, 0.2, 0.3, 0.5, 0.8, 0.9, 1.0]

pdf_render_max_workers_limit = asyncio.BoundedSemaphore(int(float(os.environ.get("BEAKER_ASSIGNED_CPU_COUNT", max(1, multiprocessing.cpu_count() - 2)))))
max_concurrent_requests_limit = asyncio.BoundedSemaphore(1)  # Actual value set by args in main()
```

- **`pdf_render_max_workers_limit`（CPU 闸门）**：名额数优先读环境变量 `BEAKER_ASSIGNED_CPU_COUNT`（Beaker 集群分配的核数），否则取 `cpu_count() - 2`（至少 1）。
- **`max_concurrent_requests_limit`（请求闸门）**：import 时先给占位值 1，真正容量由 `main()` 按命令行参数重建。

为什么用 `BoundedSemaphore` 而不是普通 `Semaphore`？BoundedSemaphore 在「归还次数 > 获取次数」时立刻抛 `ValueError`。配合 `async with`（异常路径也保证归还）本不会失衡，但一旦哪天有人手写 `release()` 配错， bounded 版本会让 bug 当场爆炸而不是静默地让闸门越开越大——**快速失败**。

#### 4.2.2 核心流程

两个闸门的使用方式完全一致，都是 `async with 闸门: 包住临界区`：

```text
渲染闸门（build_page_query 内）:
  async with pdf_render_max_workers_limit:
      image_base64 = await asyncio.to_thread(render_pdf_to_base64png, ...)
      # to_thread 只是「不阻塞事件循环」，不限并发；真正限并发的是闸门

请求闸门（try_single_page 内）:
  async with max_concurrent_requests_limit:
      status_code, response_body = await apost(COMPLETION_URL, json_data=query, api_key=api_key)
```

请求闸门的容量设置链路：

```text
argparse: --max_concurrent_requests 默认 1600 (L1224)
    ↓
main() 里 global 声明后重建:
    max_concurrent_requests_limit = asyncio.BoundedSemaphore(args.max_concurrent_requests)
```

注意闸门**只包 `apost` 调用本身**（[pipeline.py:L184-L185](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L184-L185)）：渲染、请求体组装、响应解析都在闸门外。闸门度量的正是「在途 HTTP 请求数」这个唯一需要限的量。

#### 4.2.3 源码精读

**渲染闸门的使用点**在 `build_page_query` 开头（[pipeline.py:L106-L112](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L106-L112)）：

```python
async def build_page_query(local_pdf_path: str, page: int, target_longest_image_dim: int, image_rotation: int = 0, model_name: str = "olmocr") -> dict:
    MAX_TOKENS = 8000
    assert image_rotation in [0, 90, 180, 270], ...

    # Allow the page rendering to process in the background, but limit the number of workers otherwise you can overload the system
    async with pdf_render_max_workers_limit:
        image_base64 = await asyncio.to_thread(render_pdf_to_base64png, local_pdf_path, page, target_longest_image_dim=target_longest_image_dim)
```

注释说得很清楚：`asyncio.to_thread` 把阻塞的渲染丢进线程池是为了**不阻塞事件循环**，但线程池默认线程数（约 32）远大于合理并发，真正限制同时运行的 pdftoppm 子进程数量的是外层的信号量。这是「to_thread + 信号量」的黄金组合，u2-l3 已从渲染视角讲过一侧，本讲补齐限流一侧。

**请求闸门的使用点**在 `try_single_page`（[pipeline.py:L184-L191](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L184-L191)）：

```python
        async with max_concurrent_requests_limit:
            status_code, response_body = await apost(COMPLETION_URL, json_data=query, api_key=api_key)

        if status_code != 200:
            logger.warning(
                f"Server returned {status_code} for {pdf_orig_path}-{page_num} attempt {attempt}: ..."
            )
            return None
```

所有 worker 的所有页面共享这一个全局闸门——它是整个进程范围内「同时在途请求数」的单点控制。HTTP 层失败（非 200）不抛异常，而是返回 `None` 交给 u2-l6 的温度阶梯，这与连接层错误的处理方式（下节）形成对照。

**容量的运行时注入**发生在 `main()`（[pipeline.py:L1224](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1224) 与 [pipeline.py:L1287-L1289](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L1287-L1289)）：

```python
parser.add_argument("--max_concurrent_requests", type=int, default=1600, ...)
...
    global workspace_s3, pdf_s3, max_concurrent_requests_limit
    ...
    max_concurrent_requests_limit = asyncio.BoundedSemaphore(args.max_concurrent_requests)
```

为什么必须「重建」而不是「修改」？`BoundedSemaphore` 没有调整初始容量的接口，模块 import 时又拿不到命令行参数，所以只能先放一个占位值 1，等 `main()` 解析完参数后整体替换。若有代码在 `main()` 之前抢跑，会被串行化到并发 1——实际上没有这样的调用路径。

默认 1600 是为本地多卡 vLLM 的吞吐调的；接到外部按 token 计费的 API 时应大幅调低（u1-l5 讲过 `--api_key` 场景下相关默认值的变化）。

#### 4.2.4 代码实践

1. **实践目标**：直观感受信号量对并发的整形作用，以及 `BoundedSemaphore` 对多余 `release` 的快速失败。
2. **操作步骤**：保存为 `practice_semaphore.py`（示例代码，纯标准库）：

   ```python
   import asyncio
   import time


   async def job(i, sem):
       async with sem:
           print(f"job {i:02d} 进入闸门 @ {time.monotonic() - t0:.2f}s")
           await asyncio.sleep(0.5)   # 模拟一次在途请求


   async def main():
       global t0
       t0 = time.monotonic()
       sem = asyncio.BoundedSemaphore(3)
       await asyncio.gather(*(job(i, sem) for i in range(10)))
       print(f"总耗时 {time.monotonic() - t0:.2f}s（理论值 ceil(10/3)*0.5 = 2.0s）")

       # 多归还一次：BoundedSemaphore 立刻报错
       s2 = asyncio.BoundedSemaphore(1)
       s2.acquire()
       s2.release()
       try:
           s2.release()
       except ValueError as e:
           print("BoundedSemaphore 捕获到不平衡:", e)


   asyncio.run(main())
   ```

3. **需要观察的现象**：任意时刻最多 3 个 job 同时「进入闸门」，其余按序排队；总耗时约 2.0 秒；最后一行打印出 ValueError。
4. **预期结果**：并发被精确压到 3（10 个任务分 4 批：3+3+3+1）；多余 release 被 BoundedSemaphore 当场拒绝。若运行环境不可用，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：两个信号量的初始容量分别由什么决定？为什么一个在 import 时就能定，另一个要到 `main()` 里才能定？
**答案**：渲染闸门由 `BEAKER_ASSIGNED_CPU_COUNT` 环境变量或 `cpu_count()-2` 决定——机器资源在进程启动时已知；请求闸门由 `--max_concurrent_requests` 决定——命令行参数只有 `main()` 解析后才知道，所以 import 时先占位 1、运行时重建。

**练习 2**：`asyncio.to_thread` 本身能限制并发吗？`build_page_query` 里真正限流的是谁？
**答案**：不能。`to_thread` 只负责把阻塞调用移出事件循环（丢进默认线程池），默认线程池约有几十个线程。真正限制同时渲染的是外层 `async with pdf_render_max_workers_limit`。

**练习 3**：把闸门包的范围从 `apost` 调用扩大到整个 `try_single_page`（含渲染和解析），会有什么副作用？
**答案**：闸门度量的就不再是「在途请求数」而是「处理中的页面数」：CPU 侧的渲染和解析也会占住宝贵名额，推理服务的实际在途请求远低于 1600，吞吐下降；同时渲染闸门与请求闸门嵌套，任意一方排队都会放大另一方的排队。

### 4.3 指数退避包装

#### 4.3.1 概念说明

u2-l6 讲的是**模型输出层**的失败（截断、旋转、退化重复），由温度阶梯重试。本节讲另一类完全不同的失败：**连接层**错误——拒绝连接、连接被重置、DNS 故障、超时。这类失败的特点是「不是这一页的问题，是服务/网络的问题」：

- 对它升温毫无意义（问题不在采样随机性），所以退避包装重试时 `attempt` 参数原样传递，温度不变；
- 它几乎总是**暂时的**（vLLM 正在重启、网络抖了一下），值得等待而不是放弃；
- 但如果持续失败，说明整个作业的基础设施坏了——此时把几百万页全部降级成 pdftotext 兜底是错误的（等于悄悄输出低质量结果），正确做法是**终止进程**，让人或调度系统介入。

这三点分别对应代码里的「同 attempt 重试」「指数等待」和「耗尽后 `sys.exit(1)`」。

#### 4.3.2 核心流程

退避体系分两层。第一层在 `try_single_page` 的收尾做**异常分流**：把连接类异常从普通异常里挑出来向上抛：

```text
try_single_page 内部:
  asyncio.CancelledError            → 原样重抛（协程取消必须穿透，不能当失败）
  ConnectionError / OSError / asyncio.TimeoutError
                                    → 原样重抛（注释：让调用方做指数退避）
  其他所有 Exception                → 记 warning，返回 None（走温度阶梯）
```

第二层 `try_single_page_with_backoff` 包在外面，循环「尝试 → 失败则睡 \( t_n = 10 \times 2^n \) 秒 → 再尝试」：

```text
for backoff_count in 0..9:                      # MAX_BACKOFF_ATTEMPTS = 10
    result = try_single_page(...)               # attempt 不变，温度不变
    成功 → 返回
    连接类异常 → 睡 10 * 2**backoff_count 秒，继续循环
循环走完仍失败 → logger.error + sys.exit(1)     # 终止整个进程
```

退避序列为 10、20、40、80、160、320、640、1280、2560、5120 秒。若全部失败，总睡眠时间为：

\[ \sum_{n=0}^{9} 10 \cdot 2^n = 10 \cdot (2^{10} - 1) = 10\,230 \ \text{秒} \approx 170.5 \ \text{分钟} \approx 2.84 \ \text{小时} \]

即：连接持续故障近 3 小时后，进程才判定「基础设施已死」并退出。

#### 4.3.3 源码精读

**① 异常分流**（[pipeline.py:L223-L230](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L223-L230)）：

```python
    except asyncio.CancelledError:
        raise
    except (ConnectionError, OSError, asyncio.TimeoutError):
        # Re-raise connection errors so caller can apply exponential backoff
        raise
    except Exception as e:
        logger.warning(f"try_single_page failed for {pdf_orig_path}-{page_num} attempt {attempt}: {type(e).__name__}: {e}")
        return None
```

三段式：取消穿透 → 连接类重抛 → 其余降级为 `None`。`apost` 里抛出的异常就此有了明确归宿：`ConnectionError("No response from server")` 和「无法定界」的 `ConnectionError`、`open_connection` 的 `ConnectionRefusedError`（`OSError` 子类）都进入退避；而 `ValueError("Malformed status line")` 这类协议解析错误落入第三段、返回 `None`，走温度阶梯。一个细节：在本项目要求的 Python 3.11+ 里，`asyncio.TimeoutError` 就是内建 `TimeoutError` 的别名，而 `ConnectionError`、`TimeoutError` 本身都是 `OSError` 的子类——这个三元组在类型上其实可以合并成 `OSError` 一项，分开写是刻意的**自文档化**：明确列出「我们认为属于连接问题」的三种形态。

**② 退避循环**（[pipeline.py:L253-L278](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L253-L278)）：

```python
async def try_single_page_with_backoff(args, pdf_orig_path, pdf_local_path, page_num, attempt, rotation) -> PageResult | None:
    """
    Wrapper around try_single_page that handles connection errors with exponential backoff.
    """
    MAX_BACKOFF_ATTEMPTS = 10

    for backoff_count in range(MAX_BACKOFF_ATTEMPTS):
        try:
            return await try_single_page(args, pdf_orig_path, pdf_local_path, page_num, attempt, rotation)
        except (ConnectionError, OSError, asyncio.TimeoutError) as e:
            sleep_delay = 10 * (2**backoff_count)
            logger.warning(
                f"Connection error on {pdf_orig_path}-{page_num} attempt {attempt}: {type(e).__name__}: {e}. "
                f"Backoff {backoff_count + 1}/{MAX_BACKOFF_ATTEMPTS}, sleeping {sleep_delay}s"
            )
            await asyncio.sleep(sleep_delay)

    logger.error(f"Max backoff attempts reached for {pdf_orig_path}-{page_num}, terminating job")
    sys.exit(1)
```

逐行看关键决策：

- 调用 `try_single_page` 时 `attempt` 参数**原样透传**——退避期间温度不升，因为连接错误与采样随机性无关；
- `10 * (2**backoff_count)` 就是 4.3.2 节的曲线，日志把进度（第几次/共几次）和睡眠时长都打出来，方便运维判断「是在退避还是在挂起」；
- 耗尽后 `sys.exit(1)`：`SystemExit` 沿协程栈向上传播（TaskGroup 会把它当作致命错误），整个 pipeline 终止——因为此时**所有**页面都无法到达推理服务，继续跑只会产生海量 pdftotext 降级输出。对比 u2-l6：模型层失败用 `make_fallback_result` 兜底，基础设施层失败宁可死。断点续跑由 workspace 的 done flag 保证（u1-l3），修复服务后重跑同一命令即可接着干；
- 一个阅读源码才能发现的小瑕疵：第 10 次尝试失败后仍会先睡满 5120 秒**然后才**退出循环——最后一次睡眠没有任何后续尝试，纯属浪费（见练习 3）。

**③ 调用关系全景**：`process_page` 的每一次「问一次」都经由退避包装（[pipeline.py:L296](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L296)、[pipeline.py:L312](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L312)、[pipeline.py:L336](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L336)、[pipeline.py:L348](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L348)）：

```python
    result = await try_single_page_with_backoff(args, pdf_orig_path, pdf_local_path, page_num, attempt=0, rotation=cumulative_rotation)
```

于是两套重试正交地叠在一起：**外层温度阶梯**（`--max_page_retries` 默认 8 次，处理输出质量）与**内层指数退避**（10 次，处理连通性），最坏情况一页要经历 \( 8 \times 10 = 80 \) 次连接级尝试（不含退避睡眠的叠加）。

#### 4.3.4 代码实践

1. **实践目标**：用数据回答本讲实践任务的第二问——什么异常触发退避、最多退避多少次、睡多久。

2. **操作步骤**：

   a. 打印退避曲线（示例代码）：

   ```python
   delays = [10 * 2**n for n in range(10)]
   for i, d in enumerate(delays, 1):
       print(f"第 {i:2d} 次退避: 睡 {d:5d}s")
   print("总睡眠:", sum(delays), "秒 ≈", round(sum(delays) / 3600, 2), "小时")
   ```

   b. 修改 4.1.4 的 echo 服务器脚本：不启动服务器（或让 `handle` 直接 `writer.close()` 返回空响应），然后调用 `apost`——观察抛出的异常类型；再在 4.3.3 第①段三段式异常表的语境下归类它。

3. **需要观察的现象**：a 部分输出 10、20、40 … 5120 的序列与总数 10230 秒；b 部分 `apost` 抛 `ConnectionError("No response from server")`（空响应）或 `ConnectionRefusedError`（无服务器，`OSError` 子类）。

4. **预期结果**：两类异常都命中 `try_single_page` 的第二段 except、被重抛给 `try_single_page_with_backoff`，触发退避；若 10 次全失败，进程 `sys.exit(1)` 退出。对照源码：触发异常元组定义在 [pipeline.py:L269](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L269)，上限与退出在 [pipeline.py:L264](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L264) 与 [pipeline.py:L277-L278](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L277-L278)。「待本地验证」部分：真实 vLLM 重启场景下的日志输出。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `try_single_page` 要把 `asyncio.CancelledError` 单独放在最前面重抛？
**答案**：在 Python 3.8+ 中 `CancelledError` 继承自 `BaseException` 而非 `Exception`，本不会被第三段 `except Exception` 捕获；但若被中间某层捕获，协程就失去了对「取消」的响应能力（u2-l6 讲过队列空并行重试后会 `t.cancel()` 其余任务）。单独写出来重抛既是防御，也是向读者声明：取消语义必须穿透本函数。

**练习 2**：退避耗尽后为什么 `sys.exit(1)`，而模型层失败却 `make_fallback_result` 用 pdftotext 兜底？
**答案**：模型层失败是**这一页**的问题——其他页可能好好的，兜底保证流水线整体推进；连接层持续失败是**整个作业的基础设施**问题——推理服务不可达近 3 小时意味着所有页都会失败，若仍走兜底，会静默地把整批文档降级成低质量输出。宁可显式失败（退出码 1，可被 Beaker/脚本感知并重启），靠 workspace 断点续跑恢复，也不静默降级。

**练习 3**：退避循环里第 10 次失败后发生了什么？有什么可改进之处？
**答案**：第 10 次（`backoff_count=9`）失败后仍执行 `await asyncio.sleep(5120)`，然后循环自然结束才走到 `sys.exit(1)`——最后一次睡眠后没有任何尝试，白等约 85 分钟。改进：判断 `backoff_count == MAX_BACKOFF_ATTEMPTS - 1` 时跳过睡眠直接退出，或把 `sleep` 移到循环体开头并做 `if backoff_count > 0` 守卫。

## 5. 综合实践

**任务：给 `apost` 做一次带并发闸门的微型压测。** 把 4.1 与 4.2 串起来：同一个事件循环里起一个「慢 100ms」的 echo 服务器，用 `BoundedSemaphore(10)` 作为请求闸门，并发发 50 个 `apost` 请求，验证闸门确实把并发压在了 10 以内、总耗时符合分批预测。

保存为 `mini_loadtest.py`（示例代码）：

```python
import asyncio
import json
import time

from olmocr.pipeline import apost


async def handle(reader, writer):
    headers = {}
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        key, _, value = line.decode().partition(":")
        headers[key.strip().lower()] = value.strip()
    body = await reader.readexactly(int(headers.get("content-length", 0)))

    await asyncio.sleep(0.1)  # 模拟推理延迟

    global in_flight, peak
    in_flight += 1
    peak = max(peak, in_flight)
    data = json.dumps({"ok": True, "n": len(body)}).encode()
    writer.write(b"HTTP/1.1 200 OK\r\n" + f"Content-Length: {len(data)}\r\n".encode() + b"Connection: close\r\n\r\n" + data)
    await writer.drain()
    in_flight -= 1
    writer.close()
    await writer.wait_closed()


async def one(i, sem):
    async with sem:
        status, body = await apost(f"http://127.0.0.1:8765/req{i}", json_data={"i": i})
        assert status == 200, status
        return json.loads(body)


async def main():
    global in_flight, peak
    in_flight, peak = 0, 0

    server = await asyncio.start_server(handle, "127.0.0.1", 8765)
    sem = asyncio.BoundedSemaphore(10)  # 模拟 max_concurrent_requests_limit

    t0 = time.monotonic()
    results = await asyncio.gather(*(one(i, sem) for i in range(50)))
    elapsed = time.monotonic() - t0

    print(f"完成 {len(results)} 个请求，峰值并发 {peak}，总耗时 {elapsed:.2f}s")
    print(f"理论耗时 = ceil(50/10) * 0.1s = 0.5s")

    server.close()
    await server.wait_closed()


asyncio.run(main())
```

**观察与思考**：

1. `峰值并发` 应不超过 10（注意：`one` 里 `async with sem` 之后才调用 `apost`，与 `try_single_page` 的结构一致；峰值统计放在服务器端，测的是真实在途请求数）。
2. 总耗时应约 0.5 秒。把 `BoundedSemaphore(10)` 改成 `(50)`，耗时降到约 0.1 秒、峰值并发约 50——这正是 `--max_concurrent_requests` 这个旋钮在做的事。
3. 把服务器里的 `await asyncio.sleep(0.1)` 临时改成 `writer.close(); await writer.wait_closed(); return`（空响应），观察 `apost` 抛 `ConnectionError`、50 个任务全部异常退出——设想此时若外面有 `try_single_page_with_backoff`，会发生什么（答案：每个任务各自独立退避，互不影响，最坏各自退满 10230 秒后进程退出）。
4. 思考：为什么压测里不需要关闭 keep-alive 相关的处理？（因为 `apost` 每个请求新建连接且发送 `Connection: close`，服务器端每个连接只服务一次。）

## 6. 本讲小结

- `apost`（[pipeline.py:L378-L476](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L378-L476)）是约 95 行的手写异步 HTTP POST：`urlparse` 拆地址 → `asyncio.open_connection` 建 TCP/TLS → 手工拼 HTTP/1.1 报文 → 解析状态行与小写化的头 → 按 `Content-Length` / chunked / 读到关闭 三分支读体 → `finally` 必关 socket。
- 手写的动机是**确定性**：httpcore/aiohttp 的连接池带多把锁，在亿级请求规模下偶发死锁；热路径改用「一请求一连接（`Connection: close`）+ 零池化」消除了整类问题，代价是每请求一次握手——对本地 vLLM 可忽略。低频的启动探活仍用 httpx（[pipeline.py:L929-L940](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L929-L940)）。
- `apost` 的隐含正确性前提：`json.dumps` 默认 `ensure_ascii=True` 使字符数等于字节数，`Content-Length` 才不会算错；异常一律上抛、绝不吞掉。
- 两个 `BoundedSemaphore` 闸门分工明确：渲染闸门（CPU 数，import 时定）限 pdftoppm 子进程并发，请求闸门（`--max_concurrent_requests` 默认 1600，`main()` 里重建）限全局在途请求数；闸门只包 `apost` 调用本身。
- 异常分三路：`CancelledError` 穿透；`ConnectionError / OSError / asyncio.TimeoutError` 上抛进退避；其余降级为 `None` 走温度阶梯。
- `try_single_page_with_backoff` 以 \( t_n = 10 \times 2^n \) 秒退避、最多 10 次（累计睡眠 10230 秒 ≈ 2.84 小时），期间 `attempt` 不变、温度不升；耗尽后 `sys.exit(1)` 终止整个作业——基础设施失败宁死不静默降级，与模型层失败走 pdftotext 兜底的哲学形成对照。

## 7. 下一步学习建议

- **下一讲（u2-l8）**将讲输出侧：`build_dolma_document` 如何把各页 `PageResult` 拼成 Dolma JSON。你会看到 `apost` 拿回并解析出的 `input_tokens / output_tokens` 如何流入文档属性与指标统计。
- 建议重读 [pipeline.py:L149-L231](https://github.com/allenai/olmocr/blob/f7cfe4c22098b154c76b6ec950d1c0a464eecf8d/olmocr/pipeline.py#L149-L231)，从「一次调用的视角」把本讲与 u2-l4（请求体里的提示词与图片）、u2-l5（响应体如何被 `FrontMatterParser` 解析）首尾连通。
- 对「为什么通用 HTTP 客户端在极端规模下会死锁」感兴趣，可对照阅读 httpx 底层 httpcore 的连接池实现（本项目未 vendored，需自行阅读其源码），数一数作者提到的「4 把锁」。
- 若想看同一项目里 httpx 的正常用法，读 `olmocr/bench/runners/run_server.py` 的请求部分，体会「低频路径用成熟库、热路径用极简实现」的分层取舍。
