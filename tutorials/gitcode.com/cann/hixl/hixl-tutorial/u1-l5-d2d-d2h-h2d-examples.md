# 多场景传输样例：D2D/D2H/H2D/D2rH

## 1. 本讲目标

学完本讲，你应该能够：

1. 准确说出 D2D、D2H、H2D、D2rD、D2rH 等传输路径记号的含义，以及每条路径「本地内存类型 × 远端内存类型」的组合方式。
2. 读懂 `hixl_example_d2rd`（单进程双 engine、设备到设备 WRITE）、`hixl_example_d2rh`（单进程双 engine、设备到远端主机双向异步 WRITE）、`hixl_example_d2rd_multiproc`（双进程、socket 换址、READ）三个样例的完整调用序列。
3. 对比三个样例与 quickstart 在初始化参数（`--protocol`、`--version`）、角色划分（单进程 vs client/server 双进程）、内存注册类型（`MEM_DEVICE` / `MEM_HOST`）与传输方向（READ / WRITE）上的差异。
4. 掌握多进程样例的组织方式：控制面（TCP socket 交换地址）与数据面（HIXL 单边传输）分离。

## 2. 前置知识

本讲假设你已经完成 u1-l3（quickstart 样例精读）。在继续之前，请回顾并理解以下概念：

- **单边通信**：只需要一端发起调用，就可以直接 READ（读）或 WRITE（写）对端注册过的内存，对端 CPU 不需要参与。quickstart 中 server 进程只负责注册内存并「告知地址」，真正的传输调用全部在 client 侧完成。
- **注册内存与 MemHandle**：内存必须先经 `RegisterMem` 注册（并声明类型 `MEM_DEVICE` 设备内存 / `MEM_HOST` 主机内存），才能被远端单边访问；`MemHandle` 是解注册时的凭证。
- **TransferOpDesc 三元组**：一次传输由若干 `TransferOpDesc` 描述，每个包含 `local_addr`（本端地址）、`remote_addr`（对端地址）、`len`（长度）。
- **READ 与 WRITE 的方向定义**：站在**发起传输的一端**看，`WRITE` 表示把 local 写到 remote，`READ` 表示把 remote 读到 local。方向与「谁是 server」无关。
- **ACL 运行时接口**：样例用 `aclrtSetDevice` 绑定设备、`aclrtMalloc` / `aclrtMallocHost` 分配设备 / 主机内存、`aclrtMemcpy` 在主机与设备之间搬数（即传统的 D2H/H2D 拷贝）。注意区分：**ACL 的 D2H/H2D 是本机内部的拷贝，而 HIXL 的 D2rH 是跨 engine 的网络传输**，两者层次不同。
- **engine 标识**：形如 `127.0.0.1:16000` 的 `ip:port` 字符串。`Initialize` 时 local_engine 带端口即具备 server 能力。

另外，本讲的样例都支持 `--version=0|1` 参数：`0` 表示走旧的 HCCL 集合通信域方式（legacy），`1` 表示走 HIXL CS 接口（推荐，默认值）。本讲以 version=1 为主线，legacy 分支只在源码精读中简要指出。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [examples/cpp/hixl_example_d2rd.cpp](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rd.cpp) | D2rD 单进程样例：一个进程内启动两个 engine（各绑一张卡），engine A 把设备内存 WRITE 到 engine B 的设备内存 |
| [examples/cpp/hixl_example_d2rh.cpp](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rh.cpp) | D2rH 单进程样例：双方各自把设备内存 WRITE 到对方的**主机**内存，双向异步并发 |
| [examples/cpp/hixl_example_d2rd_multiproc.cpp](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rd_multiproc.cpp) | D2rD 多进程样例：client/server 两个独立进程，socket 交换地址后 client 发起 READ |
| [examples/cpp/README.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/README.md) | 样例运行说明：参数表、协议硬件依赖、运行命令 |
| [examples/README.md](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/README.md) | 环境要求：hccn_tool 连通性检查、TLS 一致性检查 |

三个样例的构建产物在 `build/examples/cpp` 下，编译方式见 u1-l2（`bash build.sh --examples`）。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**4.1 传输路径概念**、**4.2 d2rd 单进程样例**、**4.3 d2rh 单进程样例**、**4.4 d2rd_multiproc 多进程样例**。

### 4.1 传输路径：D2D / D2H / H2D / D2rD / D2rH 到底指什么

#### 4.1.1 概念说明

HIXL 传输涉及两端内存，每端内存各有两种形态：

