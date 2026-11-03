# RPC 方法分发：XML-RPC 与 JSON-RPC

## 1. 本讲目标

上一讲（u8-l3）我们走通了 `--enable-rpc` 之后 HTTP/WebSocket 服务器的监听、请求解析与三棒命令接力，最后停在了 `processJsonRpcRequest` 门口。本讲跨过这道门，回答三个问题：

1. 一个 HTTP 请求体（JSON 或 XML 文本）如何变成统一的 `RpcRequest`，方法名如何找到对应的 C++ 对象并执行？
2. `--rpc-secret` 令牌鉴权到底发生在哪一行代码？
3. `aria2.addUri`、`aria2.tellStatus`、`aria2.changeOption`、`aria2.remove` 这些常用方法在源码里分别做了什么？

学完本讲，你应该能：

- 说出 `RpcMethod::execute()` 的「authorize → process → 异常包装」三段流程；
- 跟踪 `HttpServerBodyCommand` 中 XML/JSON 两条解析分支与响应构造（含 HTTP 状态码映射）；
- 解释 `RpcMethodFactory` 的方法名分发与缓存机制；
- 独立用 curl 完成「添加下载 → 轮询进度 → 改选项 → 删除任务」的 RPC 全流程。

## 2. 前置知识

**RPC（Remote Procedure Call，远程过程调用）**：让客户端像调用本地函数一样调用服务器上的功能。aria2 的 RPC 跑在 HTTP 之上，支持两种「方言」：

| 方言 | 请求格式 | 响应格式 | 典型客户端 |
|---|---|---|---|
| XML-RPC | `<methodCall><methodName>aria2.addUri</methodName>...` | `<methodResponse><params>...` | 老牌脚本 |
| JSON-RPC 2.0 | `{"jsonrpc":"2.0","id":"x","method":"aria2.addUri","params":[...]}` | `{"id":"x","jsonrpc":"2.0","result":...}` | webui-aria2、curl |

两种方言只是「皮」不同，内核完全一样：都解析成一棵 `ValueBase` 树，都路由到同一个 `RpcMethod` 对象，执行结果也都序列化回 `ValueBase` 再按方言编码。

**ValueBase**：aria2 自己的动态类型（在 u9-l2 会深入），可以理解为 C++ 版的 JSON 值——`String`/`Integer`/`Bool`/`Null`/`List`/`Dict` 六种节点。本讲只需要知道：RPC 的参数与返回值都是 `ValueBase` 树，`downcast<T>()` 负责带类型检查的安全取值。

**GID**：u3-l1 讲过，每个下载任务的 64 位唯一标识，RPC 里以 16 进制字符串出现（如 `"2089b05ecca3d829"`），`GroupId::toHex/expandUnique` 负责字符串与数值互转。

**前置讲义**：u8-l3（HttpServer、请求类型判定 RPC_TYPE_JSON/JSONP/XML、三棒命令接力）、u1-l5（Option/OptionHandler 选项体系）、u3-l1（RequestGroup/RequestGroupMan）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `src/HttpServerBodyCommand.cc` | RPC 请求的总入口：按请求类型分流 JSON/XML，执行方法并构造 HTTP 响应 |
| `src/rpc_helper.cc` | `processJsonRpcRequest`：JSON 字典 → RpcRequest；`xmlParseMemory`：XML 文本 → RpcRequest |
| `src/XmlRpcRequestParserStateMachine.cc` | XML-RPC 请求的 SAX 状态机（与 Metalink 解析器同骨架） |
| `src/RpcMethod.h` / `src/RpcMethod.cc` | RPC 方法基类：`authorize`/`execute`/`process` 三段流程与选项收集 |
| `src/RpcMethodFactory.cc` | 方法名 → 方法对象的映射、缓存与未知方法兜底 |
| `src/RpcMethodImpl.h` / `src/RpcMethodImpl.cc` | 全部 40 个左右方法的具体实现 |
| `src/RpcResponse.h` / `src/RpcResponse.cc` | 响应结构体与 `toXml`/`toJson` 序列化 |
| `src/json.h` / `src/json.cc` | ValueBase → JSON 文本编码器、JSONP GET 参数解码 |
| `src/ValueBase.cc` | ValueBase 各节点的构造与工厂函数 `g()` |
| `src/DownloadEngine.cc` | `validateToken`：`--rpc-secret` 的 HMAC 校验 |

## 4. 核心概念与源码讲解

### 4.1 从 HTTP 请求体到 RpcRequest：JSON 与 XML 两条解析线

#### 4.1.1 概念说明

u8-l3 的结尾处，`HttpServer` 已经按「HTTP 方法 + 请求路径」把请求分成了三型：`RPC_TYPE_XML`、`RPC_TYPE_JSON`、`RPC_TYPE_JSONP`，并且请求体在接收过程中被增量喂进了各自的解析器（XML 用 `XmlRpcDiskWriter`，JSON 用 `JsonDiskWriter`）。本模块接手最后一步：**body 收满后，把两种解析器的产物统一成 `RpcRequest` 结构体**——`{方法名, 参数 List, 请求 id, 是否 JSON-RPC}`。此后所有代码都不再关心请求来自哪种方言。

