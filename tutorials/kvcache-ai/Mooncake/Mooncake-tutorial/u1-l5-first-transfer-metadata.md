# 运行元数据服务器并完成首条跨进程传输

> 上一讲 [u1-l4](u1-l4-python-te-quickstart.md) 你用 `P2PHANDSHAKE` 模式跑通了第一次传输，但被一个细节卡住：**两个终端之间必须手工把 `get_rpc_port()` 打印出来的真实端口抄过去**。本讲就来解决这个痛点——引入一个「元数据服务器」，让发送端和接收端通过它自动发现彼此，再也不用抄端口。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚 Transfer Engine 里「元数据（metadata）」到底存了什么、为什么必须有一个地方存它。
2. 区分三种元数据后端：**P2P 握手（P2PHANDSHAKE）**、**HTTP**、**etcd**，知道它们各自适用什么场景、引擎是凭哪一个字符串决定走哪条路的。
3. 亲手启动一个零依赖的 Python HTTP 元数据服务器（`bootstrap_server.py`），并用 `curl` 观察它收到的注册请求。
4. 用「元数据服务器作为中介」的方式，在**两个独立进程**之间完成一次真正的跨进程 buffer 传输（put/get 式），并校验数据一致。

本讲是 beginner 阶段的「收官」：你将第一次让两个互不感知的进程，借助一个第三方服务，找到对方并交换数据。

## 2. 前置知识

在开始之前，先建立三个直觉。

**为什么要「元数据服务」？**
上一讲你学到：一段内存要能被网络传输，必须先「注册」，注册后引擎会为它生成一份描述（地址是多少、长度多少、RDMA 的 `rkey` 是多少、握手端口在哪……）。问题来了——**发送端怎么知道接收端注册的内存长什么样？** 这就需要一个「双方都能看见的地方」来交换这些描述，这就是**元数据服务（metadata service）**的角色。你可以把它理解成一座「布告栏」：

- 进程 A 启动后，把自己的内存描述**贴**到布告栏（注册 / publish）。
- 进程 B 想读 A 的数据时，先去布告栏**查** A 贴的描述，拿到地址和握手端口，再直接跟 A 建立数据通道。

数据本身**不走**布告栏，布告栏只交换「怎么找到你、你的内存长什么样」这类小信息。

**P2P 模式 vs 服务器模式**
上一讲用的 `P2PHANDSHAKE` 是一种特殊的「无布告栏」模式：没有第三方服务，发送端直接用 `host:端口` 这个名字去连接收端。代价就是你必须**提前知道对方的真实端口**（所以上一讲要 `get_rpc_port()` 抄端口）。本讲要学的 HTTP / etcd 模式则引入一座真正的布告栏，双方只要约定一个**名字**（segment 名），剩下的事服务器替你发现——**不再需要抄端口**。

**连接串（conn_string）就是开关**
引擎初始化时传的 `metadata_server` 参数，本质上是一个「连接串」。引擎看到 `P2PHANDSHAKE` 就走无服务器模式；看到 `http://...` 就走 HTTP 模式；看到 `etcd://...` 就走 etcd 模式。**一个字符串，决定整套行为**——这是本讲最重要的认知。

> 建议先完成 [u1-l4](u1-l4-python-te-quickstart.md)，确保你已经能在 P2P 模式下用两个进程跑通一次 `transfer_sync_read`。本讲会反复和 P2P 模式做对比。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [mooncake-transfer-engine/example/http-metadata-server-python/bootstrap_server.py](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/http-metadata-server-python/bootstrap_server.py) | 本讲的「布告栏」本体：一个用 aiohttp 写的极简 KV 服务器，暴露 `/metadata` 的 GET/PUT/DELETE。直接 `python bootstrap_server.py` 即可启动。 |
| [mooncake-transfer-engine/include/transfer_metadata_plugin.h](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_metadata_plugin.h) | 定义两个插件接口：`MetadataStoragePlugin`（布告栏的存取抽象）和 `HandShakePlugin`（两端之间的直接握手抽象）。 |
| [mooncake-transfer-engine/include/transfer_metadata.h](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_metadata.h) | `TransferMetadata` 类声明，定义 segment / buffer / rpc_meta 等数据结构，以及 `P2PHANDSHAKE` 这个魔法字符串。 |
| [mooncake-transfer-engine/src/transfer_metadata.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp) | `TransferMetadata` 的实现：构造函数里据连接串选模式、把 segment 描述和 rpc_meta 发布到存储插件、或（P2P 模式）跳过发布。 |
| [mooncake-transfer-engine/src/transfer_metadata_plugin.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata_plugin.cpp) | 三种存储插件的具体实现：`HTTPStoragePlugin`（用 libcurl）、`EtcdStoragePlugin`、`RedisStoragePlugin`，以及 `MetadataStoragePlugin::Create` 如何据前缀分发。 |
| [mooncake-transfer-engine/example/kvcache_prefix_bench.py](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/kvcache_prefix_bench.py) | 一个现成的双角色（target/initiator）基准脚本，`--metadata_server` 参数支持把默认的 `P2PHANDSHAKE` 换成任意服务器地址。是「跨进程传输」的官方模板。 |
| [mooncake-wheel/mooncake/http_metadata_server.py](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-wheel/mooncake/http_metadata_server.py) | pip 包内置的「增强版」元数据服务器，带命令行参数（`--port`/`--host`/`--log-level`），安装后可用 `mooncake_http_metadata_server` 命令启动。逻辑与 `bootstrap_server.py` 一致。 |
| [mooncake-transfer-engine/example/http-metadata-server/main.go](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/http-metadata-server/main.go) | 同一套 HTTP KV 协议的 Go（gin）实现，证明「布告栏」可以用任意语言写，只要遵守同一个接口。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 元数据服务器 bootstrap**（布告栏长什么样、它存什么）、**4.2 三种模式 P2P / HTTP / etcd**（引擎如何选模式）、**4.3 跨进程传输**（用 HTTP 模式跑通两端）。

