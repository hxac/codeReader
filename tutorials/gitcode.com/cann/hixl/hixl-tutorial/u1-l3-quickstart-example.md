# 第一个 HIXL 程序：quickstart 样例精读

## 1. 本讲目标

学完本讲，你应该能够：

1. 理解 `hixl_example_quickstart` 的 **server/client 双进程模型**：两个独立进程各自持有 `Hixl` 引擎，client 是唯一发起传输的一方，server 全程"被动"。
2. 掌握 **HIXL C++ API 的典型调用序列**：`Initialize` → `RegisterMem` → 地址交换 → `Connect` → `TransferSync(READ)` → 数据校验 → `Disconnect` → `Finalize`。
3. 理解 `local_engine` / `remote_engine` 标识的格式与含义（本例中均为 `ip:port` 形式）。
4. 会按官方文档启动样例，并掌握两类最常见的失败排查思路（device 不互通、TLS 配置不一致）。

## 2. 前置知识

阅读本讲前，建议你先建立以下几个直觉（上一讲 `u1-l2` 已讲解构建方式，此处只回顾概念）：

- **单边通信（one-sided communication）**：传输只由一端（client）发起。client 直接 READ 远端 server 的显存，server 进程不参与数据搬运，甚至可以在传输期间继续做计算。这是 HIXL 区别于 socket/send-recv 类双边通信库的核心。
- **注册内存（memory registration）**：要让通信链路能直接访问一块显存/内存，必须先把它"注册"给 HIXL 引擎。注册后引擎拿到 `MemHandle`，底层链路（HCCS/RDMA）才能对这块内存做零拷贝读写。
- **ACL（Ascend Computing Language）**：昇腾芯片的基础运行时库。样例里用 `aclrtSetDevice`/`aclrtMalloc`/`aclrtMemcpy` 完成选卡、申请显存、主机与设备间拷贝——这些都不是 HIXL 的接口，而是昇腾通用运行时接口。
- **engine 标识**：HIXL 中每个 `Hixl` 实例用一个字符串标识自己，例如 `127.0.0.1:16001`。对端通过这个字符串"指名道姓"地建链。
- **READ 与 WRITE**：HIXL 只有两个传输方向枚举。`READ` 表示"把远端的数据读到我本地"，`WRITE` 表示"把我本地的数据写到远端"。本样例使用 `READ`。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [examples/cpp/hixl_example_quickstart.cpp](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_quickstart.cpp) | 本讲主角：约 230 行的最小 HIXL 端到端样例，server/client 双角色共用一个文件 |
| [docs/zh/quick_start.md](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/quick_start.md) | 官方快速开始文档：环境前提、启动命令、成功标志、默认参数 |
| [examples/run_example.sh](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/run_example.sh) | 样例批量冒烟脚本：`run_pair` 同时拉起成对进程并检查输出中的 ERROR |
| [examples/README.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/README.md) | 样例总说明：device 连通性检查与 TLS 一致性检查 |
| [include/hixl/hixl.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h) | `Hixl` 公开类声明，本讲涉及其中 6 个接口 |
| [include/hixl/hixl_types.h](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h) | `MemDesc`/`TransferOpDesc`/`MemType`/`TransferOp` 等基础数据结构 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- 4.1 server/client 双进程模型
- 4.2 HIXL C++ API 调用序列
- 4.3 样例启动方式与常见失败排查

### 4.1 server/client 双进程模型

#### 4.1.1 概念说明

quickstart 是一个"一份源码、两种角色"的程序：同一个可执行文件通过命令行参数 `--role=server` 或 `--role=client` 决定走哪条路径。两个进程各自：

- 占用一张昇腾卡（client 用 device 0，server 用 device 2）；
- 各自创建一个 `Hixl` 引擎实例并用不同的 engine 字符串标识；
- 各自申请 1MB 显存并注册。

关键在于**角色不对称**：

- **server**：只做三件事——填充数据、注册内存、把本端显存地址告诉 client。它从不调用 `Connect`，也不发起任何传输。
- **client**：负责建链、发起 `READ` 传输、校验数据、断链。整条 HIXL 传输链路上只有 client 在"说话"。