- **D**evice（设备内存，NPU 上的显存，`MEM_DEVICE`）
- **H**ost（主机内存，CPU 侧锁页内存，`MEM_HOST`）

于是「本地内存 → 远端内存」的组合就构成一条**传输路径**。仓库样例命名中用的是「D2rX」记号，`r` 表示 **remote（远端 engine）**，避免与 ACL 本机拷贝混淆：

| 记号 | 含义 | 本地内存 | 远端内存 | 对应样例 |
| --- | --- | --- | --- | --- |
| D2rD | Device to remote Device | 设备内存 | 设备内存 | `hixl_example_d2rd`、`hixl_example_d2rd_multiproc` |
| D2rH | Device to remote Host | 设备内存 | 主机内存 | `hixl_example_d2rh` |
| D2D / D2H / H2D | 泛指设备/主机间的搬运方向（也用于 ACL 本机拷贝语境） | — | — | 文档中作为正交维度描述 |

一句话记忆：**HIXL 路径记号看两个字母——第一个是发起端（local）的内存类型，`r` 后面是对端（remote）的内存类型**。传输方向 READ/WRITE 是另一个正交维度，与路径记号无关：D2rD 可以用 WRITE（我写你）也可以用 READ（我读你，等价于你写我）。

适用场景直觉：

- **D2rD**：推理 PD 分离中最常见的 KV Cache 显存到显存直传，走 HCCS（同机）或 RDMA/UB（跨机）。
- **D2rH**：远端只想在主机侧拿到数据（例如落盘、CPU 后处理），或远端卡上显存紧张时先落到锁页内存。
- 一次 `RegisterMem` 只声明一种类型；两端注册的类型共同决定这条链路上「哪些地址可以被单边访问」。

#### 4.1.2 核心流程

一个通用传输会话的骨架（三个样例共用）：

```text
解析参数 (--protocol / --device / --version / --role)
  ↓
每个 engine: aclrtSetDevice → Initialize(local_engine, options)
  ↓                                # options 中通过 protocol_desc 声明链路协议
每个 engine: 分配内存 → RegisterMem(MEM_DEVICE 或 MEM_HOST)
  ↓
（可选）控制面交换远端地址   # 单进程样例直接读对端指针；多进程样例走 socket
  ↓
Connect / ConnectAsync
  ↓
TransferSync / TransferAsync(WRITE 或 READ, descs[])
  ↓
校验（memcmp）
  ↓
Disconnect → DeregisterMem → Finalize → aclrtResetDevice
```

其中 `options` 的构造是三个样例与 quickstart 最大的差异点：quickstart 用的是 `hccs:device` 单协议，而本讲样例把协议做成了命令行参数，通过 V2 配置项 `comm_resource_config.protocol_desc` 传给引擎。

#### 4.1.3 源码精读

协议合法列表在三个样例中各不相同，这是有意设计的——**每个样例只支持其路径走得通的协议**：