### 4.1 元数据服务器 bootstrap

#### 4.1.1 概念说明

「元数据服务器」听起来唬人，其实就是**一个能在网络上按 key 存取小段 JSON 的服务**。它需要且仅需要三个操作：

- `set(key, value)`：存（注册时用）
- `get(key)`：取（发现对端时用）
- `remove(key)`：删（下线时用）

Mooncake 把这三个操作抽象成 C++ 接口 `MetadataStoragePlugin`。任何只要能实现「按 key 存取 JSON」的东西——HTTP 服务、etcd、Redis——都能当元数据服务器。本模块先看最朴素的 HTTP 实现：`bootstrap_server.py`。

#### 4.1.2 核心流程

`bootstrap_server.py` 的全部行为可以用一句话概括：**在 `:8080` 上跑一个 aiohttp 应用，把 `/metadata` 这个路径当成一个字典来用**。

```text
客户端                                  bootstrap_server.py
  │  PUT /metadata?key=mooncake/ram/X ──►  self.store["mooncake/ram/X"] = 请求体
  │  GET /metadata?key=mooncake/ram/X ──►  返回 self.store["mooncake/ram/X"]
  │  DELETE /metadata?key=mooncake/ram/X ─► del self.store["mooncake/ram/X"]
```

key 直接放在 URL 的查询参数 `?key=...` 里，value 就是 HTTP body（一段 JSON）。这正好对应 C++ 端 `HTTPStoragePlugin` 拼出来的 URL 形如 `http://host:port/metadata?key=<转义后的key>`。

> **术语：rpc_meta**。引擎在布告栏里会存两类 key：一类是 `mooncake/ram/<段名>`（内存描述：地址、长度、rkey…），另一类是 `mooncake/rpc_meta/<段名>`（握手定位：IP + rpc 端口）。`bootstrap_server.py` 里有一处针对 `rpc_meta` 的特殊处理——本讲实践环节会带你看到它。

#### 4.1.3 源码精读

**整个服务器类只有 ~100 行**。构造函数建一个 aiohttp 应用、一个普通 Python 字典 `self.store` 当存储、一把 `asyncio.Lock` 当并发保护，然后注册路由：