#### 4.1.2 核心流程

```
HttpServerBodyCommand::execute()
├─ receiveBody() 收满？
│   └─ 否 → requeue 自己，继续等
├─ 方法 == OPTIONS？ → CORS 预检响应，结束
└─ switch (请求类型)
    ├─ RPC_TYPE_XML
    │   ├─ XmlRpcDiskWriter::finalize()   ← 解析收尾，返回错误码
    │   ├─ error < 0？ → HTTP 400，结束
    │   ├─ dw->getResult() → RpcRequest（SAX 状态机产物）
    │   ├─ rpc::getMethod(方法名) → RpcMethod*
    │   ├─ method->execute(req, e_)       ← 三段流程（4.3 节）
    │   └─ rpc::toXml(res) → HTTP 200 "text/xml"
    └─ RPC_TYPE_JSON / RPC_TYPE_JSONP
        ├─ JSONP：query 里解出 method/id/params（Base64）拼回 JSON 文本再解析
        ├─ 解析失败 → JSON-RPC 错误 -32700 "Parse error."
        ├─ 顶层是 Dict → 单次调用 processJsonRpcRequest()
        ├─ 顶层是 List → 批量调用，逐个 processJsonRpcRequest()
        └─ 其它 → -32600 "Invalid Request."
```

`processJsonRpcRequest` 对 JSON 字典做三道检查：缺 `id` 拒绝（不支持 JSON-RPC 的 notification 语义）；`method` 不是字符串拒绝；`params` 必须是数组（不支持命名参数，字典参数报 -32602）。

#### 4.1.3 源码精读

XML 分支——finalize 解析、执行、序列化、以 `text/xml` 回 200（错误也是 200，错误信息装在 `<fault>` 里）：