为什么 client 必须先知道 server 的显存地址才能 READ？因为单边 READ 的语义是"从远端的某个地址读 N 字节到我本地"，这个"远端地址"必须由 server 侧显式告知。样例用一个普通 TCP socket 来交换这个地址——注意这个 socket 只传 8 字节的指针值，**不传任何业务数据**，业务数据走的是 HIXL 的 HCCS 链路。这也演示了一个重要的工程事实：HIXL 只定义了传输原语，"地址如何协商"属于应用层协议，需要用户自己设计（生产环境通常用 zookeeper、rpc 或框架的集群管理来完成）。

#### 4.1.2 核心流程

两个进程的时序关系如下（先启动 server，再启动 client）：

```
server 进程                                client 进程
──────────                                ──────────
aclrtSetDevice(2)
Hixl::Initialize("127.0.0.1:16001")
aclrtMalloc 1MB 显存
填充 0x5A
RegisterMem(desc, MEM_DEVICE)
                                          aclrtSetDevice(0)
                                          Hixl::Initialize("127.0.0.1:16000")
socket: accept ────────────────────────── socket: connect (TCP 17001 端口)
send(本端显存地址 8 字节) ──────────────── recv(得到 server 显存地址)
                                          aclrtMalloc 1MB 显存
                                          RegisterMem(desc, MEM_DEVICE)
                                          Connect("127.0.0.1:16001")   ← HIXL 建链
                                          TransferSync("127.0.0.1:16001",
                                                       READ, {op})      ← 单边读
                                          aclrtMemcpy 拷回 host
                                          memcmp 校验 = 0x5A
recv("d" 完成信号) ◀───────────────────── send("d")
                                          Disconnect
                                          DeregisterMem / Finalize
DeregisterMem / Finalize
```

注意一个细节：server 侧的顺序是**先 RegisterMem、再发送地址**。源码注释明确解释了原因——"避免未注册地址被 client 提前使用"。如果 client 在 server 注册完成前就发起 READ，远端地址尚未登记进引擎，传输会失败。

#### 4.1.3 源码精读

**① 角色与常量定义**