- [bootstrap_server.py:14-27](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/http-metadata-server-python/bootstrap_server.py#L14-L27)：`KVBootstrapServer.__init__` 与 `_setup_routes`，把所有方法都路由到 `/metadata`。注意存储就是 `self.store = dict()`，纯内存，重启即丢——这正是它「轻量」也「不持久」的特性。

**一个函数分发三种 HTTP 方法**：

- [bootstrap_server.py:29-39](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/http-metadata-server-python/bootstrap_server.py#L29-L39)：`_handle_metadata` 从查询参数取 `key`，按 `GET`/`PUT`/`DELETE` 分别派发。

**PUT 里的 rpc_meta 防重复逻辑**（本讲实践要观察的重点）：

- [bootstrap_server.py:50-58](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/http-metadata-server-python/bootstrap_server.py#L50-L58)：`_handle_put`。如果 key 里含 `rpc_meta` 且该 key 已存在，就返回 `400 Duplicate rpc_meta key not allowed`。含义是：**同一个段名的「握手定位」只允许注册一次**——防止多个进程抢同一个段名导致定位错乱。本讲末尾的实践会带你亲眼看到这条 400。

**GET / DELETE**：

- [bootstrap_server.py:41-48](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/http-metadata-server-python/bootstrap_server.py#L41-L48)：`_handle_get`，字典里没有就返回 `404 metadata not found`，有就把原始 body 原样返回。
- [bootstrap_server.py:60-67](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/http-metadata-server-python/bootstrap_server.py#L60-L67)：`_handle_delete`。

**启动入口**：脚本底部固定在 `:8080` 启动，主线程 `wait()` 永远阻塞，按 Ctrl+C 优雅关闭：

- [bootstrap_server.py:68-85](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/http-metadata-server-python/bootstrap_server.py#L68-L85)：`_run_server`，在一个后台线程里跑 asyncio 事件循环，`web.TCPSite(runner, port=self.port)` 监听端口。
- [bootstrap_server.py:98-104](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/http-metadata-server-python/bootstrap_server.py#L98-L104)：`__main__`，`KVBootstrapServer(port=8080)` 后 `threading.Event().wait()` 阻塞主线程。

> 这个服务器**只跑在一个进程里、只在内存里**，进程一退出所有元数据丢失。它适合开发与教学。生产环境要持久化、要高可用，就该换 etcd（见 4.2）。

#### 4.1.4 代码实践

**目标**：单独启动 `bootstrap_server.py`，用 `curl` 模拟引擎的「注册 / 查询 / 删除」三步，验证布告栏确实在按 key 存取 JSON。

**步骤**：

1. 装好 aiohttp（若上一讲已 `pip install mooncake-transfer-engine-non-cuda`，依赖里已含 `aiohttp`）：

   ```bash
   pip install aiohttp
   ```

2. 终端 1 启动服务器：

   ```bash
   cd mooncake-transfer-engine/example/http-metadata-server-python
   python bootstrap_server.py
   ```

3. 终端 2 用 `curl` 模拟一次「注册」（PUT 一段 JSON 到某个 key）：

   ```bash
   curl -s -X PUT \
     "http://127.0.0.1:8080/metadata?key=mooncake/ram/my-test-segment" \
     -H "Content-Type: application/json" \
     --data '{"addr":1234567890,"length":4096}'
   ```

4. 再用 `curl` 模拟「查询」（GET 同一个 key）：

   ```bash
   curl -s "http://127.0.0.1:8080/metadata?key=mooncake/ram/my-test-segment"
   ```

5. 最后模拟「删除」（DELETE）后再 GET 一次，确认变 404：

   ```bash
   curl -s -X DELETE "http://127.0.0.1:8080/metadata?key=mooncake/ram/my-test-segment"
   curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:8080/metadata?key=mooncake/ram/my-test-segment"
   ```

**需要观察的现象**：第 3 步 PUT 返回 `metadata updated`；第 4 步 GET 返回你刚 PUT 的那段 JSON；第 5 步 DELETE 后再 GET，HTTP 状态码应变成 `404`。

**预期结果**：布告栏的 set / get / remove 三个原语都正常工作，证明 `bootstrap_server.py` 就是一个合格的「按 key 存取 JSON」的服务。

> 若 PUT 报连接被拒绝：**待本地验证**。确认终端 1 的 `python bootstrap_server.py` 仍在前台运行、且 `:8080` 未被占用（`ss -ltnp | grep 8080`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `bootstrap_server.py` 的存储用普通 `dict` + 一把 `asyncio.Lock`，而不需要数据库？

> **答案**：元数据体积小、读写频繁但量不大，且引擎对单个 key 的读写是「读多写少 + 偶尔注册」。一个进程内字典足以，`asyncio.Lock` 保证并发 PUT/GET 不会撕裂字典。代价是没有持久化与多副本——这是它「轻量」的代价，也是生产环境换 etcd 的理由。

**练习 2**：`_handle_put` 里 `key.find("rpc_meta") != -1` 这个判断保护的是什么不变量？

> **答案**：保护「同一段名的握手定位（rpc_meta）全局唯一」。因为 `rpc_meta/<段名>` 存的是「这个段名对应哪个 IP/端口」，如果允许覆盖，第二个进程用同样的段名注册就会把指向改成自己，导致别的进程按这个名字找到错误的端点，所以宁可拒绝（返回 400）。

---

### 4.2 P2P / HTTP / etcd 三种模式

#### 4.2.1 概念说明

上一模块我们看到「布告栏」是一个 `MetadataStoragePlugin`。但 Mooncake 其实有**两种「交换元数据的方式」**：

1. **有布告栏（服务器模式）**：HTTP / etcd / Redis。两端都连同一个服务器，一端贴、一端取。
2. **无布告栏（P2P 模式）**：`P2PHANDSHAKE`。没有第三方服务，发起方**直接用对端的名字（host:port）去连**，握手时当面交换元数据。

引擎到底是哪一种，完全由你传给 `initialize` 的 `metadata_server`（连接串）决定。本模块讲清楚这个分发逻辑，以及三种模式各自的取舍。

#### 4.2.2 核心流程

**模式选择的总开关**在 `TransferMetadata` 构造函数里，逻辑只有几行：

```text
读取 conn_string（连接串）
├─ 先无条件创建 handshake_plugin_（SocketHandShakePlugin，两端直接握手用）
├─ if conn_string == "P2PHANDSHAKE":
│      p2p_handshake_mode_ = true
│      直接 return（不创建 storage_plugin_，什么都不发布到服务器）
└─ else:
       按 conn_string 的协议前缀创建 storage_plugin_：
         "etcd://"   → EtcdStoragePlugin
         "redis://"  → RedisStoragePlugin
         "http(s)://"→ HTTPStoragePlugin
```

关键结论：**P2P 模式根本不碰服务器**，所以它的 segment 描述、rpc_meta 都不会被 PUT 出去；服务器模式则要把这些信息写到服务器上。

**为什么 P2P 模式下你必须在终端之间抄端口？**
看 `getRpcMetaEntry`（拿到「对端的 IP + 握手端口」）在两种模式下的差别就能看懂：

```text
P2P 模式：      ip, port = parseHostNameWithPort(server_name)   # 直接从「名字字符串」里解析
HTTP/etcd 模式：rpcMetaJSON = storage_plugin_->get("mooncake/rpc_meta/" + server_name)
                ip   = rpcMetaJSON["ip_or_host_name"]            # 从服务器查回来
                port = rpcMetaJSON["rpc_port"]
```

- P2P 模式里，**名字本身就是地址**。所以名字必须是 `host:真实rpc端口`，发起方必须提前知道这个端口——这就是上一讲要 `get_rpc_port()` 抄端口的根因。
- HTTP/etcd 模式里，**名字只是一个不透明的 key**，真实的 IP 和端口是接收端启动时自己探测到并「贴」到服务器的（`rpc_meta` 条目），发起方去服务器查回来即可。**双方只要约定一个段名，不用互相通知端口。** 这正是本讲要掌握的核心优势。

#### 4.2.3 源码精读

**魔法字符串 `P2PHANDSHAKE` 的定义**：

- [transfer_metadata.h:41](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_metadata.h#L41)：`#define P2PHANDSHAKE "P2PHANDSHAKE"`。引擎就是拿连接串和它做字符串相等比较来判定模式的。

**模式分发（总开关）**——构造函数里：

- [transfer_metadata.cpp:130-165](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L130-L165)：`TransferMetadata` 构造函数。第 136-144 行读环境变量 `MC_METADATA_CLUSTER_ID` 作为可选的命名空间前缀；第 146-147 行算出 key 前缀 `common_key_prefix_ = "mooncake/"`、`rpc_meta_prefix_ = "mooncake/rpc_meta/"`；**第 149 行无条件创建 `handshake_plugin_`**；**第 155-158 行是判定 P2P 模式的关键**——`if (conn_string == P2PHANDSHAKE) { p2p_handshake_mode_ = true; return; }`，直接返回，跳过第 159 行的 `storage_plugin_` 创建。

**两类 key 的拼法**——决定了布告栏里 key 长什么样：

- [transfer_metadata.cpp:146-147](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L146-L147)：`common_key_prefix_` 与 `rpc_meta_prefix_` 的计算。
- [transfer_metadata.cpp:169-184](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L169-L184)：`getFullMetadataKey`，把段名拼成完整 key `mooncake/ram/<段名>`（段名不含 `/` 时）。

**服务器模式下，segment 描述如何「贴」到布告栏**：

- [transfer_metadata.cpp:466-485](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L466-L485)：`updateSegmentDesc`。**第 468-470 行：P2P 模式直接 `return 0`（啥也不发）**；否则第 478 行 `storage_plugin_->set(getFullMetadataKey(...), segmentJSON)`——这就是一次 HTTP PUT。

**rpc_meta 如何发布（含 P2P 分支）**：

- [transfer_metadata.cpp:1138-1172](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L1138-L1172)：`addRpcMetaEntry`。P2P 分支（第 1142-1162 行）只是注册若干回调并启动握手 daemon，**不写服务器**；服务器分支（第 1164-1171 行）把 `{ip_or_host_name, rpc_port}` 写到 `mooncake/rpc_meta/<段名>`。

**两种模式查 rpc_meta 的差别**（本模块最关键的一处对比）：

- [transfer_metadata.cpp:1212-1238](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L1212-L1238)：`getRpcMetaEntry`。第 1222-1225 行（P2P）`parseHostNameWithPort(server_name)` 直接从名字解析；第 1226-1234 行（服务器模式）`storage_plugin_->get(rpc_meta_prefix_ + server_name, ...)` 去服务器查。

**存储插件接口与按前缀分发**：

- [transfer_metadata_plugin.h:21-31](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_metadata_plugin.h#L21-L31)：`MetadataStoragePlugin` 接口，只有 `get`/`set`/`remove` 三个纯虚函数。任何后端实现这三个就能当布告栏。
- [transfer_metadata_plugin.cpp:525-542](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata_plugin.cpp#L525-L542)：`parseConnectionString`，把 `etcd://host:port` 拆成 `(协议, 域)`；没有 `://` 的默认当成 `etcd`。
- [transfer_metadata_plugin.cpp:544-596](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata_plugin.cpp#L544-L596)：`MetadataStoragePlugin::Create` 工厂。第 548-551 行 `etcd`→EtcdStoragePlugin、第 554-582 行 `redis`→RedisStoragePlugin、**第 585-589 行 `http`/`https`→HTTPStoragePlugin**。都匹配不上则第 592 行 `LOG(FATAL)` 终止。

**HTTP 存储插件如何变成 HTTP 请求**（用 libcurl）：

- [transfer_metadata_plugin.cpp:254-261](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata_plugin.cpp#L254-L261)：`encodeUrl`，把 `metadata_uri_`（你传的整个 http URL）拼上 `?key=<转义key>`。所以连接串必须是**完整的 `http://host:port/metadata`**，`/metadata` 这段路径不能少。
- [transfer_metadata_plugin.cpp:265-306](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata_plugin.cpp#L265-L306)：`HTTPStoragePlugin::get`（HTTP GET）。
- [transfer_metadata_plugin.cpp:308-353](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata_plugin.cpp#L308-L353)：`HTTPStoragePlugin::set`（HTTP PUT，body 为序列化的 JSON）。
- [transfer_metadata_plugin.cpp:356-389](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata_plugin.cpp#L356-L389)：`HTTPStoragePlugin::remove`（HTTP DELETE）。

**握手插件（两端直接 TCP 握手）始终存在**：

- [transfer_metadata_plugin.h:33-73](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/transfer_metadata_plugin.h#L33-L73)：`HandShakePlugin` 接口，负责两端建立连接后当面交换 QP/地址等细节。
- [transfer_metadata_plugin.cpp:1253-1256](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata_plugin.cpp#L1253-L1256)：`HandShakePlugin::Create` 无条件返回 `SocketHandShakePlugin`——无论哪种模式，两端最终都要直接握一次手。HTTP/etcd 模式下，服务器只负责「告诉发起方去哪握手」，握手本身仍是直连。

三种模式对照表：

| 模式 | 连接串 | 需启动外部服务 | 段名是否=地址 | 典型场景 |
|------|--------|---------------|--------------|----------|
| P2P 握手 | `P2PHANDSHAKE` | 否 | **是**（必须 `host:真实端口`） | 单机速览、两三台临时测试 |
| HTTP | `http://host:port/metadata` | 是（Python/Go 写的 KV 服务器） | 否（段名是不透明 key） | 开发、中小规模、想要零依赖 |
| etcd | `etcd://host:port` | 是（etcd 集群） | 否 | 生产、多副本、需要持久化与高可用 |

#### 4.2.4 代码实践

**目标**：用一个不启动任何外部服务的纯源码阅读实践，验证「模式选择」与「段名是否等于地址」的差别——通过对照阅读两段 `getRpcMetaEntry` 分支，回答「为什么 HTTP 模式不用抄端口」。

**步骤**：

1. 打开 [transfer_metadata.cpp:1212-1238](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L1212-L1238)，对比两个分支：
   - P2P 分支（1222-1225 行）调用 `parseHostNameWithPort(server_name)`，说明 IP 和端口是**从段名字符串里切出来的**。
   - 服务器分支（1226-1234 行）调用 `storage_plugin_->get(...)`，说明 IP 和端口是**从布告栏查回来的**。

2. 再打开 [transfer_metadata.cpp:1138-1172](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L1138-L1172) 的 `addRpcMetaEntry`，确认：服务器分支第 1167 行把 `{ip_or_host_name, rpc_port}` PUT 到 `mooncake/rpc_meta/<段名>`。这就是「接收端把自己的真实端口贴上布告栏」的那一步。

3. 得出结论并记录：P2P 模式里段名=地址，所以发起方必须知道真实端口；HTTP 模式里段名=key、真实端口由接收端自助发布，发起方查表即可。

**需要观察的现象**：两段代码用**同一个 `server_name`**，却通过完全不同的途径得到 `ip_or_host_name`/`rpc_port`——一个从字符串解析，一个从网络查。

**预期结果**：你能用自己的话讲出「为什么换上 HTTP 元数据服务器后，两个终端之间再也不用抄 `get_rpc_port()` 的端口」。

> 本实践是纯阅读型，无需运行。结论会在 4.3 的动手实践中被「真机验证」。

#### 4.2.5 小练习与答案

**练习 1**：假设我把连接串写成 `http://127.0.0.1:8080`（漏掉了 `/metadata`），会发生什么？

> **答案**：`HTTPStoragePlugin` 会把它当作 `metadata_uri_`，`encodeUrl`（[transfer_metadata_plugin.cpp:254-261](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata_plugin.cpp#L254-L261)）拼出的 URL 是 `http://127.0.0.1:8080?key=...`，而 `bootstrap_server.py` 只在 `/metadata` 路径上注册了路由，于是 PUT/GET 命中根路径，大概率返回 404/405，引擎报 `Failed to register segment descriptor`。连接串必须带 `/metadata`。

**练习 2**：P2P 模式下，引擎会不会向任何服务器发 PUT？为什么？

> **答案**：不会。`updateSegmentDesc` 与 `addRpcMetaEntry` 在 P2P 分支都提前 `return`（见 [transfer_metadata.cpp:468-470](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L468-L470) 与 [1142-1162](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L1142-L1162)），甚至根本没创建 `storage_plugin_`（构造函数第 155-158 行直接 return）。元数据全部在两端握手时当面交换。

---

### 4.3 跨进程传输（HTTP 元数据模式）

#### 4.3.1 概念说明

有了布告栏（4.1）和模式选择（4.2），现在把整条链路跑通：**两个独立进程，借助一个 HTTP 元数据服务器，完成一次真正的跨进程 buffer 传输**。

注意「跨进程」在本讲的含义：target 进程注册一段内存并写入数据；initiator 进程通过元数据服务器发现 target、建立连接、把数据 `transfer_sync_read` 过来——全程 target 和 initiator 是两个互不感知的进程，它们唯一的「共享」就是那台元数据服务器。这正是一个 put/get 式的远程数据搬运：target「放（put）」好数据，initiator「取（get）」走数据。

#### 4.3.2 核心流程

```text
终端 1: bootstrap_server.py          （布告栏，:8080，全程常驻）

终端 2: target（放数据）
  1. engine.initialize("127.0.0.1:19001", "http://127.0.0.1:8080/metadata", "tcp", "")
  2. engine.register_memory(addr, SIZE)        # 内部 PUT mooncake/ram/127.0.0.1:19001 + PUT mooncake/rpc_meta/127.0.0.1:19001
  3. write_bytes_to_buffer(addr, b"HELLO...")  # 在本机内存里写好待发送内容
  4. 等待 initiator（time.sleep）

终端 3: initiator（取数据）
  1. engine.initialize("127.0.0.1:19002", "http://127.0.0.1:8080/metadata", "tcp", "")
  2. engine.register_memory(recv, SIZE)        # 注册自己的接收缓冲区
  3. remote = engine.get_first_buffer_address("127.0.0.1:19001")
        ↑ 先 GET mooncake/ram/127.0.0.1:19001（内存描述）+ GET mooncake/rpc_meta/...（握手端口）
        ↑ 再直连 target 握手，拿到对端第一块 buffer 地址
  4. engine.transfer_sync_read("127.0.0.1:19001", recv, remote, 15)  # 把数据读过来
  5. read_bytes_from_buffer(recv, 15) 校验 == b"HELLO..."
```

对比上一讲 P2P 模式的两个关键变化：

- 两端的 `metadata_server` 都填**同一个 HTTP URL**（不再是 `P2PHANDSHAKE`）。
- initiator 调 `get_first_buffer_address` 用的名字就是 target 的 `local_hostname`（这里是 `127.0.0.1:19001`）。**这个名字只是布告栏里的一个 key，它里面的 `19001` 不需要是真实端口**——真实端口 target 已经自己探测并贴上去了。所以你**不需要**像上一讲那样抄 `get_rpc_port()` 的结果。

#### 4.3.3 源码精读

**官方模板 `kvcache_prefix_bench.py` 已经支持任意元数据服务器**——它默认 P2P，但留了开关：

- [kvcache_prefix_bench.py:58-62](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/kvcache_prefix_bench.py#L58-L62)：`--metadata_server` 参数，默认 `P2PHANDSHAKE`。把它换成 `http://127.0.0.1:8080/metadata` 即可走本讲的 HTTP 模式。
- [kvcache_prefix_bench.py:182-187](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/kvcache_prefix_bench.py#L182-L187)：target 端 `engine.initialize(local_server_name, args.metadata_server, args.protocol, "")`——注意第二参数 `args.metadata_server` 直接透传连接串。

- [kvcache_prefix_bench.py:248-253](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/kvcache_prefix_bench.py#L248-L253)：initiator 端同样透传 `metadata_server`。注意第 255-259 行有一段 `if args.metadata_server == "P2PHANDSHAKE": ... get_rpc_port()`——**这段只在 P2P 模式下才需要**拼真实名字；HTTP 模式下这段被跳过，直接用原始 `local_server_name`，正印证了「服务器模式不用抄端口」。
- [kvcache_prefix_bench.py:276](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/kvcache_prefix_bench.py#L276)：`remote_addr = engine.get_first_buffer_address(args.target_server_name)`——target 的段名作为 key 去布告栏查。
- [kvcache_prefix_bench.py:290-292](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/kvcache_prefix_bench.py#L290-L292)：连接 warmup 用的 `engine.transfer_sync_read(...)`。

**Python `TransferEngine.initialize` 把连接串一路传到底**（与上一讲一致，这里只点出处）：

- [mooncake-integration/transfer_engine/transfer_engine_py.cpp:168-177](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L168-L177)：`initialize(local_hostname, metadata_server, protocol, device_name)`，`metadata_server` 即连接串，最终传给 `TransferMetadata(conn_string)`。

#### 4.3.4 代码实践

**目标**：在**三个终端**里完成一次「target 放数据 → initiator 取数据」的跨进程传输，校验数据一致，并用 `curl` 观察元数据服务器收到的注册请求（segment 描述与 rpc_meta）。

> 前提：已按 [u1-l4](u1-l4-python-te-quickstart.md) `pip install` 好匹配本机的 Mooncake 包，并能 `from mooncake.engine import TransferEngine`。本机无 RDMA 时务必用 `protocol="tcp"`。

**步骤**：

1. **终端 1：启动元数据服务器**（常驻，不要关）。

   ```bash
   cd mooncake-transfer-engine/example/http-metadata-server-python
   python bootstrap_server.py
   ```

   看到「Server error」以外的安静运行即正常（它没有启动横幅）。用 `ss -ltnp | grep 8080` 确认已监听。

2. 把下面这段**示例代码**存为 `http_transfer.py`（与上一讲 `mini_transfer.py` 同构，只是把 `P2PHANDSHAKE` 换成了 HTTP 服务器，并去掉了抄端口逻辑）：

   ```python
   # http_transfer.py —— 示例代码（单机 TCP + HTTP 元数据服务器）
   import sys, mmap, ctypes, time

   META = "http://127.0.0.1:8080/metadata"
   SIZE = 4096

   def alloc(size):
       m = mmap.mmap(-1, size, access=mmap.ACCESS_WRITE)
       addr = ctypes.addressof(ctypes.c_char.from_buffer(m))
       return m, addr

   def main():
       # argv: mode target_name | initiator target_name
       mode = sys.argv[1]                 # "target" 或 "initiator"
       my_name = sys.argv[2]              # 本端段名（HTTP 模式下只是布告栏里的 key）
       from mooncake.engine import TransferEngine
       engine = TransferEngine()
       assert engine.initialize(f"127.0.0.1:{my_name.split(':')[-1]}", META, "tcp", "") == 0, "init failed"
       # 说明：上面把段名也写成 host:port 形式，仅为了与官方脚本习惯一致；
       #       HTTP 模式下这个端口不需要是真实 RPC 端口，服务器会自动发布真实端口。

       if mode == "target":
           m, addr = alloc(SIZE)
           assert engine.register_memory(addr, SIZE) == 0          # 触发 PUT mooncake/ram/... + rpc_meta/...
           engine.write_bytes_to_buffer(addr, b"HELLO_FROM_TARGET!", 17)
           print(f"[target] segment={my_name} buf={addr:#x}, waiting 120s for initiator...")
           time.sleep(120)                                          # 等待 initiator 来取

       elif mode == "initiator":
           target_name = sys.argv[3]
           m, recv = alloc(SIZE)
           assert engine.register_memory(recv, SIZE) == 0
           remote = engine.get_first_buffer_address(target_name)   # GET 布告栏发现 target
           assert remote != 0, f"无法发现 {target_name}，确认 target 已启动并指向同一个 META"
           ret = engine.transfer_sync_read(target_name, recv, remote, 17)
           data = engine.read_bytes_from_buffer(recv, 17)
           print(f"[initiator] transfer_sync_read -> {ret} (0=成功)")
           print(f"[initiator] received -> {data!r}")
           assert data == b"HELLO_FROM_TARGET!", "数据不一致！"

   main()
   ```

   > 上面的 `my_name` 用作「段名」。为与官方脚本一致写成 `host:port`，但请记住：HTTP 模式下这里的端口**不是**真实 RPC 端口，target 的真实端口由引擎自行探测并写入 `rpc_meta`，initiator 通过布告栏查到。

3. **终端 2：启动 target（放数据）**。

   ```bash
   python http_transfer.py target 19001
   ```

   预期看到 `[target] segment=... waiting ...`。

4. **观察注册请求**（在另开一个终端，趁 target 运行时）：用 `curl` 直接查看布告栏里 target「贴」上去的两类 key。

   ```bash
   # (a) 段内存描述：地址 / 长度
   curl -s "http://127.0.0.1:8080/metadata?key=mooncake/ram/127.0.0.1:19001"
   echo
   # (b) 握手定位：真实 IP + 真实 RPC 端口（这才是引擎自动探测到的端口）
   curl -s "http://127.0.0.1:8080/metadata?key=mooncake/rpc_meta/127.0.0.1:19001"
   echo
   ```

   预期 (a) 返回一段含 `buffers`（`addr`、`length`）的 JSON；预期 (b) 返回 `{"ip_or_host_name":"127.0.0.1","rpc_port":<某个随机端口>}`。注意 (b) 里的 `rpc_port` 跟你命令行写的 `19001` **不一样**——这正证明「真实端口是引擎自己发到服务器的，不是你传的名字里的端口」。

5. **终端 3：启动 initiator（取数据）**。

   ```bash
   python http_transfer.py initiator 19002 127.0.0.1:19001
   ```

6. **（进阶）观察 rpc_meta 的 400 防重复**：保持上面 target 还在运行，再开一个终端，**用同一个段名再注册一个 target**——

   ```bash
   python http_transfer.py target 19001
   ```

   你应看到 `register_memory` 返回非 0、或引擎日志里出现 `Failed to ... rpc_meta`。再去 `curl` 那个 rpc_meta key 不变。这是因为布告栏拒绝了重复的 `rpc_meta`（见 [bootstrap_server.py:50-58](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/http-metadata-server-python/bootstrap_server.py#L50-L58)）。

**需要观察的现象**：
- 终端 2（target）打印 `[target] ... waiting ...`。
- 第 4 步的 `curl` (a)(b) 各返回一段 JSON，(b) 的 `rpc_port` 与命令行的 `19001` 不同。
- 终端 3（initiator）打印 `transfer_sync_read -> 0` 且 `received -> b'HELLO_FROM_TARGET!'`。
- 第 6 步重复注册被拒（现象为注册失败或 rpc_meta 不被覆盖）。

**预期结果**：initiator 成功把 target 写入的 17 字节读过来并校验一致；布告栏里能看到 target 注册时 PUT 的 `mooncake/ram/...` 与 `mooncake/rpc_meta/...`。**全程两个数据进程之间没有手工交换过任何端口**——这正是元数据服务器的价值。

> 若 `get_first_buffer_address` 返回 0：**待本地验证**。按顺序排查——(1) bootstrap_server 是否仍在 :8080 运行；(2) 两端 `META` 是否完全相同；(3) target 是否确实进入 `waiting`（已执行到 `register_memory` 之后）。若 `transfer_sync_read` 返回非 0，常见是 target 已 `sleep` 到期退出，或本机防火墙挡了回环端口。

> **进阶提示（源码阅读型，可选）**：想直接看到「服务器每次收到请求」的日志，可把仓库里的 `bootstrap_server.py` **拷贝一份**（例如 `my_bootstrap.py`，不要改原文件），在 `_handle_metadata` 开头加一行 `print(request.method, key)` 再运行。对比 curl 与两端引擎产生的 PUT/GET 序列，能直观看到「注册时 2 次 PUT、发现时 2 次 GET」的调用链。

#### 4.3.5 小练习与答案

**练习 1**：在本讲的实践里，initiator 命令行写的是 `127.0.0.1:19002`，target 命令行写的是 `127.0.0.1:19001`。这两个数字真的被当作网络端口用了吗？

> **答案**：没有。HTTP 模式下，段名只是布告栏里的 key（见 [getRpcMetaEntry](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata.cpp#L1212-L1238) 的非 P2P 分支）。target 的真实握手端口由引擎自己探测并写入 `mooncake/rpc_meta/<段名>`（见第 4 步 curl 的 (b)）。这两个 `19001/19002` 仅为可读性写成 host:port，引擎并不据此建 socket。

**练习 2**：如果你故意把 initiator 的 `META` 写成 `http://127.0.0.1:9999/metadata`（一个没人监听的端口），会在哪一步、以什么形式失败？

> **答案**：在 `register_memory`（initiator 自己注册时也要 PUT 自己的 segment/rpc_meta）或 `get_first_buffer_address`（GET target）时失败。`HTTPStoragePlugin::set/get` 在 curl 失败时返回 `false`（见 [transfer_metadata_plugin.cpp:265-306](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata_plugin.cpp#L265-L306)），上层报 `Failed to register segment descriptor` / `Failed to find location of ...`。所以「两端必须指向同一个活着的服务器」是硬性前提。

**练习 3**：为什么 `bootstrap_server.py` 把 rpc_meta 的重复 PUT 直接拒掉（400），而普通 ram 段描述允许覆盖？

> **答案**：`ram/<段名>` 描述的是「这个段当前注册的内存」，进程重启或重新注册时理应刷新成最新值，所以允许覆盖；`rpc_meta/<段名>` 描述的是「这个段名指向哪个进程的 IP/端口」，它必须是全局唯一的身份——两个不同进程抢同一个段名会导致定位混乱，所以宁可拒绝（见 [bootstrap_server.py:50-58](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/http-metadata-server-python/bootstrap_server.py#L50-L58)）。

---

## 5. 综合实践

把三个模块串起来，做一个「三终端 put/get 数据搬运」的完整演示，并把观察到的现象画成一张时序。

**任务**：用本讲的 `bootstrap_server.py` + 自写 `http_transfer.py`，完成一次「target 放一段较长的数据 → initiator 取走并校验」，并产出一份「布告栏调用时序」。

**步骤**：

1. 启动 `bootstrap_server.py`（终端 1）。
2. 改造 `http_transfer.py` 的 target：把写入内容从 17 字节换成一段可识别的模式串，例如 4096 字节、每 16 字节为 `"MOONCAKE_%04d"`（可用 `struct` 或简单字符串拼接生成）。
3. 启动 target（终端 2），记下它打印的段名。
4. 用 `curl` 抓下 `mooncake/ram/<段名>` 与 `mooncake/rpc_meta/<段名>` 两个 key 的内容，**截图或复制保存**。
5. 启动 initiator（终端 3），让它 `transfer_sync_read` 全部 4096 字节，并逐 16 字节比对，打印「匹配 / 不匹配块数」。
6. 画一张时序图：标注「target PUT ram」「target PUT rpc_meta」「initiator GET ram」「initiator GET rpc_meta」「两端直连握手」「transfer_sync_read 数据流」这六件事的先后顺序。

**验收标准**：
- initiator 报「全部 4096 字节匹配」。
- 你能讲清楚：前 4 步是「经布告栏的元数据交换」，第 5 步的握手与数据流是「两端直连」——**数据本身不经过元数据服务器**。
- 时序图中 `rpc_meta` 的端口 ≠ 你命令行写的端口，证明「真实端口由引擎自助发布」。

**延伸思考**：把 `META` 换成 `etcd://127.0.0.1:2379`（先在本机起一个 etcd），重复实验。预期行为完全一致，只是布告栏从「内存字典」换成了「etcd」。这能帮你理解 `MetadataStoragePlugin` 抽象的意义——后端可换，上层不变。

## 6. 本讲小结

- 元数据服务是两端之间的「布告栏」：负责交换 segment 内存描述（`mooncake/ram/<段名>`）和握手定位（`mooncake/rpc_meta/<段名>`），**数据本身不走它**。
- `bootstrap_server.py` 是一个 ~100 行的 aiohttp KV 服务器，靠 `/metadata` 的 GET/PUT/DELETE 就能满足引擎全部元数据需求；它对 `rpc_meta` 做了「同段名唯一」的防重复保护。
- 三种后端由**连接串**决定：`P2PHANDSHAKE`（无服务器）、`http://host:port/metadata`（HTTP）、`etcd://host:port`（etcd）；`TransferMetadata` 构造函数和 `MetadataStoragePlugin::Create` 是两个分发点。
- P2P 模式下「段名=地址」，必须手工抄 `get_rpc_port()`；HTTP/etcd 模式下「段名=不透明 key」，真实端口由接收端自助发布到布告栏、发起端查表得到——**两个数据进程之间无需交换端口**。
- 无论哪种模式，两端最终都要用 `SocketHandShakePlugin` 直连握一次手；服务器只解决「去哪握手」的发现问题。
- 跨进程传输的三步走不变：`initialize`（指向同一台元数据服务器）→ `register_memory` → `get_first_buffer_address` + `transfer_sync_read`。

## 7. 下一步学习建议

- **进入 Store**：TransferEngine 是底层搬运工，之上的 `MooncakeDistributedStore`（`from mooncake.store import MooncakeDistributedStore`）提供 KV cache 语义（put/get/replicate），底层同样用这台元数据服务器。建议进入 Store 相关讲义，学习 `register_buffer` 与 PyTorch tensor 的零拷贝。
- **换 etcd 后端**：本讲用了零依赖的 HTTP 服务器。生产环境通常用 etcd。可在本机起一个 etcd，把连接串改成 `etcd://127.0.0.1:2379` 重跑本讲示例，体会 `MetadataStoragePlugin` 抽象带来的「后端可替换」。
- **阅读握手细节**：本讲把 `SocketHandShakePlugin` 当黑盒。后续可精读 [transfer_metadata_plugin.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/src/transfer_metadata_plugin.cpp) 里 `SocketHandShakePlugin::startDaemon`（第 656 行起）与 `doConnect`（第 942 行起），理解两端直连时 QP/地址等细节是如何在一条 TCP 连接里交换的。
- **Go 版元数据服务器**：若你的部署偏好单二进制，可阅读 [http-metadata-server/main.go](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/http-metadata-server/main.go)，它用 gin 实现了与 Python 版完全相同的 KV 协议，证明「布告栏」可用任意语言实现。