[HttpServerBodyCommand.cc:222-253](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc#L222-L253)：`RPC_TYPE_XML` 分支先 `finalize()` 收尾 XML 解析，失败直接回 HTTP 400；成功则 `getResult()` 取出 `RpcRequest`，日志打印方法名后交给 `getMethod()` 分发、`execute()` 执行，最后 `rpc::toXml()` 序列化响应并 `feedResponse(responseData, "text/xml")`。

JSON 分支——同样的骨架，但错误走 JSON-RPC 错误码协议：

[HttpServerBodyCommand.cc:254-312](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc#L254-L312)：JSONP 请求先从 query 解出参数，普通 JSON 则 `JsonDiskWriter::finalize()`；解析失败返回 -32700；顶层是 `Dict` 走单次调用、是 `List` 走批量调用（逐个 `processJsonRpcRequest` 收集结果后 `sendJsonRpcBatchResponse`）、否则报 -32600。

[rpc_helper.cc:80-106](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/rpc_helper.cc#L80-L106)：`processJsonRpcRequest` 依次校验 `id`（缺失即 -32600）、`method`（必须字符串）、`params`（必须是 `List`，字典参数报 -32602 "Invalid params."），然后把四个字段打包成 `RpcRequest{方法名, params, id, jsonRpc=true}` 交给 `getMethod()->execute()`。注意它用 `popValue` 把 `id`/`params` 从字典里「摘走」而不是借用，避免悬挂指针。

XML-RPC 请求本身由 SAX 状态机解析，骨架与 u8-l1 的 Metalink 解析器完全同构：

[XmlRpcRequestParserStateMachine.cc:43-81](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/XmlRpcRequestParserStateMachine.cc#L43-L81)：文件级静态单例状态对象（`methodCallState`、`paramsState`、`valueState`、`structState`……）随 `stateStack_` 压栈弹栈；[L107-L121](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/XmlRpcRequestParserStateMachine.cc#L107-L121) 的 `beginElement` 把标签分派给栈顶状态、`endElement` 处理后再弹栈——栈深度镜像 `<methodCall><params><param><value><struct>...` 的嵌套深度。

[rpc_helper.cc:53-67](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/rpc_helper.cc#L53-L67)：`xmlParseMemory` 是 XML-RPC 解析的独立入口（WebSocket 之外的场景用），解析失败抛 `DL_ABORT_EX`；栈顶值是 `List` 就直接当 params，否则补空列表。

#### 4.1.4 代码实践

1. **实践目标**：用 curl 分别以 XML-RPC 与 JSON-RPC 调用 `system.listMethods`，验证两条解析线最终产出同样的方法列表。
2. **操作步骤**（假设已按 u1-l2 编译出 `./src/aria2c`）：

```bash
# 终端 1：启动 RPC 服务
./src/aria2c --enable-rpc --rpc-listen-port=6800 --rpc-secret=secret123

# 终端 2：JSON-RPC（POST /jsonrpc）
curl http://localhost:6800/jsonrpc -d '{"jsonrpc":"2.0","id":"t1","method":"system.listMethods","params":["token:secret123"]}'

# 终端 2：XML-RPC（POST /rpc）
curl http://localhost:6800/rpc -d '<?xml version="1.0"?><methodCall><methodName>system.listMethods</methodName><params><param><value><string>token:secret123</string></value></param></params></methodCall>'
```

3. **需要观察的现象**：两次返回的方法列表内容一致；JSON 版是数组，XML 版是 `<value><array><data>...`。
4. **预期结果**：两种方言仅编码不同。若返回 `Unauthorized`，检查 token 是否写对（鉴权见 4.3 节）。
5. 以上命令为常见用法，具体行为**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：发一个 `params` 是字典（命名参数）的 JSON-RPC 请求，aria2 返回什么？由哪行代码产生？

**答案**：返回 JSON-RPC 错误 `-32602 "Invalid params."`，由 [rpc_helper.cc:99-102](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/rpc_helper.cc#L99-L102) 产生——`params` 既不是 `List` 也不是空时（即是 `Dict`），直接构造 -32602 错误响应。

**练习 2**：故意发一段非法 JSON（如 `{"jsonrpc":`），HTTP 状态码和错误码是多少？

**答案**：HTTP 500，JSON 错误码为 `-32700 "Parse error."`。解析错误经 `createJsonRpcErrorResponse(-32700, ...)` 构造响应后仍走 `sendJsonRpcResponse`（见 [HttpServerBodyCommand.cc:275-283](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc#L275-L283)），因其 `res.code` 非零进入状态码映射 switch，-32700 不匹配 `1`/`-32600`/`-32601` 任何 case，落入 [L127-L129](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc#L127-L129) 的 `default` 分支得到 500。

### 4.2 ValueBase：两种 RPC 方言的统一中间表示

#### 4.2.1 概念说明

RPC 的参数和返回值必须是「既能从 JSON/XML 来、又能回到 JSON/XML 去」的通用数据。aria2 不为每种方言定义一套类型，而是让一切流经 `ValueBase`：**解析器产出 ValueBase 树 → 方法实现读/写 ValueBase 树 → 序列化器遍历 ValueBase 树输出文本**。序列化靠访问者模式（`accept(visitor)`），同一棵树配不同 visitor 就得到不同方言的输出。

#### 4.2.2 核心流程

```
请求文本 ──解析──▶ ValueBase 树 ──execute──▶ ValueBase 结果树 ──visitor──▶ 响应文本
                    │                             │
                    │ downcast<T>() 类型安全取值    │ accept() 双分派
                    ▼                             ▼
              JsonValueBaseVisitor            XmlValueBaseVisitor
              （json.h 模板 encode）           （RpcResponse.cc encodeValue）
```

#### 4.2.3 源码精读

JSON 序列化 visitor：

[json.h:48-115](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/json.h#L48-L115)：`json::encode` 是模板函数，内部 `JsonValueBaseVisitor` 按 ValueBase 实际类型输出——`Integer` 直接流式输出数字、`Bool` 输出 `true`/`false`、`Null` 输出 `null`、`Dict` 输出 `{...}`。注意 `List`/`Dict` 的遍历是递归 `accept`，天然支持任意嵌套。

[json.cc:48-93](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/json.cc#L48-L93)：`jsonEscape` 处理 JSON 转义——`"`、`\`、`/` 加反斜杠，控制字符转 `\n`/`\t`/`\u00XX`。

XML 序列化 visitor 有一个重要差异——**Bool 和 Null 的 visit 是空实现**：

[RpcResponse.cc:51-100](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcResponse.cc#L51-L100)：`XmlValueBaseVisitor` 只实现 `String`（`<value><string>`）、`Integer`（`<value><int>`）、`List`（`<array><data>`）、`Dict`（`<struct><member>`）四种；[L73-L75](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcResponse.cc#L73-L75) 的 `Bool`/`Null` 什么都不输出。这解释了 `RpcMethodImpl.cc` 里的一个「怪现象」：布尔字段一律用字符串 `"true"`/`"false"`（常量 `VLB_TRUE`/`VLB_FALSE`，见 [RpcMethodImpl.cc:87-88](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc#L87-L88)），而不是 `Bool` 节点——否则 XML-RPC 客户端会收到空 `<value></value>`。

[RpcResponse.cc:153-178](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcResponse.cc#L153-L178)：`encodeJsonAll` 拼出 JSON-RPC 2.0 响应外壳 `{"id":...,"jsonrpc":"2.0","result":...}` 或 `"error":...`；`callback` 非空时外包 `callback(...)` 变成 JSONP。[L180-L198](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcResponse.cc#L180-L198) 的 `toJson` 支持经 GZipEncoder 压缩输出（客户端带 `Accept-Encoding: gzip` 时）。

[json.cc:108-164](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/json.cc#L108-L164)：`decodeGetParams` 解析 JSONP 的 GET query——取 `method=`、`id=`、`params=`（percent-decode + Base64 decode）与 `jsoncallback=`，手工拼回一段合法 JSON 文本再交给 JSON 解析器；method 与 id 都缺省时按批量调用处理。

#### 4.2.4 代码实践

1. **实践目标**：直观验证「XML-RPC 序列化丢 Bool」这一源码结论。
2. **操作步骤**：分别用 JSON-RPC 和 XML-RPC 调 `aria2.getVersion` 或带 BT 任务的 `aria2.tellStatus`（其结果含 `selected`、`seeder` 等布尔字段），对比两边的字段值。
3. **需要观察的现象**：JSON 响应里 `selected` 的值是字符串 `"true"` 而非布尔 `true`（因为源码放的就是 String 节点）；XML 响应里同样是 `<string>true</string>`。
4. **预期结果**：印证 4.2.3 的分析——RPC 返回的「布尔」实际是字符串，这是为兼容 XML-RPC 序列化器的刻意选择。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`tellStatus` 返回的 `completedLength` 是 `Integer` 节点，JSON 客户端拿到的是数字。如果这个值超过 JavaScript 的安全整数（2^53-1）会怎样？源码层面有防护吗？

**答案**：源码没有防护。[json.h:59-62](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/json.h#L59-L62) 直接把 `int64_t` 流式输出为数字字面量，精度问题由客户端（如 webui 用字符串解析）自行处理。这也是 aria2 文档提醒超大数值需注意的根源。

**练习 2**：为什么 `processJsonRpcRequest` 里用 `popValue` 而不是 `get` 来取 `id` 和 `params`？

**答案**：`popValue` 把节点从字典中摘除并交出所有权，`RpcRequest` 因此能以 `unique_ptr` 独占持有参数列表（后续 `authorize` 还要对它 `pop_front` 剥 token）；若用 `get` 借指针，字典销毁后就是悬垂指针。参见 [rpc_helper.cc:82-95](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/rpc_helper.cc#L82-L95)。

### 4.3 RpcMethod 基类：authorize → process → 异常包装三段流程

#### 4.3.1 概念说明

所有 40 个左右 RPC 方法共享同一个基类骨架。[RpcMethod.h:57-64](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethod.h#L57-L64) 的注释明确了设计约定：**子类必须无状态**（实例会被缓存复用），只需实现 `process()`。`execute()` 是模板方法：先 `authorize()` 鉴权，再调 `process()` 干活，最后把可能抛出的异常统一包装成错误响应。

#### 4.3.2 核心流程

```
execute(req, e)
├─ ① authorize(req, e)
│   ├─ 首参数是 "token:xxx" 字符串？→ 剥离出 token 并 pop_front
│   ├─ validateToken(token) 失败 → 抛 DL_ABORT_EX("Unauthorized")
│   └─ 成功 → authorized 标志置 AUTHORIZED
├─ ② process(req, e)           ← 子类实现的业务逻辑，返回 ValueBase
└─ ③ 异常边界：catch RecoverableException
    └─ createErrorResponse：JSON 用 code/message，XML 用 faultCode/faultString
       → RpcResponse(code=1, ...)
```

#### 4.3.3 源码精读

鉴权与 token 剥离——本讲实践任务问的「secret 在哪一行检查」就是这里：

[RpcMethod.cc:70-89](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethod.cc#L70-L89)：`authorize` 检查第一个参数，若是以 `token:` 开头的字符串就取出后 6 字节之后的部分并把它**从参数列表里删掉**——注释写明这是为了让所有方法实现都不必感知 token 的存在；随后第 86 行 `if (!e || !e->validateToken(token))` 是鉴权判定的落点，失败抛 `"Unauthorized"`。

[DownloadEngine.cc:615-634](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L615-L634)：真正的密钥比对在 `DownloadEngine::validateToken`——未设置 `--rpc-secret` 直接放行；否则用随机 HMAC 密钥分别计算「期望值」（对 secret）与「请求值」（对 token）再比较，等价于常数时间比较，可抵御时序攻击。

模板方法 execute 与错误包装：

[RpcMethod.cc:91-105](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethod.cc#L91-L105)：`execute` 先把 `authorized` 预置为 `NOTAUTHORIZED`，`authorize` 通过后才改写为 `AUTHORIZED`——因此即便鉴权失败后走了 catch 分支，响应里也带着「未授权」标记（供上层加延迟）。`process` 抛出的任何 `RecoverableException` 都被转成 `code=1` 的错误响应而不是让异常穿透到命令层。

[RpcMethod.cc:61-68](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethod.cc#L61-L68)：`createErrorResponse` 按 `req.jsonRpc` 标志选择键名——JSON-RPC 用 `code`/`message`，XML-RPC 用 `faultCode`/`faultString`（XML-RPC 规范的 fault 结构）。

选项收集辅助函数（addUri 的第二个参数 `options` 字典就靠它落地）：

[RpcMethod.cc:141-148](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethod.cc#L141-L148)：`gatherRequestOption` 遍历 options 字典，把键转成 `PrefPtr` 后查 `OptionParser`，**只接受带 initialOption 标志的选项**（即 u3-l3 讲过的「条目级可覆盖白名单」），不认识的键静默忽略。cumulative 选项（如 `header`）还接受数组值逐条 parse，见 [L108-L138](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethod.cc#L108-L138)。

未授权响应的额外惩罚——延迟一秒：

[HttpServerBodyCommand.cc:103-134](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc#L103-L134)：`sendJsonRpcResponse` 按 `res.code` 映射 HTTP 状态码（`1`→400、`-32600`→400、`-32601`→404、其余→500），并且当 `not_authorized(res)` 为真时调用 [L147-L159](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc#L147-L159) 的 `addHttpServerResponseCommand(true)`——响应命令被包进 `DelayedCommand` 延迟 1 秒才发出，同时禁用 keep-alive，抬高暴力猜测 token 的成本。

#### 4.3.4 代码实践

1. **实践目标**：体感验证「无 token → Unauthorized + 延迟」，并在源码中标出检查点。
2. **操作步骤**：

```bash
# 不带 token 调用（服务端设置了 --rpc-secret=secret123）
time curl -s http://localhost:6800/jsonrpc \
  -d '{"jsonrpc":"2.0","id":"t2","method":"aria2.getVersion","params":[]}'

# 再带上正确 token 对比耗时
time curl -s http://localhost:6800/jsonrpc \
  -d '{"jsonrpc":"2.0","id":"t3","method":"aria2.getVersion","params":["token:secret123"]}'
```

3. **需要观察的现象**：第一次返回 HTTP 400，body 是 `{"id":"t2","jsonrpc":"2.0","error":{"code":1,"message":"Unauthorized"}}`，且明显耗时约 1 秒；第二次正常返回版本信息，耗时毫秒级。
4. **预期结果**：鉴权检查点在 [RpcMethod.cc:86](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethod.cc#L86) 的 `validateToken` 调用（抛 Unauthorized 也在 [L86-L88](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethod.cc#L86-L88)），密钥比对在 [DownloadEngine.cc:615-634](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadEngine.cc#L615-L634)，1 秒延迟来自 [HttpServerBodyCommand.cc:151-154](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc#L151-L154) 的 `DelayedCommand`。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `execute` 要把 `authorized` 预置为 `NOTAUTHORIZED` 而不是成功后再置位？

**答案**：因为 `authorize` 抛出的异常会直接跳到 catch 分支；若预置为 AUTHORIZED，未授权响应就丢失了标记，上层 `not_authorized(res)` 判断失效，1 秒延迟惩罚也就不会触发。预置 NOTAUTHORIZED 保证了任何异常路径下标记都是保守正确的。参见 [RpcMethod.cc:93-96](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethod.cc#L93-L96)。

**练习 2**：`aria2.addUri` 的 options 参数里传 `{"max-download-limit":"10M"}` 与 `{"dir":"/tmp"}`，哪个会生效？

**答案**：都生效（两者都是 initial option）。但若传了不在白名单内的选项（如 `{"rpc-secret":"x"}`），会被静默忽略而非报错——因为 [RpcMethod.cc:117-120](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethod.cc#L117-L120) 对 `!handler || !pred(handler)` 的处理是 `continue`。

### 4.4 RpcMethodFactory：方法名分发、缓存与未知方法兜底

#### 4.4.1 概念说明

「方法名字符串 → C++ 对象」的翻译工作由 `RpcMethodFactory` 完成。它不是注册表式的 map（没有宏注册魔法），而是一张**方法名清单 + 一条 if 链 + 一个进程级缓存**。清单同时服务于 `system.listMethods` 的应答与文档生成，缓存则保证方法对象只构造一次（这也解释了为什么 `RpcMethod` 子类必须无状态）。

#### 4.4.2 核心流程

```
getMethod(methodName)
├─ cache（map<string, unique_ptr<RpcMethod>>）命中 → 返回缓存对象
├─ 未命中 → createMethod(methodName)
│   ├─ 逐个 if 比较 getMethodName() → make_unique<具体类>()
│   └─ 全不匹配 → nullptr
└─ nullptr → 返回单例 noSuchRpcMethod（NoSuchMethodRpcMethod）
                └─ process() 只会抛 "No such method: xxx"
```

#### 4.4.3 源码精读

方法名总清单（注意条件编译条目）：

[RpcMethodFactory.cc:53-94](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodFactory.cc#L53-L94)：`rpcMethodNames` 罗列全部方法——`aria2.addTorrent`、`aria2.getPeers` 只在 `ENABLE_BITTORRENT` 时存在，`aria2.addMetalink` 只在 `ENABLE_METALINK` 时存在，印证 u1-l1「特性与编译宏一一绑定」的结论。`system.multicall`、`system.listMethods`、`system.listNotifications` 三个系统方法在队尾。

[RpcMethodFactory.cc:116-267](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodFactory.cc#L116-L267)：`createMethod` 是纯 if 链，每个方法类都以 `getMethodName()` 静态函数声明自己的名字，保证「清单、分发、类定义」三处字符串不会漂移；结尾 `return nullptr` 表示未知方法。

[RpcMethodFactory.cc:270-288](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodFactory.cc#L270-L288)：`getMethod` 先查 `cache`（[L45](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodFactory.cc#L45) 的匿名命名空间 map），命中直接返回；未命中则创建并 `insert`。未知方法不进缓存，而是返回懒初始化的单例 `noSuchRpcMethod`（[L49-50](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodFactory.cc#L49-L50)、[L280-284](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodFactory.cc#L280-L284)）。

[RpcMethodImpl.cc:1519-1523](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc#L1519-L1523)：`NoSuchMethodRpcMethod::process` 只有一行——抛出 `"No such method: <名字>"`，经基类 `execute` 包装成 code=1 错误，HTTP 层映射为 400。顺带一提：`HttpServerBodyCommand.cc` switch 里的 `case -32601: httpCode = 404`（[L124-125](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpServerBodyCommand.cc#L124-L125)）目前是防御性分支——当前代码没有任何路径产生 -32601，未知方法实际走 code=1。

#### 4.4.4 代码实践

1. **实践目标**：验证「未知方法的兜底」与方法清单的一致性。
2. **操作步骤**：

```bash
# 调一个不存在的方法
curl -s http://localhost:6800/jsonrpc -d '{"jsonrpc":"2.0","id":"t4","method":"aria2.nosuchmethod","params":["token:secret123"]}'
# 列出全部方法，数一数个数
curl -s http://localhost:6800/jsonrpc -d '{"jsonrpc":"2.0","id":"t5","method":"system.listMethods","params":["token:secret123"]}' | python3 -c 'import json,sys;print(len(json.load(sys.stdin)["result"]))'
```

3. **需要观察的现象**：第一个请求返回 `error.code=1`、`message="No such method: aria2.nosuchmethod"`；第二个请求返回的数组长度应等于 `rpcMethodNames` 在当前编译配置下的条目数（全特性约 40 个）。
4. **预期结果**：与 [RpcMethodFactory.cc:53-94](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodFactory.cc#L53-L94) 和 [RpcMethodImpl.cc:1481-1489](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc#L1481-L1489) 完全对应。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：如果要新增 `aria2.myMethod`，需要改哪几个文件？

**答案**：四步——① 在 `RpcMethodImpl.h/cc` 定义继承 `RpcMethod` 的类，实现 `process()` 与静态 `getMethodName()`；② 在 `RpcMethodFactory.cc` 的 `rpcMethodNames` 清单中加入名字；③ 在 `createMethod()` 的 if 链中加一个分支；④ 重新编译。这也是 u9-l6 二次开发实践的雏形（基类注释 [RpcMethod.h:62-64](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethod.h#L62-L64) 明确要求别忘 Factory）。

**练习 2**：为什么 `noSuchRpcMethod` 不放进 cache？

**答案**：cache 的键是合法方法名；未知方法名空间无限大，放进 cache 既浪费内存也无收益。单例 + 惰性初始化让所有未知方法共享同一个兜底对象（其 `process` 从 `req.methodName` 动态取名字拼错误消息，无需按名缓存）。

### 4.5 常用方法实现精读：addUri / tellStatus / changeOption / remove 与 system.multicall

#### 4.5.1 概念说明

这一节把镜头对准具体方法。它们的共同套路是：**`checkParam` 系列做参数校验 → 从 `DownloadEngine` 拿到 `RequestGroupMan` 等部件 → 干活 → 返回 GID 字符串 / 状态字典 / "OK"**。参数校验模板定义在 `RpcMethodImpl.h`：

[RpcMethodImpl.h:62-76](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.h#L62-L76)：`checkParam<T>(req, index, required)` 用 `downcast<T>` 检查第 index 个参数的类型，类型不符抛「wrong type」，`required=true` 且缺位抛「is required but missing」——RPC 方法的第一道防线。

#### 4.5.2 核心流程

以 `aria2.addUri` 为例（其余方法同构）：

```
AddUriRpcMethod::process
├─ checkRequiredParam<List>(0)   ← uris 数组，必填
├─ checkParam<Dict>(1)           ← options 字典，可选
├─ checkParam<Integer>(2)        ← position，可选
├─ extractUris 抽出字符串列表，为空则抛 "URI is not provided."
├─ 复制全局 Option 为 requestOption（任务级选项的父链起点，承接 u1-l5）
├─ gatherRequestOption 合并 options 字典（白名单过滤）
└─ createRequestGroupForUri(...) ← u3-l3 讲过的任务创建流水线原样复用！
    └─ addReservedGroup / insertReservedGroup 入等待队列
        └─ 返回 GID 十六进制字符串
```

#### 4.5.3 源码精读

**addUri——RPC 与命令行共用一条任务创建流水线**：

[RpcMethodImpl.cc:228-258](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc#L228-L258)：参数三连校验后，以 `*e->getOption()` 为底本复制出任务选项，`gatherRequestOption` 合并 options 字典，然后直接调用 u3-l3 精读过的 `createRequestGroupForUri`（注意两个实参 `ignoreForceSeq=true, ignoreLocalPath=true`——RPC 场景禁止参数化 URI 展开与本地文件路径嗅探）。返回值经 [L153-L156](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc#L153-L156) 的 `createGIDResponse` 转成 GID 十六进制字符串；入队逻辑在 [L164-L175](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc#L164-L175)（给了 position 就插队，否则尾插）。

**tellStatus——活动任务与已停任务两条数据来源**：

[RpcMethodImpl.cc:998-1035](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc#L998-L1035)：先 `str2Gid` 解析 GID（[L194-L211](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc#L194-L211)，支持唯一前缀匹配），第二个参数 keys 数组做字段过滤（`requested_key`：keys 为空返回全部，否则只放行点名的键）；`findGroup` 找不到活动任务就转 `findDownloadResult` 找已停快照（u3-l2 的 DownloadResult），两条路分别走 `gatherProgress`（活动态）与 `gatherStoppedDownload`（终态）。

[RpcMethodImpl.cc:616-691](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc#L616-L691)：`gatherProgressCommon` 逐键填充 `gid`/`totalLength`/`completedLength`/`downloadSpeed`/`bitfield`/`files` 等字段，数据来自 RequestGroup、PieceStorage 与 `calculateStat()`——这正是 u3-l5 讲过的 TransferStat 快照。BT 任务在 [L788-L817](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc#L788-L817) 的 `gatherProgress` 中追加 peer/seeder 信息，校验中的任务还会附 `verifiedLength`。

**changeOption——「活动任务先暂停再改」的两阶段语义**：

[RpcMethodImpl.cc:1109-1144](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc#L1109-L1144)：对等待中的任务，用 `gatherChangeableOptionForReserved` 收集允许改的选项后直接 `changeOption` 生效；对**活动中的任务**则分两类——可运行时改的进 `option`，只能下次启动生效的进 `pendingOption`，一旦 pendingOption 非空就调 `pauseRequestGroup` 暂停任务并置 `setRestartRequested(true)`，等任务回到等待队列后由引擎重启应用。全局版本对应 [L1147-L1155](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc#L1147-L1155) 的 `changeGlobalOption`。

**remove——只「请求停止」，不立即删除**：

[RpcMethodImpl.cc:389-421](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc#L389-L421)：`removeDownload` 对活动任务只置 `setHaltRequested`（force 版置 `setForceHaltRequested`），实际收割由引擎主循环完成——RPC 是单线程事件循环的一环（u8-l3 结论），绝不能在这里同步等待任务结束；对等待任务且依赖已解决的才直接 `removeReservedGroup`。找不到任务抛 "Active Download not found"。

**system.multicall——唯一重写 execute 的方法**：

[RpcMethodImpl.cc:1421-1478](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc#L1421-L1478)：`SystemMulticallRpcMethod::execute` 不走基类的 authorize/process 骨架（其 `process` 里干脆 `assert(false)`，见 [L1413-L1419](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc#L1413-L1419)），而是遍历参数里的方法规格数组，为每个子调用构造独立 `RpcRequest` 后递归 `getMethod()->execute()`——**鉴权因此发生在每个子调用内部**，token 要写在各子调用的 params 首位而非外层；递归 multicall 被显式拒绝（[L1441-L1444](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc#L1441-L1444)）。成功子调用的结果被包成单元素数组（XML-RPC multicall 规范要求）。

#### 4.5.4 代码实践

1. **实践目标**：走通「addUri → tellStatus 轮询 → changeOption → remove」全流程，观察状态机迁移。
2. **操作步骤**：

```bash
# ① 添加下载（一个大文件便于观察），保存返回的 gid
GID=$(curl -s http://localhost:6800/jsonrpc -d '{"jsonrpc":"2.0","id":"q1","method":"aria2.addUri","params":["token:secret123",["https://releases.ubuntu.com/22.04/ubuntu-22.04.5-live-server-amd64.iso"],{"dir":"/tmp"}]}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["result"])')
echo "GID=$GID"

# ② 轮询进度（只取关心的键）
curl -s http://localhost:6800/jsonrpc -d "{\"jsonrpc\":\"2.0\",\"id\":\"q2\",\"method\":\"aria2.tellStatus\",\"params\":[\"token:secret123\",\"$GID\",[\"gid\",\"status\",\"totalLength\",\"completedLength\",\"downloadSpeed\"]]}"

# ③ 运行中改限速（触发"暂停-重启"路径）
curl -s http://localhost:6800/jsonrpc -d "{\"jsonrpc\":\"2.0\",\"id\":\"q3\",\"method\":\"aria2.changeOption\",\"params\":[\"token:secret123\",\"$GID\",{\"max-download-limit\":\"500K\"}]}"

# ④ 删除任务
curl -s http://localhost:6800/jsonrpc -d "{\"jsonrpc\":\"2.0\",\"id\":\"q4\",\"method\":\"aria2.remove\",\"params\":[\"token:secret123\",\"$GID\"]}"
```

3. **需要观察的现象**：②中 `status` 从 `active` 变化，`completedLength` 持续增长；③返回 `"OK"` 后，若当时任务在下载，状态会短暂经历暂停/重启（开 `-l rpc.log --log-level=info` 能看到 `Executing RPC method ...` 日志行，它来自 [rpc_helper.cc:103](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/rpc_helper.cc#L103)）；④返回原 GID，随后的 `tellStatus` 显示 `status` 为 `removed`（来自 [RpcMethodImpl.cc:833-843](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc#L833-L843) 的 DownloadResult 分支）。
4. **预期结果**：四个调用全部按源码语义返回；下载 URL 可替换为任意可达大文件。**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`aria2.remove` 一个正在下载的任务后立刻 `aria2.tellStatus`，`status` 是什么？为什么不是马上消失？

**答案**：是 `removed`。因为 `removeDownload` 只置了 `setHaltRequested` 标志就返回 GID，真正的停机收割要等引擎主循环按 u2-l3 的命令退场机制完成，完成后任务进入 `DownloadResults` 列表，`tellStatus` 走 [RpcMethodImpl.cc:1010-1017](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc#L1010-L1017) 的 `findDownloadResult` 分支并映射为 `removed` 状态（[L833-836](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc#L833-L836)）。

**练习 2**：`aria2.addTorrent` 在 JSON-RPC 与 XML-RPC 下参数格式有何不同？源码依据？

**答案**：JSON-RPC 下 torrent 内容必须 Base64 编码（`process` 里 `if (req.jsonRpc)` 分支做 `base64::decode`），XML-RPC 下则用 XML-RPC 原生 `<base64>` 类型、解析器已自动解码。见 [RpcMethodImpl.cc:279-284](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc#L279-L284)。

**练习 3**：`system.multicall` 里嵌套调用 `system.multicall` 会怎样？

**答案**：返回错误 "Recursive system.multicall forbidden."，见 [RpcMethodImpl.cc:1441-1444](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RpcMethodImpl.cc#L1441-L1444)。

## 5. 综合实践

**任务：写一个 30 行的 aria2 命令行控制脚本，把本讲的分发链路整体串起来。**

要求实现四个子命令，每个子命令对应本讲一个模块的验证点：

1. `list`：调 `system.listMethods`，统计方法数，并与 `src/RpcMethodFactory.cc` 中 `rpcMethodNames` 的条目数人工核对（模块 4.4 的分发清单）。
2. `auth`：先用错误 token 调 `aria2.getVersion` 计时，再用正确 token 计时，输出两次耗时差（模块 4.3 的鉴权 + 延迟惩罚）。
3. `lifecycle <url>`：`addUri` → 循环 `tellStatus` 直到 `status != active`（每 2 秒一次，打印 `completedLength/totalLength` 百分比）→ 全程开 `--log-level=info` 日志，结束后从日志里数出 `Executing RPC method` 出现次数并与方法调用次数核对（模块 4.1/4.5 的执行链）。
4. `format`：对同一方法 `aria2.getVersion` 分别发 JSON-RPC 与 XML-RPC 请求，把两份响应保存成文件，手工画出各自的 ValueBase 树并标注序列化差异（模块 4.2 的统一中间表示）。

参考实现要点：用 `python3` 的 `urllib.request` 或 `curl` 均可；JSON-RPC 端点为 `http://HOST:PORT/jsonrpc`，XML-RPC 为 `http://HOST:PORT/rpc`；token 一律作为 params 第一个元素 `"token:..."` 传入。完成后回答思考题：**为什么整个 RPC 子系统里找不到一个 mutex/锁？**（提示：回顾 u8-l3「RPC 服务不是独立线程，而是单线程事件循环里的普通命令」——所有方法都在引擎线程内执行，天然串行。）

## 6. 本讲小结

- **一条流水线**：JSON/XML 请求体 → `ValueBase` 树 → `RpcRequest{方法名, params, id}` → `getMethod()` 分发 → `RpcMethod::execute()` → `ValueBase` 结果 → `toXml`/`toJson` 按方言序列化。
- **三段流程**：`execute()` = `authorize()`（剥 `token:` 前缀参数 + `validateToken` HMAC 校验）→ 子类 `process()` → 异常统一包装为 code=1 错误响应；未授权响应会被 `DelayedCommand` 延迟 1 秒发出。
- **分发即 if 链 + 缓存**：`RpcMethodFactory` 用方法名清单（受编译宏裁剪）+ `createMethod` if 链 + 进程级缓存实现分发；未知方法落到单例 `NoSuchMethodRpcMethod`，实际返回 HTTP 400（switch 里的 -32601→404 是防御性死分支）。
- **统一中间表示的取舍**：XML 序列化 visitor 不支持 `Bool`/`Null`，因此 RPC 布尔字段一律用 `"true"`/`"false"` 字符串；`params` 只支持位置参数（JSON 字典参数报 -32602）。
- **复用而非重建**：`addUri` 直接调 u3-l3 的 `createRequestGroupForUri` 任务创建流水线；`remove`/`pause` 只置标志位、收割交给事件循环；`changeOption` 对活动任务走「暂停→pendingOption→重启」两阶段。
- **`system.multicall` 是唯一重写 `execute` 的方法**：子调用各自鉴权（token 须嵌在各子 params 里），禁止递归 multicall。

## 7. 下一步学习建议

本讲之后，单元 8（Metalink 与 RPC）只剩最后一讲：

- **u8-l5 libaria2：把 aria2 嵌入你的程序**——把控制面从「网络 RPC」换成「进程内 C API」，对比 `aria2::session`/`aria2::run()` 与本讲 RPC 方法的异同（两者共用同一套引擎与 `RequestGroup` 体系）。
- 想深入解析器内部，先读 **u9-l2 ValueBase 与解析器骨架**：本讲的 `JsonDiskWriter`/`XmlRpcRequestParserStateMachine` 都建立在其上（`StructParserStateMachine` 的状态机骨架在 bencode/JSON/XML 三处复用）。
- 想亲手加一个 RPC 方法，把本讲练习 4.4-1 的四步改动与 **u9-l6 二次开发实战** 的「选项驱动功能开关」结合，走完设计-实现-验证全流程。
- 源码方面建议顺序精读：`rpc_helper.cc`（100 行）→ `RpcMethod.cc`（220 行）→ `RpcMethodFactory.cc`（290 行）→ `RpcMethodImpl.cc` 按需查读（`getMethodName()` 是最好的目录索引）。