- d2rd 支持 `roce:device`、`roce:host`、`uboe:device`、`ub_rtp:device`、`ub_ctp:device`（无 hccs，因为单进程双 engine 的两卡间不走 hccs 域）：[examples/cpp/hixl_example_d2rd.cpp:33-34](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rd.cpp#L33-L34)
- d2rh 额外支持 `ub_ctp:host`（主机侧协议，D2rH 路径必需）：[examples/cpp/hixl_example_d2rh.cpp:35-36](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rh.cpp#L35-L36)
- multiproc 支持 `hccs:device`（继承 quickstart 的跨卡场景）：[examples/cpp/hixl_example_d2rd_multiproc.cpp:39-40](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rd_multiproc.cpp#L39-L40)

V2 模式下把协议列表拼成 JSON 数组塞进 `OPTION_GLOBAL_RESOURCE_CONFIG`：

```cpp
std::string config = "{\"comm_resource_config.protocol_desc\": [" + desc_array + "]}";
options[OPTION_GLOBAL_RESOURCE_CONFIG] = config.c_str();
```

这段代码在 [examples/cpp/hixl_example_d2rd.cpp:152-163](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rd.cpp#L152-L163)，作用是把命令行传入的协议列表转成引擎可识别的资源描述配置。协议与芯片的硬件对应关系（如 `ub_*` 系列仅支持 Ascend 950PR/950DT）总结在 [examples/cpp/README.md:129](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/README.md#L129) 的参数表中。

#### 4.1.4 代码实践

**实践目标**：不动代码，先在纸面上建立「路径记号 → 注册类型 → 传输方向」的映射能力。

**操作步骤**：

1. 打开 [examples/cpp/README.md:127-134](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/README.md#L127-L134) 的参数表，抄下三个样例各自支持的协议集合。
2. 用 `Grep` 在 `examples/cpp/` 下搜索 `RegisterMem`，统计每个样例出现的 `MEM_DEVICE` / `MEM_HOST` 次数。
3. 用 `Grep` 搜索 `TransferSync` 与 `TransferAsync`，记录每个样例用的操作码是 `READ` 还是 `WRITE`。

**需要观察的现象**：d2rd 只注册 `MEM_DEVICE`；d2rh 两种都注册；multiproc 只注册 `MEM_DEVICE`；操作码上 d2rd/d2rh 用 `WRITE`，multiproc 用 `READ`。

**预期结果**：你能填出下面这张表（本讲综合实践会再次用到）：

| 样例 | 路径 | 注册类型 | 操作码 | 进程模型 |
| --- | --- | --- | --- | --- |
| d2rd | D2rD | 仅 MEM_DEVICE | WRITE | 单进程双 engine |
| d2rh | D2rH | MEM_DEVICE + MEM_HOST | WRITE（双向） | 单进程双 engine |
| multiproc | D2rD | 仅 MEM_DEVICE | READ | 双进程 client/server |

#### 4.1.5 小练习与答案

**练习 1**：如果要把 d2rd 样例改成 H2rD 路径（本地主机内存写到远端设备内存），`RegisterMem` 和 `TransferOpDesc` 需要改哪里？

**答案**：发起端的缓冲区从 `aclrtMalloc` 设备内存换成 `aclrtMallocHost` 锁页主机内存，发起端的 `RegisterMem` 类型从 `MEM_DEVICE` 改为 `MEM_HOST`；`TransferOpDesc` 的 `local_addr` 改为该主机缓冲区地址，`remote_addr` 仍指向对端设备缓冲区；对端保持 `MEM_DEVICE` 注册不变。（注：仓库当前没有现成的 H2rD 样例，此为示例代码层面的推演。）

**练习 2**：「D2rH 用 WRITE」和「对端用 READ 把自己的主机内存读走」在数据流向上等价吗？

**答案**：数据流向上等价（都是设备内存的数据落到主机内存），但发起端不同：前者由持数据的一端发起 WRITE，后者由持主机内存的一端发起 READ。单边通信中谁发起很关键——发起端需要同时握有本端地址和远端地址，而被动端可以完全不感知。

### 4.2 hixl_example_d2rd：单进程双 engine 的 D2rD

#### 4.2.1 概念说明

这个样例回答一个问题：**验证 HIXL 一定要起两个进程吗？** 不一定。一个进程里可以创建两个 `Hixl` 对象（两个 engine），分别 `aclrtSetDevice` 绑到两张卡上，两个 engine 互为对端建链。这样调试传输路径时只需一个终端、一条命令。

代价是：两个 engine 共享进程的地址空间，所以「远端地址」直接就是一个普通 C++ 指针，**不需要任何控制面交换地址**——这正是它比 multiproc 样例短的原因。

注意版本约束：单进程用例要求 CANN ≥ 9.1.0（见 [examples/cpp/README.md:121](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/README.md#L121)）。

#### 4.2.2 核心流程

```text
main: ParseArgs(--protocol 必选, --device=id1,id2, --version)
  ↓ 构造 ctx_a(device 0, "127.0.0.1:16000") / ctx_b(device 2, "127.0.0.1:16001")
InitEngine(A): SetDevice(0) → Initialize(16000, opts) → aclrtMalloc(8MB)
               → RegisterMem(MEM_DEVICE) → 拷入 0xAA
InitEngine(B): 同上, 绑 device 2, 填 0xBB
Connect: A→B 同步 Connect(5000ms)          # 只有 A 发起，B 只是被动监听
Transfer: 构造 512 个 16KB 的 TransferOpDesc
          A.TransferSync(B, WRITE, descs, 30000ms)   # A 的 dev → B 的 dev
Verify: B 侧 aclrtMemcpy(D2H) 回主机, memcmp 全 0xAA 则成功
Finalize: A.Disconnect(B) → 双方 DeregisterMem → aclrtFree → Finalize → ResetDevice
```

#### 4.2.3 源码精读

每个 engine 的上下文打包在 `EngineCtx` 里，注意它只持有 `dev_buf` / `dev_handle`，没有主机注册内存：[examples/cpp/hixl_example_d2rd.cpp:44-52](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rd.cpp#L44-L52)。

初始化的核心序列（SetDevice → Initialize → malloc → RegisterMem → 灌数据）：[examples/cpp/hixl_example_d2rd.cpp:165-195](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rd.cpp#L165-L195)。其中第 186 行 `RegisterMem(desc, MEM_DEVICE, ctx.dev_handle)` 把 8MB 设备内存登记为可被远端单边访问的内存；第 192 行用 `aclrtMemcpy(H2D)` 灌入区分性填充值（A 填 `0xAA`，B 填 `0xBB`）。

传输构造：8MB 被切成 512 个 16KB 的块，每块一条 `TransferOpDesc`，`local_addr` 与 `remote_addr` 同偏移递增：[examples/cpp/hixl_example_d2rd.cpp:208-225](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rd.cpp#L208-L225)。第 218 行 `TransferSync(ctx_b.name, WRITE, descs, kXferTimeout)` 一次同步下发 512 条描述——这演示了 **HIXL 的批量语义：一组 descs 一次调用**，而不是 512 次传输调用。

校验在 B 侧进行，先把设备内存 `aclrtMemcpy` 回主机再 `memcmp` 期望值 `0xAA`：[examples/cpp/hixl_example_d2rd.cpp:227-240](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rd.cpp#L227-L240)。这一步本身就是一次传统的 D2H 拷贝——印证 4.1 中「ACL 拷贝与 HIXL 传输是两个层次」。

清理顺序值得留意：先 Disconnect，再 DeregisterMem，再 aclrtFree，最后 Finalize 与 ResetDevice：[examples/cpp/hixl_example_d2rd.cpp:242-271](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rd.cpp#L242-L271)。这是注册内存生命周期 的标准倒序释放。

#### 4.2.4 代码实践

**实践目标**：直观感受「WRITE + 批量 descs」的行为，并验证传输确实发生在设备内存之间。

**操作步骤**：

1. 按 u1-l2 编译样例：`bash build.sh --examples`。
2. 进入 `build/examples/cpp`，在两张互通的卡上运行：
   ```shell
   ./hixl_example_d2rd --protocol=roce:device
   # 或指定卡号
   ./hixl_example_d2rd --protocol=roce:device --device=0,2
   ```
3. 把填充值对比作为观察点：打开源码把 `kFillA`（0xAA）与 `kFillB`（0xBB）位置记下来，理解 `Verify` 为何只检查 `0xAA`。
4. 把 `kXferBlockSize`（16KB）改为 64KB 重新编译，观察日志中传输完成时间的变化。

**需要观察的现象**：终端依次打印 `InitEngine ... success`、`Connect success`、`Transfer completed`、`Verify success`；改为更大块后 desc 数量从 512 变为 128。

**预期结果**：`Verify success`，退出码 0。若两卡不互通会卡在 Connect 超时（5000ms）——回到 [examples/README.md:32-63](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/README.md#L32-L63) 用 hccn_tool 检查。无昇腾硬件环境时本实践**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 d2rd 只需要 A 发起 `Connect`，B 不需要调用 Connect？

**答案**：B 的 engine 标识带端口（`127.0.0.1:16001`），Initialize 后即具备监听能力；单边通信的建链由主动方发起即可。这与 quickstart 的模型一致——被动方的全部职责是初始化、注册内存、保持存活。

**练习 2**：把第 218 行的 `WRITE` 改成 `READ`，程序还能验证通过吗？

**答案**：不能。`READ` 语义是「把 remote 读到 local」，改后 A 会把 B 的 `0xBB` 数据读进自己的缓冲区，而 `Verify` 仍在 B 侧检查 `0xAA`，B 的缓冲区从未被改写，校验会失败（B 里仍是初始的 `0xBB`）。要让程序通过，需同时把 Verify 移到 A 侧并期望 `0xBB`。这个练习说明：操作码决定数据流向，校验位置必须与之匹配。

### 4.3 hixl_example_d2rh：双向 D2rH 与异步接口全家桶

#### 4.3.1 概念说明

d2rh 在 d2rd 基础上引入三个新变量，正好覆盖 HIXL 异步接口全家桶：

1. **主机内存注册（`MEM_HOST`）**：每个 engine 同时注册设备内存和锁页主机内存（`aclrtMallocHost` 分配），D2rH 路径的远端落点是主机缓冲区。
2. **双向并发**：A 和 B **同时**向对方的 engine 发起 `ConnectAsync`，再**同时**发起 `TransferAsync`——不再是「一个主动一个被动」，两个 engine 地位对等。
3. **异步状态轮询**：建链用 `GetAsyncConnectStatus`，传输用 `GetTransferStatus`，全部靠轮询状态机推进。

为什么主机内存要用 `aclrtMallocHost`（锁页内存）而不是普通 `malloc`？因为单边传输要求远端能直接 DMA 访问这段内存，锁页内存保证物理页不会被操作系统换出，且地址可以注册给网卡/HCCS 使用。

#### 4.3.2 核心流程

```text
InitEngine(A/B): SetDevice → Initialize → aclrtMalloc(8MB dev) + aclrtMallocHost(8MB host)
                 → RegisterMem(dev, MEM_DEVICE) + RegisterMem(host, MEM_HOST)
                 → dev 与 host 都灌填充值
Connect(双向异步):
  A.ConnectAsync(B) 且 B.ConnectAsync(A)
  循环: GetAsyncConnectStatus 直到两端都 CONNECTED (任一 CONNECT_FAILED 即退出)
Transfer(双向异步):
  A: descs[i] = {local: A.dev_buf+off, remote: B.host_buf+off}  → TransferAsync(WRITE)
  B: descs[i] = {local: B.dev_buf+off, remote: A.host_buf+off}  → TransferAsync(WRITE)
  循环: GetTransferStatus(req) 直到两个 req 都离开 WAITING
Verify: 直接 memcmp(A.host_buf)==0xBB 且 memcmp(B.host_buf)==0xAA
Disconnect(双向异步): DisconnectAsync + 轮询回到 NOT_CONNECT
```

状态机视角：

```text
建链: NOT_CONNECT ──ConnectAsync──> CONNECT_PENDING ──> CONNECTED
                                      └──失败──> CONNECT_FAILED
断链: CONNECTED ──DisconnectAsync──> DISCONNECT_PENDING ──> NOT_CONNECT

传输: WAITING ──> COMPLETED / 其他终态(非 WAITING 即出结果)
```

#### 4.3.3 源码精读

`EngineCtx` 扩展为双缓冲区、双句柄（`dev_buf/host_buf`、`dev_handle/host_handle`）：[examples/cpp/hixl_example_d2rh.cpp:46-56](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rh.cpp#L46-L56)。

同一个 engine 注册两种内存的写法：先 `RegisterMem(desc, MEM_DEVICE, dev_handle)`，复用同一个 `MemDesc` 换成主机地址后再 `RegisterMem(desc, MEM_HOST, host_handle)`：[examples/cpp/hixl_example_d2rh.cpp:190-203](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rh.cpp#L190-L203)。

双向异步建链与状态轮询（10ms 间隔轮询，任一端 `CONNECT_FAILED` 立即失败退出）：[examples/cpp/hixl_example_d2rh.cpp:210-237](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rh.cpp#L210-L237)。

D2rH 的关键一行在描述符构造：`local_addr` 取自本端**设备**缓冲区，`remote_addr` 取自对端**主机**缓冲区：[examples/cpp/hixl_example_d2rh.cpp:239-248](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rh.cpp#L239-L248)。

双向异步传输：A 先发起 `TransferAsync(WRITE)`，随后**原地复用**同一批 descs、把地址改成 B→A 方向后立刻发起第二次 `TransferAsync`——两个传输在引擎内并发执行，互不阻塞：[examples/cpp/hixl_example_d2rh.cpp:250-273](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rh.cpp#L250-L273)。

传输状态轮询带看门狗（最多 100000 次 × 100µs，超过判超时），只有 `COMPLETED` 才算成功：[examples/cpp/hixl_example_d2rh.cpp:275-302](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rh.cpp#L275-L302)。

校验最能体现 D2rH 的价值：`Verify` **直接** `memcmp` 主机缓冲区（[examples/cpp/hixl_example_d2rh.cpp:315-328](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rh.cpp#L315-L328)），不需要任何 `aclrtMemcpy`——数据已经落在 CPU 可直接访问的锁页内存里了，这正是 D2rH 相对 D2rD 省掉的那一步。

断链也是双向异步 + 轮询回 `NOT_CONNECT`：[examples/cpp/hixl_example_d2rh.cpp:330-355](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rh.cpp#L330-L355)。

#### 4.3.4 代码实践

**实践目标**：观察 `ub_ctp:host` 协议下 D2rH 双向并发传输，并与 d2rd 的同步 WRITE 对比。

**操作步骤**：

1. 在支持 host 侧协议的环境（Ascend 950PR/950DT）运行：
   ```shell
   ./hixl_example_d2rh --protocol=ub_ctp:host,ub_ctp:device
   ```
   在 A2/A3 环境则运行 `./hixl_example_d2rh --protocol=roce:device`。
2. 观察日志中两次 `TransferAsync` 之间没有等待——对比 d2rd 中 `TransferSync` 调用后立刻打印完成。
3. 在 `Verify` 前加一行 `printf` 打印 `A.host_buf` 前 16 字节（示例代码改动），确认 CPU 能直接读到远端写入的数据。

**需要观察的现象**：`Connect success` 在两端状态都到 `CONNECTED` 后才打印；`Transfer completed` 在两个 `req` 都离开 `WAITING` 后打印；`Verify success` 无需任何 D2H 拷贝。

**预期结果**：`Verify success`，A 的主机缓冲区为 `0xBB`、B 的为 `0xAA`。host 侧协议与硬件强相关，不满足条件时会在参数校验或 Initialize 阶段报错；无对应硬件时**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：d2rh 的两个 `TransferAsync` 为什么能并发？如果换成两次 `TransferSync` 会怎样？

**答案**：`TransferAsync` 只是下发描述符并立刻返回一个 `TransferReq`，真正搬运由引擎异步完成，CPU 随后轮询状态；因此两次下发之间没有阻塞。换成 `TransferSync` 则第一次调用会阻塞到 A→B 传输完成后才发起 B→A，两次传输串行执行，总耗时近似翻倍（单边传输的对端无需参与，所以并发是安全的）。

**练习 2**：`WaitTransfers` 里为什么把「离开 `WAITING`」和「等于 `COMPLETED`」分成两次判断？

**答案**：`TransferStatus` 除了 `WAITING/COMPLETED` 还有失败类终态。轮询循环先以 `WAITING` 作为「还没出结果」的判据退出循环，再单独检查终态是否为 `COMPLETED`，把「完成」与「出结果」区分开，失败状态能被正确捕获并打印，而不是被当成成功。

### 4.4 hixl_example_d2rd_multiproc：双进程模型与 socket 控制面

#### 4.4.1 概念说明

这是三个样例中唯一贴近真实部署形态的：**client 和 server 是两个独立进程**（可以分布在两台机器），各持一个 engine。它解决的问题和 quickstart 一样——跨进程后「远端缓冲区地址」不能再直接读指针，必须有一条控制面把地址送过来。

样例的控制面选择是最朴素的 TCP socket：

- server 在 `引擎端口 + 1000` 上监听（16001 → 17001），accept 后把自己的设备缓冲区地址（8 字节 `uintptr_t`）发给 client；
- client 连上这个 socket 收下地址，然后关闭数据面之外的一切依赖，走 HIXL `Connect` + `TransferSync(READ)`。

为什么传输用 `READ` 而不是 WRITE？因为地址流向决定的：server 把**自己**的地址发给了 client，client 同时知道自己的地址（本地指针）和远端地址，所以由 client 发起 READ（把 server 的 `0xAA` 拉进自己的设备内存）。这与 quickstart 的模型完全一致，区别是 quickstart 协议固定为 `hccs:device`，而本样例把协议做成了参数（支持 `hccs:device`、`roce:device` 等 6 种）。

#### 4.4.2 核心流程

```text
server 进程                          client 进程
────────────                        ────────────
ParseArgs(--role=server,            ParseArgs(--role=client,
  --protocol, --device,               --protocol, --device,
  --local/remote-engine)                --local/remote-engine)
InitEngine: SetDevice → Initialize   InitEngine: 同左
  → malloc → RegisterMem(MEM_DEVICE)
  → 灌 0xAA
监听 TCP 端口 = engine端口+1000       连接 server 的 TCP 端口(重试10次)
accept → 发送 dev_buf 地址(8字节)  →  recv 收到 remote_addr
等待 client 断开(阻塞 recv)          Connect(remote_engine, 5000ms)
                                     TransferSync(READ, 512×16KB descs)
                                     Verify: D2H 拷回主机 memcmp 0xAA
                                     Disconnect → 清理 → TCP 关闭
检测到 client 断开 → 清理退出
```

#### 4.4.3 源码精读

`EngineCtx` 增加了三个控制面字段：`remote_addr`（收到的远端地址）、`listen_fd` / `conn_fd`（socket 句柄）：[examples/cpp/hixl_example_d2rd_multiproc.cpp:50-63](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rd_multiproc.cpp#L50-L63)。

参数解析支持 `--role`、`--device`（单值）、`--local-engine`、`--remote-engine`、`--protocol`、`--version`：[examples/cpp/hixl_example_d2rd_multiproc.cpp:104-129](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rd_multiproc.cpp#L104-L129)。角色还决定默认值兜底（client 默认 device 0 / `127.0.0.1:16000`，server 默认 device 2 / `127.0.0.1:16001`），见 [examples/cpp/hixl_example_d2rd_multiproc.cpp:172-178](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rd_multiproc.cpp#L172-L178)。

只有 server 灌数据（`if (!ctx.is_client)` 分支）：[examples/cpp/hixl_example_d2rd_multiproc.cpp:244-247](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rd_multiproc.cpp#L244-L247)。client 的缓冲区留空等待 READ 填充——被动方逻辑极简的又一次体现。

client 侧地址获取：连接 `远端引擎端口 + 1000` 的 TCP 端口（最多重试 10 次、每次间隔 0.5s，容忍 server 后启动），循环 `recv` 直到收满 8 字节地址：[examples/cpp/hixl_example_d2rd_multiproc.cpp:252-289](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rd_multiproc.cpp#L252-L289)。

传输核心：`local_addr` 用自己的设备缓冲区，`remote_addr` 用 socket 收来的地址，`TransferSync(READ, ...)`：[examples/cpp/hixl_example_d2rd_multiproc.cpp:302-319](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rd_multiproc.cpp#L302-L319)。

server 侧控制面三步曲——`SetupListenSocket`（bind + listen）→ `AcceptAndSendAddr`（accept 后循环 send 8 字节地址）→ `WaitForClientDisconnect`（阻塞 recv 直到 client 关闭连接，保证 engine 存活到传输结束）：[examples/cpp/hixl_example_d2rd_multiproc.cpp:336-405](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rd_multiproc.cpp#L336-L405)。

`Run` 里的角色分派一目了然：client 走「取地址→建链→传输→校验」，server 只走「发地址→等断开」：[examples/cpp/hixl_example_d2rd_multiproc.cpp:436-459](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rd_multiproc.cpp#L436-L459)。

#### 4.4.4 代码实践

**实践目标**：跑通双进程样例，理解 server 先启动的时序约束与 socket 控制面的作用。

**操作步骤**：

1. 两个终端分别执行（server 先启动）：
   ```shell
   # 终端 1
   ./hixl_example_d2rd_multiproc --role=server --protocol=roce:device
   # 终端 2（看到 server 打印 Waiting for client 后再执行）
   ./hixl_example_d2rd_multiproc --role=client --protocol=roce:device
   ```
2. 同机跨卡场景可换用 `--protocol=hccs:device`（默认 device 组合 0/2 已避开 A3 单卡双die不互通问题，见 [examples/README.md:61-63](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/README.md#L61-L63)）。
3. 先故意只启动 client 不启动 server，观察 TCP 重试日志（`Connect retry 1/10...`），10 次失败后进程退出。
4. 用 `ss -tnp | grep 17001` 在传输期间观察控制面连接的存在。

**需要观察的现象**：server 打印 `Sent buffer addr 0x... to client`；client 打印 `GetRemoteAddr success` → `Connect success` → `TransferSync READ completed` → `Verify success`；client 退出后 server 打印 `Client disconnected, server exiting`。

**预期结果**：双侧均以退出码 0 结束；client 侧 `Verify success` 证明 8MB `0xAA` 数据完整到达。无硬件环境时**待本地验证**，但步骤 3 的重试行为可以直接从 [源码 252-271 行](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/hixl_example_d2rd_multiproc.cpp#L252-L271) 静态推演。

#### 4.4.5 小练习与答案

**练习 1**：为什么 socket 端口要设计成 `引擎端口 + 1000`，而不是复用引擎自己的端口？

**答案**：引擎端口（16000/16001）属于 HIXL 数据面/通信面，由引擎内部使用；控制面 socket 是样例自己拼的简易业务通道。用偏移 1000 避开端口冲突，且两个端口可以从同一个 engine 字符串推导出来，不需要额外参数。

**练习 2**：server 的 `WaitForClientDisconnect` 一直阻塞到 client 断开才退出，删掉这一步会发生什么？

**答案**：server 可能在 client 传输完成前就执行 `DeregisterMem`、`Finalize` 甚至进程退出，远端注册内存失效，client 的 READ 会失败或读到无效数据。这对应 u1-l3 讲过的「done 信号保证引擎存活」——单边通信中被动方必须维持注册内存和 engine 存活，直到主动方完成传输。

**练习 3**：multiproc 与 d2rd 都是 D2rD，为什么一个用 READ 一个用 WRITE？

**答案**：谁掌握双方地址谁发起。d2rd 单进程里发起方 A 直接可见 B 的指针，用 WRITE 顺理成章；multiproc 中 server 把地址发给 client，client 是唯一同时知道两端地址的一方，只能由它发起——用 READ 把远端数据拉过来。数据流向上两种写法等价，选择取决于控制面地址的流向。

## 5. 综合实践

**任务：制作三样例调用序列对比表并验证一个「改向」实验。**

1. **对比表**：重读三个样例源码，填写并扩展 4.1.4 中的表格，至少包含以下维度：进程模型、engine 数量、`--protocol` 支持集、`--device` 语义（`id1,id2` vs 单 `id`）、注册内存类型、控制面（无 / TCP socket）、建链方式（Connect / 双向 ConnectAsync）、传输接口（TransferSync / TransferAsync+GetTransferStatus）、操作码（WRITE / READ）、校验位置与方式（D2H 拷回后 memcmp / 直接 memcmp 锁页内存）。
2. **改向实验**：以 d2rh 为模板做一份拷贝（示例代码），把 B→A 方向的第二次 `TransferAsync` 删掉，只保留 A→B 单向传输；同步修改 `Verify`（只检查 B 的 host_buf）与 `WaitTransfers`（只轮询 `req_a`）。重新编译运行，确认行为退化为「单向 D2rH」。
3. **记录**：把每一步的日志前后对比写入笔记，特别记录：删掉第二次下发后，第一次传输的完成时间是否有变化（预期基本无变化，因为 d2rh 中两个方向本就并发）。

无昇腾硬件时，第 1 项可完整完成（纯源码阅读），第 2、3 项标注「待本地验证」。

## 6. 本讲小结

- 传输路径记号由两端内存类型拼成：D2rD（设备→远端设备）、D2rH（设备→远端主机）；READ/WRITE 是与之正交的方向维度，由发起方视角定义。
- `hixl_example_d2rd`：单进程双 engine（各绑一张卡）验证 D2rD，免控制面、同步 WRITE、512×16KB 批量 descs 一次下发。
- `hixl_example_d2rh`：双 engine 对等，各自注册 `MEM_DEVICE` + `MEM_HOST`，双向 `ConnectAsync` + 双向 `TransferAsync` 并发，靠 `GetAsyncConnectStatus` / `GetTransferStatus` 轮询推进；校验直接 memcmp 锁页内存。
- `hixl_example_d2rd_multiproc`：真实部署形态的双进程模型，TCP socket（引擎端口+1000）作控制面交换地址，client 发起 READ；server 靠「等 client 断开」保住注册内存存活。
- 单边通信的通用规律在本讲反复出现：**谁同时掌握两端地址，谁就是发起方**；被动方只需初始化、注册内存、保持存活。
- 协议（`--protocol`）与传输版本（`--version=0|1`）都是命令行参数，协议支持集与芯片强相关，查 [examples/cpp/README.md:129](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/README.md#L129) 的参数表。

## 7. 下一步学习建议

本讲你已通过样例把 HIXL 五组接口（生命周期/内存/链路/传输/通知之外的绝大部分）都「用」过一遍。下一讲进入单元 2：

- **u2-l1（Hixl 类与初始化流程）**：本讲样例里每个 `Initialize` 传入的 options（`OPTION_GLOBAL_RESOURCE_CONFIG`、`OPTION_LOCAL_COMM_RES`、`OPTION_BUFFER_POOL`）会逐一展开精读。
- **u2-l3（内存注册）**：本讲只展示了 `RegisterMem` 的调用侧，下一单元深入引擎内部的 segment 与 `HixlMemStore` 登记逻辑，解释「注册之后发生了什么」。
- **u2-l4 / u2-l5（建链与传输）**：本讲的 `ConnectAsync` 状态机与 `GetTransferStatus` 轮询将下沉到 `HixlClient` / `ClientManager` 的实现层。

建议在进入单元 2 前，把本讲综合实践的对比表留好——它是后续阅读引擎实现时的「调用侧事实清单」。