[examples/cpp/hixl_example_quickstart.cpp:L26-L33](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_quickstart.cpp#L26-L33)

```cpp
constexpr int32_t kDeviceClient = 0;
constexpr int32_t kDeviceServer = 2;
constexpr const char *kClientEngine = "127.0.0.1:16000";
constexpr const char *kServerEngine = "127.0.0.1:16001";
constexpr int32_t kSocketPort = 17001;
constexpr int32_t kTimeoutMs = 5000;
constexpr size_t kBufSize = 1024 * 1024;
constexpr uint8_t kFillValue = 0x5A;
```

这段定义了全部默认参数：client 用 device 0 / engine 端口 16000，server 用 device 2 / engine 端口 16001，TCP 地址交换用端口 17001，传输缓冲 1MB，server 侧填充字节 0x5A。两个 engine 端口必须不同，否则两个引擎会端口冲突。

**② 角色解析入口**

[examples/cpp/hixl_example_quickstart.cpp:L201-L228](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_quickstart.cpp#L201-L228)

```cpp
if (arg == "--role=client") {
  is_client = true;  has_role = true;
} else if (arg == "--role=server") {
  is_client = false; has_role = true;
} else { /* 打印 Usage 并退出 */ }
...
if (is_client) { RunClient(); } else { RunServer(); }
```

`main` 只做一件事：解析 `--role` 参数，然后分派到 `RunClient` 或 `RunServer`。参数缺失或拼写错误会直接打印 Usage 返回。

**③ TCP 地址交换**

[examples/cpp/hixl_example_quickstart.cpp:L81-L109](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_quickstart.cpp#L81-L109)

```cpp
void ExchangeAddr(bool is_client, void *local_buf, uintptr_t &remote_addr, int &fd) {
  if (is_client) {
    // connect 到 server 的 17001 端口，recv 8 字节的远端显存地址
    recv(fd, &remote_addr, sizeof(remote_addr), 0);
  } else {
    // bind + listen + accept，然后把本端显存地址 send 给 client
    uintptr_t local_addr = reinterpret_cast<uintptr_t>(local_buf);
    send(fd, &local_addr, sizeof(local_addr), 0);
  }
}
```

client 分支 `connect` + `recv`，server 分支 `bind`/`listen`/`accept` + `send`。整个函数只是传输一个 `uintptr_t`——这就是"应用层自己协商远端地址"的最小实现。

**④ server 主流程**

[examples/cpp/hixl_example_quickstart.cpp:L174-L198](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_quickstart.cpp#L174-L198)

```cpp
void RunServer() {
  ...
  InitEngine(ctx, kServerEngine);
  // 申请、填充并注册本地内存，再经 socket 交换地址
  ACL_EXIT_ON_FAILURE(aclrtMalloc(&ctx.buf, kBufSize, ACL_MEM_MALLOC_HUGE_ONLY));
  ...
  std::vector<uint8_t> fill(kBufSize, kFillValue);
  ACL_EXIT_ON_FAILURE(aclrtMemcpy(ctx.buf, kBufSize, fill.data(), kBufSize, ACL_MEMCPY_HOST_TO_DEVICE));
  HixlExitOnFailure(ctx.engine.RegisterMem(ctx.desc, MEM_DEVICE, ctx.handle), "RegisterMem");
  ...
  ExchangeAddr(false, ctx.buf, remote_addr, ctx.fd);
  char dummy = 0;
  HixlExitOnFailure(recv(ctx.fd, &dummy, 1, 0) == 1, "recv done signal failed");
  Finalize(ctx);
}
```

server 在 host 侧构造 1MB 的 `0x5A` 向量，用 `aclrtMemcpy`（HOST_TO_DEVICE 方向）灌进显存，注册后发地址，然后阻塞在一个 1 字节的 `recv` 上等待 client 的"done"信号——这就是 server 在整个样例中的全部工作：它甚至不知道数据何时被读走，只等 client 说"我读完了"。

#### 4.1.4 代码实践

**实践 A：验证 socket 通道只传地址、不传数据**

1. **实践目标**：确认 TCP 交换通道与 HIXL 传输通道是两条独立路径，理解"控制面（socket）与数据面（HCCS）分离"。
2. **操作步骤**：
   - 阅读源码中 `ExchangeAddr` 的两个分支（无需运行环境，纯源码阅读型实践）。
   - 统计两个分支各自通过 TCP socket 发送/接收的字节数：client 侧 `recv` 一次收 `sizeof(uintptr_t)`（8 字节），随后 `send(fd, "d", 1, 0)` 发 1 字节；server 侧对称。
   - 对比 `kBufSize`（1MB）业务数据的去向：它只经过 `TransferSync` 走 HIXL 链路。
3. **需要观察的现象**：TCP 通道上总流量 ≤ 9 字节/方向，而 1MB 数据从不经过 socket。
4. **预期结果**：得出结论——socket 仅承担"地址协商 + 完成同步"两个控制语义；1MB 数据全走 HIXL 引擎的 `hccs:device` 链路。
5. 本实践为源码阅读型，无需昇腾硬件。

**实践 B（需硬件）：双进程启动观察角色不对称**

1. **实践目标**：亲眼确认 server 进程日志里不会出现 `Connect`/`TransferSync` 字样。
2. **操作步骤**：按 4.3 节的命令分别在两个终端启动 server 和 client。
3. **需要观察的现象**：server 终端依次输出 `Server waiting on port 17001...`、`RegisterMem success`、`Sent local addr: ...`、`Server got done signal`、`Finalize done`；全程无任何传输日志。
4. **预期结果**：server 日志证明它是"被动方"。若无昇腾硬件，此实践标注「待本地验证」，可用源码 `printf` 顺序推演替代。

#### 4.1.5 小练习与答案

**练习 1**：如果把两个终端都以 `--role=server` 启动，会发生什么？

**参考答案**：两个进程都会去 `bind` 17001 端口。第二个进程 `bind` 失败（`SO_REUSEADDR` 不能让两个进程绑定同一端口同时 `listen`），样例虽未检查 `bind` 返回值，但会在 `accept` 处阻塞或失败；同时也没有任何进程扮演 client，永远不会有 HIXL 传输发生。这印证了"成对启动"是样例的硬性前提。

**练习 2**：为什么 client 的 `ExchangeAddr` 在 `RegisterMem` **之前**调用，而 server 在**之后**调用？（见 `RunClient` L157-L159 与 `RunServer` L180-L191 的顺序）

**参考答案**：对 server 来说，发给 client 的地址必须是"已注册"的地址，否则 client 拿到地址立即 READ 会访问未登记内存，所以 server 必须先 `RegisterMem` 再发地址。对 client 来说，它收到的只是远端地址，与本端内存何时注册无关；本端 `RegisterMem` 只需在 `TransferSync` 之前完成即可。这个顺序差异是初学者最容易搞错的点。

**练习 3**：样例里 server 发送完地址后为什么还要 `recv` 一个 "d" 字符才退出？

**参考答案**：这是最简单的同步手段——保证 server 的引擎和注册内存在 client 传输期间一直存活。若 server 发完地址立刻 `Finalize` 并退出，client 的 `READ` 可能还没开始，远端引擎已销毁，传输必然失败。1 字节信号充当了"client 已完成传输"的释放许可。

### 4.2 HIXL C++ API 调用序列

#### 4.2.1 概念说明

HIXL Engine 对外的 C++ API 极简（这正是第一讲所说的"仅 10 余个核心调用"）。本样例用到其中 6 个，覆盖了五组接口中的四组：

| 接口 | 所属分组 | 本样例中的用途 |
|---|---|---|
| `Initialize` | 生命周期 | 创建引擎并绑定本端 engine 标识 |
| `Finalize` | 生命周期 | 销毁引擎、释放通信资源 |
| `RegisterMem` / `DeregisterMem` | 内存 | 把显存登记进引擎 / 解除登记 |
| `Connect` / `Disconnect` | 链路 | 与远端引擎建链 / 断链 |
| `TransferSync` | 传输 | 同步执行一批传输描述（本例为一条 READ） |

涉及的基础数据结构都定义在 `hixl_types.h` 中：

- [include/hixl/hixl_types.h:L59-L61](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h#L59-L61)：`enum MemType { MEM_DEVICE, MEM_HOST };` 与 `enum TransferOp { READ, WRITE };`——内存类型与传输方向是两个正交维度。
- [include/hixl/hixl_types.h:L63-L66](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h#L63-L66)：`MemDesc { uintptr_t addr; size_t len; }`——描述待注册的一块内存。
- [include/hixl/hixl_types.h:L69-L72](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h#L69-L72)：`TransferOpDesc { uintptr_t local_addr; uintptr_t remote_addr; size_t len; }`——一次传输的三要素：本端地址、远端地址、长度。注意 `READ` 与 `WRITE` 语义下 `local_addr`/`remote_addr` 的含义不变，变的是数据流向。

所有接口的返回值都是 `Status`，成功为 `SUCCESS`（0），失败为非 0 错误码，可通过 `aclGetRecentErrMsg()` 拿到最近一次错误描述（样例 `GetRecentErrMsg` 封装了它，见 [examples/cpp/hixl_example_quickstart.cpp:L35-L38](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_quickstart.cpp#L35-L38)）。

#### 4.2.2 核心流程

client 侧的 API 调用序列（伪代码）：

```
opts["GlobalResourceConfig"] = {"comm_resource_config.protocol_desc": ["hccs:device"]}
engine.Initialize("127.0.0.1:16000", opts)          # 1. 初始化，声明使用 HCCS 链路
buf = aclrtMalloc(1MB)                               # 2. ACL 申请显存（非 HIXL 接口）
remote_addr = socket_recv()                          # 3. 应用层协商远端地址
handle = engine.RegisterMem({buf, 1MB}, MEM_DEVICE)  # 4. 注册本端显存
engine.Connect("127.0.0.1:16001", 5000)              # 5. 建链
engine.TransferSync("127.0.0.1:16001", READ,
                    [{buf, remote_addr, 1MB}], 5000) # 6. 单边读
verify(buf)                                          # 7. 校验
engine.Disconnect("127.0.0.1:16001", 5000)           # 8. 断链
engine.DeregisterMem(handle); engine.Finalize()      # 9. 清理
```

其中第 6 步语义展开为：

\[
\underbrace{\text{local\_addr}}_{\text{client 显存}} \xleftarrow{\;len\;} \underbrace{\text{remote\_addr}}_{\text{server 显存}}
\]

即把 server 显存中起始于 `remote_addr`、长度为 `len` 的数据，直接 DMA 到 client 显存的 `local_addr`，中间没有任何 host 侧缓冲。

#### 4.2.3 源码精读

**① 引擎初始化与链路选项**

[examples/cpp/hixl_example_quickstart.cpp:L74-L79](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_quickstart.cpp#L74-L79)

```cpp
void InitEngine(EngineCtx &ctx, const char *local) {
  ACL_EXIT_ON_FAILURE(aclrtSetDevice(ctx.device));
  std::map<AscendString, AscendString> opts;
  opts[OPTION_GLOBAL_RESOURCE_CONFIG] = R"({"comm_resource_config.protocol_desc": ["hccs:device"]})";
  HixlExitOnFailure(ctx.engine.Initialize(local, opts), "Initialize");
}
```

初始化前必须先 `aclrtSetDevice` 选卡（HIXL 引擎绑定当前 device）。选项键 `OPTION_GLOBAL_RESOURCE_CONFIG` 定义于 [include/hixl/hixl_types.h:L33](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl_types.h#L33)（值为字符串 `"GlobalResourceConfig"`），其 JSON 值里的 `"hccs:device"` 表示走 HCCS 链路的 device 内存语义。对应公开接口声明 [include/hixl/hixl.h:L46](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h#L46)：`Status Initialize(const AscendString &local_engine, const std::map<AscendString, AscendString> &options);`

**② client：注册、建链、传输、校验**

[examples/cpp/hixl_example_quickstart.cpp:L153-L172](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_quickstart.cpp#L153-L172)

```cpp
HixlExitOnFailure(ctx.engine.RegisterMem(ctx.desc, MEM_DEVICE, ctx.handle), "RegisterMem");
HixlExitOnFailure(ctx.engine.Connect(kServerEngine, kTimeoutMs), "Connect");
HixlExitOnFailure(ctx.engine.TransferSync(kServerEngine, READ, {ctx.op}, kTimeoutMs), "TransferSync");
printf("[INFO] TransferSync READ completed\n");
VerifyData(ctx.buf);
HixlExitOnFailure(send(ctx.fd, "d", 1, 0) == 1, "send done signal failed");
HixlExitOnFailure(ctx.engine.Disconnect(kServerEngine, kTimeoutMs), "Disconnect");
```

这 8 行就是 HIXL 传输的全部核心调用。`TransferSync` 的第 3 个参数是 `std::vector<TransferOpDesc>`，本例只传一个元素 `{ctx.op}`（在 `PrepareClientMemAndOp` 中填充，见 [examples/cpp/hixl_example_quickstart.cpp:L148-L150](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_quickstart.cpp#L148-L150)）；批量传多个描述符即可一次下发多条传输。接口签名见 [include/hixl/hixl.h:L124-L125](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h#L124-L125)。`RegisterMem`/`Connect`/`Disconnect` 的声明分别在 [include/hixl/hixl.h:L60](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h#L60)、[L75](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h#L75)、[L83](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h#L83)。

**③ 数据校验**

[examples/cpp/hixl_example_quickstart.cpp:L111-L118](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_quickstart.cpp#L111-L118)

```cpp
void VerifyData(void *buf) {
  std::vector<uint8_t> host(kBufSize);
  ACL_EXIT_ON_FAILURE(aclrtMemcpy(host.data(), kBufSize, buf, kBufSize, ACL_MEMCPY_DEVICE_TO_HOST));
  std::vector<uint8_t> expected(kBufSize, kFillValue);
  bool ok = (memcmp(host.data(), expected.data(), kBufSize) == 0);
  ...
}
```

校验思路：把 client 显存整块拷回 host，与全 `0x5A` 的期望值 `memcmp`。若远端数据没有正确到达，或读到了未初始化内存，校验会失败并打印 `Verify failed`。

**④ 资源清理**

[examples/cpp/hixl_example_quickstart.cpp:L120-L137](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_quickstart.cpp#L120-L137)

`Finalize` 辅助函数按"关 socket → `DeregisterMem` → `aclrtFree` → `engine.Finalize()` → `aclrtResetDevice`"的顺序逆序释放。注意 [docs/zh/quick_start.md:L5](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/quick_start.md#L5) 特别说明：本样例失败路径直接 `exit`，不做这些清理，仅用于功能验证；正式业务需补全异常分支的资源释放。

#### 4.2.4 代码实践（本讲主实践）

**把样例改为传输 4KB 数据并打印校验结果**

1. **实践目标**：通过亲手改小传输量，验证你对 `kBufSize` 贯穿链路（分配、填充、注册、传输、校验）的理解。
2. **操作步骤**：
   1. 确认已执行 `source /usr/local/Ascend/cann/set_env.sh` 并完成 `bash build.sh --examples` 编译（见上一讲）。
   2. 复制一份样例源码再修改（示例代码，非项目原有代码）：
      ```bash
      cp examples/cpp/hixl_example_quickstart.cpp /tmp/my_quickstart.cpp
      ```
   3. 在副本中将 [L32](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_quickstart.cpp#L32) 的
      ```cpp
      constexpr size_t kBufSize = 1024 * 1024;
      ```
      改为
      ```cpp
      constexpr size_t kBufSize = 4 * 1024;
      ```
   4. 把副本加入 examples 构建或用与原样例相同的编译选项手动编译（头文件与链接库路径可参考 `build/examples/cpp` 下已有产物及 [examples/run_example.sh:L240](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/run_example.sh#L240) 所在目录约定）。
   5. 在两张互通 device 上先启动 server 再启动 client（命令见 4.3 节）。
3. **需要观察的现象**：client 终端输出 `TransferSync READ completed` 后紧跟 `Verify success`；与 1MB 版本相比，`VerifyData` 阶段明显更快。
4. **预期结果**：因为填充值、校验逻辑都不变，4KB 传输同样应打印 `Verify success`——传输正确性与数据量无关。
5. 若当前环境没有昇腾硬件，本实践「待本地验证」；可先做纯源码推演：列出所有依赖 `kBufSize` 的代码行（L32、L112-L114、L141、L143、L150、L184 共 7 处），确认改一个常量即可全链路生效、无需其他改动。

#### 4.2.5 小练习与答案

**练习 1**：把 `TransferSync` 的第二个参数从 `READ` 改成 `WRITE`，其余不动，会发生什么？

**参考答案**：语义反转为"把 client 显存写入 server 显存"。但 client 的显存从未被填充过（内容不确定），server 侧也没有任何校验逻辑，因此程序大概率"成功"执行但没有可观察的正确性证据。要做一个有意义的 WRITE 实验，需要 client 先填充数据、server 在收到 done 信号后把显存拷回 host 校验——这正是 `RunServer`/`RunClient` 职责需要互换的部分。此题说明：`TransferOp` 只换方向，地址三元组含义不变。

**练习 2**：`TransferSync` 一次能传几段数据？依据是什么？

**参考答案**：多段。第 3 个参数是 `const std::vector<TransferOpDesc> &op_descs`（[include/hixl/hixl.h:L124-L125](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/hixl/hixl.h#L124-L125)），样例用 `{ctx.op}` 只传了 1 个元素。向量的每个元素都是独立的 `(local_addr, remote_addr, len)` 三元组，一次调用即可下发一批传输，这是 KV Cache 按层/按 block 批量传输的基础。

**练习 3**：`kTimeoutMs = 5000` 用在了哪几个接口上？超时后接口行为是什么？

**参考答案**：用在 `Connect`、`TransferSync`、`Disconnect` 三个调用上（`RunClient` L162-L168）。超时后接口返回非 `SUCCESS` 的 `Status`，样例的 `HixlExitOnFailure` 打印 `aclGetRecentErrMsg()` 的信息并以 `exit(EXIT_FAILURE)` 退出——注意这会跳过所有资源清理，这是 quickstart 刻意简化的失败处理方式。

### 4.3 样例启动方式与常见失败排查

#### 4.3.1 概念说明

"跑通一个分布式样例"的大部分时间通常不在代码上，而在环境上。对昇腾双 device 样例，最常见的两类失败是：

1. **两个 device 物理上不互通**（HCCS 链路不可达，例如 A3 环境单卡双 die 之间互不通信）。
2. **两个 device 的 TLS 证书配置不一致**（一端开、一端关，无法建链）。

官方文档 [docs/zh/quick_start.md:L21-L22](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/quick_start.md#L21-L22) 明确指出执行失败时应先到 [examples/README.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/README.md) 检查这两项。

#### 4.3.2 核心流程

启动流程：

```
source CANN 环境变量 → bash build.sh --examples → cd build/examples/cpp
  → 终端1: ./hixl_example_quickstart --role=server   （必须先启动）
  → 终端2: ./hixl_example_quickstart --role=client
  → 成功标志：client 侧出现 "[INFO] TransferSync READ completed" 与 "[INFO] Verify success"
```

排查流程（失败时按序检查）：

```
npu-smi info                          → 确认 device id 存在
hccn_tool -i <id> -ip -g              → 拿到两张卡的 device ip
hccn_tool -i a -ping -g address ip_b  → 双向 ping 验证连通
hccn_tool -i <id> -tls -g             → 确认 TLS 开关一致，不一致则统一关闭
```

#### 4.3.3 源码精读

**① 官方启动命令与成功标志**

[docs/zh/quick_start.md:L24-L47](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/quick_start.md#L24-L47)

文档规定：进入 `build/examples/cpp` 目录，**先启动 server 再启动 client**，client 终端出现 `TransferSync READ completed` 和 `Verify success` 两行日志即代表 READ 传输与数据校验成功。默认参数（client 用 device 0 / `127.0.0.1:16000`，server 用 device 2 / `127.0.0.1:16001`）记录在 [docs/zh/quick_start.md:L60-L67](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/docs/zh/quick_start.md#L60-L67)。

**② device 连通性检查**

[examples/README.md:L32-L63](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/README.md#L32-L63)

文档给出用 `hccn_tool` 三步检查：查 ip（`hccn_tool -i $i -ip -g`）、双向 ping、确认连通。特别提醒 A3 环境是"一卡双 die"架构，**单卡的两个 die（如 device-0 与 device-1）之间不互通**，需要挑选满足连通关系的 device id 组合——quickstart 默认的 0 和 2 正是绕开了同卡双 die 的组合。

**③ TLS 一致性检查**

[examples/README.md:L65-L79](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/README.md#L65-L79)

```bash
for i in {0..7}; do hccn_tool -i $i -tls -g; done | grep switch
# 统一关闭 TLS：
for i in {0..7}; do hccn_tool -i $i -tls -s enable 0; done
```

TLS 状态不一致的两张卡无法建链，官方建议测试环境统一关闭 TLS 以排除该变量。

**④ 批量冒烟脚本中的成对拉起**

[examples/run_example.sh:L76-L164](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/run_example.sh#L76-L164)

`run_pair` 函数是脚本的核心原语：校验可执行文件存在后，用 `eval "$cmd > $tmp 2>&1 &` 把成对命令**同时**丢到后台（[L117-L124](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/run_example.sh#L117-L124)），`wait` 全部退出后汇总输出并用 `grep -qi "ERROR"` 判定成败（[L127-L136](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/run_example.sh#L127-L136)）。它把"先 server 后 client"的人工时序变成了"同时拉起、由 socket accept 自然同步"，因为 server 在 `accept` 上等待，client 晚几秒 connect 也没关系。需要说明：quickstart 并未列入 `run_hixl_cpp_examples`（[L184-L204](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/run_example.sh#L184-L204)）的冒烟清单，该脚本覆盖的是 d2rd/d2rh/multiproc 等样例——quickstart 定位是人工快速验证。

#### 4.3.4 代码实践

**实践：启动失败排查演练**

1. **实践目标**：掌握"日志定位 + hccn_tool 检查"的排查套路。
2. **操作步骤**：
   1. 有硬件时：故意把 client 的 `--role=client` 拼成 `--role=Client`（大写 C），观察 Usage 输出；再故意先启动 client 后启动 server，观察 client 阻塞在 `socket connect failed` 的报错。
   2. 无硬件时：阅读 [examples/README.md:L32-L79](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/README.md#L32-L79)，整理一份"启动失败 → 可能原因 → 检查命令"三列对照表。
3. **需要观察的现象**：角色参数错误立即打印 Usage 退出；client 先于 server 启动时在 socket `connect` 处失败（因为 17001 端口无人监听）。
4. **预期结果**：整理出至少 3 类失败模式：参数错误（Usage 退出）、启动顺序错误（connect 失败）、环境问题（ping 不通 / TLS 不一致导致 `Initialize` 或 `Connect` 返回非 SUCCESS）。
5. 无昇腾硬件时运行部分「待本地验证」，源码整理部分可直接完成。

#### 4.3.5 小练习与答案

**练习 1**：client 比 server 早启动 10 秒，样例还能成功吗？

**参考答案**：能或不能取决于差距多大：client 的 `socket connect` 在 server 尚未 `listen` 时会立刻失败（样例不重试），直接退出。所以"先 server 后 client"是硬性顺序。但注意 server 内部在 `accept` 上阻塞等待，server 先启动任意长时间都没问题。

**练习 2**：为什么 quickstart 选 device 0 和 device 2，而不是 0 和 1？

**参考答案**：因为 A3 环境单卡双 die（device-0/1 同卡、device-2/3 同卡）之间不互通，而样例希望一套默认参数在 A2 和 A3 上都能跑通，0 和 2 的组合在两类环境下都可互通（见 [examples/README.md:L61-L63](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/README.md#L61-L63)）。这是跨芯片代际兼容性在样例默认值上的体现。

**练习 3**：如何不用任何修改，把 quickstart 跑在非默认的两张卡上？

**参考答案**：做不到——quickstart 的 device id 与 engine 地址全部是编译期常量（L26-L30），没有命令行参数可以覆盖。这正好说明了它"仅用于功能验证"的定位；需要灵活选卡的样例请参考 `hixl_example_d2rd`（支持 `--device=` 参数，见 [examples/run_example.sh:L191-L193](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/run_example.sh#L191-L193)），这也是下一讲的内容。

## 5. 综合实践

**任务：给 quickstart 画一张完整的"接口-角色-时序"注解图，并做一次 4KB 改造实验。**

1. 打开 [examples/cpp/hixl_example_quickstart.cpp](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_quickstart.cpp)，逐行标注每个 HIXL 调用属于哪个角色、处于调用序列的第几步、依赖哪个前置条件。例如 `RegisterMem`（server）依赖 `aclrtMalloc` 已完成；`TransferSync`（client）依赖"本端已注册 + 远端地址已交换 + Connect 成功"三个前置条件。
2. 把标注结果整理成两张检查清单：**client 前置清单**（Initialize → Malloc → ExchangeAddr → RegisterMem → Connect）与 **server 前置清单**（Initialize → Malloc → Fill → RegisterMem → ExchangeAddr），任何一步失败时对照 [examples/cpp/hixl_example_quickstart.cpp:L40-L54](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_quickstart.cpp#L40-L54) 的错误打印定位是哪一步失败。
3. 有硬件的读者：完成 4.2.4 节的 4KB 改造实验并跑通；无硬件的读者：完成 `kBufSize` 全部 7 处引用的静态排查，写出"改一个常量为何足够"的分析（提示：填充、校验、传输长度都由同一个常量驱动，无硬编码字节数残留）。

## 6. 本讲小结

- quickstart 采用 **server/client 双进程模型**：一份源码两种角色，server 只准备数据并告知地址，client 独占全部 HIXL 主动调用，直观展示了"单边通信"中被动方的工作量几乎为零。
- HIXL C++ API 的典型序列是 **Initialize → RegisterMem → （应用层协商远端地址）→ Connect → TransferSync → Disconnect → DeregisterMem/Finalize**；`READ` 传输的本质是用一个 `(local_addr, remote_addr, len)` 三元组发起的远端显存直达本端显存的 DMA。
- 远端地址必须由应用层自行协商，样例用一条 9 字节以内的 TCP 通道完成"地址交换 + 完成同步"，业务数据则完全走 HIXL 的 `hccs:device` 链路——控制面与数据面分离。
- 顺序约束是隐性合同：server 必须先 `RegisterMem` 再发地址；client 必须先 `Connect` 成功再 `TransferSync`；server 靠 1 字节 done 信号保证引擎在传输期间存活。
- 样例默认参数（client=device 0/16000，server=device 2/16001）是为了同时兼容 A2/A3（A3 单卡双 die 互不互通）而精心挑选的；常见失败排查抓手是 `npu-smi info`、`hccn_tool ping` 与 `hccn_tool tls` 三件套。
- quickstart 失败路径不做资源清理，仅用于功能验证；生产代码需参考其他样例补全异常处理。

## 7. 下一步学习建议

下一讲（u1-l5）将把 quickstart 的单一 D2D READ 场景扩展为 **D2D / D2H / H2D / D2rH 多路径样例对比**：阅读 [examples/cpp/hixl_example_d2rd.cpp](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rd.cpp)、[examples/cpp/hixl_example_d2rh.cpp](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rh.cpp) 与 [examples/cpp/hixl_example_d2rd_multiproc.cpp](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rd_multiproc.cpp)，观察它们如何用命令行参数（`--protocol`、`--device`、`--version`）覆盖 quickstart 中的编译期常量，并体会 `MemType`（MEM_DEVICE/MEM_HOST）与链路协议（hccs/roce）的组合方式。完成入门单元后，单元 2 将逐个精读 `include/hixl/hixl.h` 中本讲只是"用过"的接口的内部实现。
