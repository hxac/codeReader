# 通信矩阵：ZMQ 心跳与 llm_datadist 传输端口

## 1. 本讲目标

上一讲（u4-l2）我们读完了 `LLMDataDistConnector` 的四个协作类，知道了 KV 块是「怎么搬」的。本讲换一个视角，回答「在哪儿搬」：P 节点和 D 节点之间到底开了哪些端口、每条链路由谁监听、由谁发起、在什么时机被触发。

学完本讲，你应该能够：

1. 说出 ZMQ 心跳链路（PUB/SUB、PUSH/PULL、IPC）与 llm_datadist RoCE 数据链路各自的职责与端口默认值（5568、15566、15567）。
2. 根据部署规模（TP/DP/PP、P 实例数）推算出每类端口在每台机器上的具体取值。
3. 在运行中的部署里用 `ss`/`netstat` 找到这些监听端口，并与 omni-npu README 的端点矩阵逐行对照，形成自己团队的部署网络文档。

## 2. 前置知识

### 2.1 控制面与数据面

一个分布式系统通常有两类通信：

- **控制面（control plane）**：小消息、低频率、丢了能补救。本讲里 ZMQ 承载的全部内容——回执、心跳、超时通知——都是控制面。
- **数据面（data plane）**：大流量、高吞吐、丢一块就出错。KV Cache 张量的实际搬运走 llm_datadist 的 RoCE 链路，是数据面。

两者分离的价值：心跳抖动不影响 KV 传输性能，KV 大流量也不会把心跳挤到超时。

### 2.2 ZMQ 三种套接字模式

ZMQ（ZeroMQ）是消息队列库，不是独立服务器。本讲用到三种模式：

| 模式 | 方向 | 特点 | 本讲用途 |
| --- | --- | --- | --- |
| `PUSH`/`PULL` | 单向管道 | PUSH 端 `connect`，PULL 端 `bind`；每条消息只投给一个接收者 | D → P 发送「KV 已拉取」回执与心跳 |
| `PUB`/`SUB` | 发布订阅 | PUB 端 `bind`，SUB 端 `connect`；按主题前缀过滤，可多订阅者 | P → D 广播心跳 `prefill_hb:<cluster_id>` |
| IPC | 同机进程间 | 地址形如 `ipc:///tmp/xxx`，不走网卡 | P 节点内 rank0 通知其他 rank 执行 `force_unlink` |

记住一个关键不对称：**bind 的一端监听端口，connect 的一端只发起连接**。后面用 `ss -tlnp` 观察时会看到，很多端口只在 P 节点处于 LISTEN 状态。

### 2.3 RoCE 与 llm_datadist

RoCE（RDMA over Converged Ethernet）让以太网具备 RDMA 能力：网卡直接读写对端内存，绕过双方 CPU 协议栈。`llm_datadist` 是昇腾提供的 KV 传输库（Python 包 `llm_datadist`），底层走 RoCE 零拷贝搬运注册过的显存块。u4-l2 已经讲过它的会话管理，本讲只关注它占用的端口。

### 2.4 「默认路由发现本机 IP」技巧

代码里获取本机 IP 的方式是：创建一个 UDP socket，`connect` 一个公网地址（8.8.8.8:80），然后读本地端点地址。UDP 的 `connect` 不发任何包，只是让内核查路由表选出「去往该地址会用哪块网卡的哪个源 IP」。因此机器有多块网卡时，**默认路由决定了被发现 的 IP**——这是多机部署配错网卡时 KV 链路连错的根源之一。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `components/omni-npu/README.md` | 官方端点矩阵（6 行表格），本讲的对照基准 |
| `components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py` | 四个协作类所在文件；所有 ZMQ 套接字的创建、bind/connect、心跳线程都在这里 |
| `components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py` | llm_datadist 引擎封装；数据面端口（`listen_ip_info`）、cluster_id 编码、IP 发现在这里 |
| `components/omni-npu/src/omni_npu/connector/utils.py` | `get_config_from_dict_or_env` 等工具；本讲用它证明 prefill 侧 DP 恒为 1 |
| `tools/scripts/pd_run.sh` | 部署脚本；`VLLM_LLMDATADIST_ZMQ_PORT` 的默认值与导出在这里 |

## 4. 核心概念与源码讲解

### 4.1 ZMQ 心跳链路：PUB/SUB、PUSH/PULL 与 IPC

#### 4.1.1 概念说明

u4-l2 讲过容错机制的三层递进，其中第三层是「双向心跳 + force_unlink 清尸」。本模块把这条心跳链路拆到端口级。

为什么需要心跳？D 侧对每个远端 P 维护了 `registered_link_infos`（已建立的 llm_datadist 链路表），P 侧为每个已完成的请求延迟释放 KV 块。如果对端进程直接被 kill -9 或机器断电，链路表和延迟释放的块就成了没人认领的垃圾。心跳的作用是：**双方每 5 秒互报一次存活，超过 60 秒没收到对方心跳，就主动清理与对方相关的所有状态**。

方向很重要，两条心跳走的通道不一样：

- **P → D**：P 的 rank0 进程用 `PUB` 套接字广播 `prefill_hb:<host_cluster_id>`，默认端口 15566；D 侧每个 worker 用 `SUB` 订阅。
- **D → P**：D 侧 worker 复用发回执的那条 `PUSH` 连接（默认 5568），定期发送 `["decode_hb:<cluster_id>"]`；P 的 rank0 用 `PULL` 套接字接收，靠 `decode_hb:` 前缀把心跳和回执区分开。

此外还有一条同机 IPC：P 节点上只有 rank0 能收到 D 的心跳（PULL 端口只 bind 在 rank0），但链路是每个 rank 各自持有的，所以超时后 rank0 要通过 IPC 通知持有该链路的 rank 去执行 `force_unlink`。

#### 4.1.2 核心流程

所有时间常量在文件头部定义：

- 心跳间隔：\( t_{\text{interval}} = 5 \) 秒
- 超时判定：\( t_{\text{last}} + 60 < t_{\text{now}} \) 即认定对端死亡

一次完整的心跳周期（伪代码）：

```text
每 5 秒（P 侧 rank0 线程 prefill_heartbeat_thread）：
    检查 remote_hb_info 中每个 D cluster 的最后心跳时间
    对超时者：
        解码 cluster_id 得到 ip:port
        若 port == 0：自己直接 force_unlink(cluster_id)
        否则：经 IPC 发送 cluster_id 给持有链路的 rank
    向 PUB 端口(15566) 发布 "prefill_hb:<host_cluster_id>"

每 5 秒（D 侧每个 worker 线程 decode_worker_hb_<local_rank>）：
    对每条已注册链路：
        若尚无 SUB 套接字：connect tcp://<P_ip>:15566 并订阅前缀 "prefill_hb"
        NOBLOCK 尝试收心跳 → 成功则刷新时间戳
        超过 60 秒未收到 → close_link(...) 并清理对应 PUSH 套接字
    对每条 PUSH 连接(5568)：发送 ["decode_hb:<自己的 cluster_id>"]
```

#### 4.1.3 源码精读

**（1）端口与时间常量。** [components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py:L47-L54](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L47-L54) 定义了四个全局常量：`BLOCK_RELEASE_DELAY`（600 秒兜底释放）、`LLMDATADIST_BASE_PORT = 15567`、`HEARTBEAT_INTERVAL = 5`、`CLUSTER_HEARTBEAT_TIMEOUT = 60`，以及 IPC 路径前缀 `ipc:///tmp/prefill_llmdatadist_connector_ipc`。注意 15566 并没有独立常量——它是「15567 减 1」推导出来的。

**（2）5568 端口的解析优先级。** [components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py:L146-L153](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L146-L153) 用 `get_config_from_dict_or_env` 解析 ZMQ 端口：先查环境变量 `VLLM_LLMDATADIST_ZMQ_PORT`，再查 `kv_transfer_config.kv_port`，都没有则用默认 `"5568"`；最后加上 `dp_rank` 偏移。解析函数在 [components/omni-npu/src/omni_npu/connector/utils.py:L240-L256](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/utils.py#L240-L256)，其 L251-L253 明确「ENV 优先于配置参数」。注释说明该变量是为了解决**同机多 P 部署**的 ZMQ 端口冲突，一般场景不要设置。

**（3）P 侧 rank0 的两个监听端口。** [components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py:L351-L370](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L351-L370)：`PrefillConnectorWorker.__init__` 中，只有 `rank == 0 and pp_rank == 0` 的进程才 `bind` 两个 TCP 端口——`PULL` 套接字绑 `tcp://<P_ip>:5568+dp_rank+pp_rank`（回执 + D 心跳入口，日志打印 `ConnectWorker bind ...`），`PUB` 套接字绑心跳端口 \( 15567 - 1 = 15566 \)（日志 `Prefill create heartbeat publisher`）。其余 rank（L368-L370）走 `heartbeat_server_func`，在 [L419-L428](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L419-L428) 中 `bind` 一个 IPC 地址 `ipc:///tmp/prefill_llmdatadist_connector_ipc_<rank>`，等待 rank0 的超时通知。

**（4）P 侧心跳发送与超时处理。** [components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py:L382-L417](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L382-L417) 是 rank0 的心跳主循环：先淘汰 `remote_hb_info` 里超时 60 秒的 D cluster（L390-L393）；再对每个死者解码 cluster_id（L397），`port == 0` 直接 `force_unlink`，否则把 cluster_id 字符串经 IPC `PUSH` 给 `_<port>` 号 rank（L399-L412）——**cluster_id 的 port 字段就是目标 rank 号**，1P1D 形态下 D 侧 16 个全局 rank 0～15 与 P 侧 TP16 的 rank 0～15 恰好同号，IPC 寻址得以对上。最后 L416 向 PUB 端口广播 `prefill_hb:<host_cluster_id>`。

**（5）P 侧接收端：一条 PULL 区分两种消息。** [components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py:L487-L509](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L487-L509)：`get_pulled_kv_req_list` 线程轮询 5568 端口，收到的 JSON 列表若首元素以 `decode_hb:` 开头就刷新 `remote_hb_info[cluster_id]` 的时间戳（L494-L502），否则当作「KV 已拉取」的请求回执加入 `receive_req_list`（L505-L507）——回执与心跳复用同一条连接。

**（6）D 侧心跳线程：SUB 订阅 + 超时 close_link。** [components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py:L696-L743](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L696-L743)：对每条已注册链路，L706-L707 计算订阅地址 \( \text{tcp://}P_{ip}:15566 + \text{prefill\_dp\_rank} \)；首次遇到时 L724-L728 创建 `SUB` 套接字 `connect` 并订阅前缀 `"prefill_hb"`（日志 `subscribe to ...`）；L711-L720 用 `NOBLOCK` 收包刷新时间戳，60 秒未收到则 `close_link` 并记入清理名单；L738-L742 向所有 `PUSH` 连接发送 `["decode_hb:<cluster_id>"]`。README 矩阵第 3 行「row 2 port + prefill dp_rank」说的就是这个加法——本仓库部署形态下 prefill 的 DP 恒为 1（见 4.3.3 的源码证据），所以实际订阅端口总是 15566。

**（7）D 侧 PUSH 连接的来源。** [components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py:L913-L928](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L913-L928)：`_send_pulled_kv_req_list` 按需创建 `PUSH` 套接字并 `connect` 到目标地址（日志 `create new socket path:...`）。目标地址来自请求携带的 `kv_transfer_params["remote_host_ip"]`，它在 P 侧调度器生成：[L303-L329](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L303-L329) 的 L324 拼出 `f"tcp://{self.host_ip}:{self.host_port}"`，即 `tcp://<P_ip>:5568`。

**（8）矩阵第 5 行：D 节点内部的 sched-pub IPC。** [components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py:L528-L532](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L528-L532)：当 `additional_config.async_pull_kv` 为真时，D 侧调度器 `bind` 一条写死的 IPC 地址 `ipc:///tmp/sched-pub-<kv_rank>-<dp_rank_local>`，随后在 [L622-L628](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L622-L628) 把 `pickle` 序列化的 metadata 经 PUB 发给 worker，绕过 vLLM 正常调度循环走「快路径」。**注意（待确认）**：worker 侧消费该 IPC 的线程目标是 `self.on_fast_path_req`（[L683-L687](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L683-L687)），但该方法在 omni-npu 当前代码里搜不到定义——同名方法存在于 omni-cache 的 `omni_cache/connector/decode/worker.py:267`。因此在 omni-npu 里开启 `async_pull_kv` 很可能直接抛 `AttributeError`，该特性疑似处于向 omni-cache 迁移的中间状态。本仓库的 ansible 模板未开启它，README 第 5 行仅作机制记录。

对照官方矩阵（[components/omni-npu/README.md:L72-L79](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/README.md#L72-L79)），控制面 5 行的监听方/发起方归纳如下（以 1P1D、P 单机 TP16、D 单机 16 个 DP server 为例）：

| 矩阵行 | 协议 | 地址 | 谁监听（bind） | 谁发起（connect） | 触发时机 |
| --- | --- | --- | --- | --- | --- |
| 1 | ZMQ PUSH/PULL TCP | `tcp://<P_ip>:5568` | P 的 rank0（加 dp/pp 偏移） | D 侧 worker | KV 拉取完成回执；每 5 秒心跳；请求被 ABORT |
| 2 | ZMQ PUB TCP | `tcp://<P_ip>:15566` | P 的 rank0 | ——（广播方） | P 每 5 秒发布 `prefill_hb` |
| 3 | ZMQ SUB TCP | `tcp://<P_ip>:15566+dp` | —— | D 侧 worker 订阅 | D 心跳线程每 5 秒轮询 |
| 4 | ZMQ PUSH/PULL IPC | `ipc:///tmp/prefill_llmdatadist_connector_ipc_<rank>` | P 的非 0 rank | P 的 rank0 | P 检测到远端心跳超时 |
| 5 | ZMQ PUB IPC | `ipc:///tmp/sched-pub-<kv_rank>-<dp_local>` | D 的调度器 | D 的 worker（待确认） | `async_pull_kv=True` 时的快路径 |

#### 4.1.4 代码实践

**实践目标**：不启动服务，仅凭源码推算 1P1D 形态下两台机器各自应出现的 ZMQ 端点，形成「预期清单」，为第 5 节的综合实践做准备。

**操作步骤**：

1. 打开 [tools/scripts/pd_run.sh:L14](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/scripts/pd_run.sh#L14)，确认部署链路注入的 `VLLM_LLMDATADIST_ZMQ_PORT` 默认值是字符串 `"5568"`（L292 将其 export 进引擎环境）。
2. 阅读上文精读点（2）（3）（6），写出两份清单：
   - P 节点：TCP LISTEN `5568`（仅 rank0 进程）、TCP LISTEN `15566`（仅 rank0 进程）、本机 IPC 文件 `/tmp/prefill_llmdatadist_connector_ipc_1` ～ `_15`。
   - D 节点：**没有任何上述 TCP 监听**——SUB 和 PUSH 都是 connect 方；只有可选的 `/tmp/sched-pub-*` IPC。
3. 回答：为什么 D 节点上用 `ss -tlnp` 找不到 5568 和 15566 的 LISTEN？

**需要观察的现象 / 预期结果**：清单中每个端点都能对应到一条源码 bind 语句；D 节点无 LISTEN 是正常现象而非故障。步骤 3 的答案：ZMQ 里只有 bind 方监听端口，D 侧全部是 connect 方，其 socket 状态是 ESTABLISHED 而非 LISTEN。实际部署中的核对需在真机上进行（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：P → D 心跳用 PUB/SUB，D → P 心跳为什么复用 5568 的 PUSH/PULL，而不是再开一个端口？

**参考答案**：D → P 的心跳和回执都是「D 主动发、P 的 rank0 唯一接收」的单向小消息，PUSH/PULL 语义完全吻合；复用一条连接省掉一次握手和端口占用，接收端用 `decode_hb:` 前缀即可区分消息类型（L487-L509）。而 P → D 方向是一对多广播且 D 侧按前缀过滤，正适合 PUB/SUB 的一对多分发。

**练习 2**：如果把 `HEARTBEAT_INTERVAL` 从 5 秒改成 60 秒，会发生什么？

**参考答案**：超时阈值仍是 60 秒（`CLUSTER_HEARTBEAT_TIMEOUT` 是独立常量），心跳间隔接近阈值后，一次普通网络抖动或线程调度延迟就会造成误判，触发 `close_link`/`force_unlink` 误清活链路；随后下次拉取 KV 又要重新 `register_link`，表现为周期性的传输卡顿。工程上间隔应远小于阈值（当前 5 vs 60，有 12 倍余量）。

**练习 3**：矩阵第 4 行的 IPC 通知里，rank0 是怎么知道该通知哪个 rank 的？

**参考答案**：超时的远端是 D 侧某个进程，其 `cluster_id` 的 port 字段编码了该进程的全局 rank（见 4.2.3 的位布局；D 侧构造 cluster_id 时 `port_offset = self.rank`）。rank0 解码 cluster_id 得到 port，直接拼出 IPC 地址 `..._ipc_<port>` 通知同号的本机 rank 去执行 `force_unlink`；port 为 0 时即 rank0 自己的链路，就地 unlink（L395-L412）。

### 4.2 RoCE 传输端口：llm_datadist 数据面

#### 4.2.1 概念说明

控制面谈妥之后，真正的 KV 张量走 llm_datadist 引擎。它的端口模型很简单：

- **P 侧监听**：每个 worker 进程在 `15567 + local_rank` 上监听，等待 D 侧发起建链。TP16 的 P 节点上会看到 15567～15582 共 16 个监听端口。
- **D 侧不监听**：D 的 `LLMDataDistManager` 以 `host_port = 0` 构造（端口 0 即「不监听」），它永远是建链的发起方。

「谁监听、谁发起」由 PD 分离的请求方向决定：请求总是先到 P 做 prefill，D 收到请求后才知道要去哪个 P 拉 KV，所以建链动作天然发生在 D 侧（u4-l2 讲过的惰性建链：首次 `pull_kv` 才 `register_link`）。

每个 llm_datadist 实例有一个 int64 的 **cluster_id** 充当「地址牌」。它把 ip、port、tp_size、pp_size 压缩进一个整数，控制面消息（心跳、force_unlink）里传的都是它，收到后用位运算反向解出地址。理解它的位布局，是手工核对链路日志的前提。

#### 4.2.2 核心流程

cluster_id 的编码布局（`ip_port_to_int`）：

\[ \text{cluster\_id} = (\text{ip\_int} \ll 32) \;|\; (\text{port} \ll 16) \;|\; ((\text{tp\_size}-1) \ll 8) \;|\; (\text{pp\_size}-1) \]

其中 ip_int 是 IPv4 地址按大端的 32 位整数。port 字段的取值规则：

- P 侧：\( \text{port} = 15567 + \text{local\_rank} \)（每个进程一个独立端口，也是它的监听端口）；
- D 侧：\( \text{port} = 0 + \text{rank} \)（全局 rank，不监听、仅作身份编号）。

一次 KV 传输的端口级时序：

```text
D 侧首次为某请求拉 KV：
    从 kv_transfer_params 取出 P 的 host_cluster_id（含时间戳的元组）
    register_link：
        对元组中每个 ip：解码 cluster_id 得 remote ip:port
        构造 LLMClusterInfo（append_remote_ip_info(P_ip, 15567+local_rank)）
        data_dist_engine.link_clusters(clusters, timeout=5000ms)
    pull_blocks 经 RoCE 链路搬运 KV 张量
P 侧：引擎初始化即 listen 在 ip:15567+local_rank，被动接受上述连接
```

#### 4.2.3 源码精读

**（1）cluster_id 与端口偏移。** [components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py:L92-L101](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L92-L101)：注释写明「Prefill uses local_rank, Decode uses rank for cluster_id」，P 侧 `port_offset = local_rank`、D 侧 `port_offset = rank`，随后 `ip_port_to_int(f"{ip}:{host_port + port_offset}", tp_size, pp_size)` 生成 cluster_id。

**（2）编码与解码函数。** [components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py:L708-L729](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L708-L729) 的 `ip_port_to_int` 按上面的公式打包（L728）；反向解码在 [L565-L578](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L565-L578) 的 `cluster_id_to_ip_port`：低 8 位加 1 还原 pp_size，8～16 位加 1 还原 tp_size，16～32 位取 port，高 32 位经 `inet_ntoa` 还原 IP。两个函数严格互逆。

**（3）P 侧监听地址。** [components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py:L268-L291](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L268-L291)：`_init_llm_data_dist` 中，只有 prefill 分支设置 `listen_ip_info = f"{ip}:{15567 + local_rank}"`（L279-L282），并设置 `sync_kv_timeout = SYNC_KV_TIMEOUT`（5000ms，L272-L273）防止拉取超时。D 侧构造 `LLMDataDistManager` 时传入 `host_port=0`（[llmdatadist_connector_v1.py:L662](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L662)），不产生监听。

**（4）建链时远端端口的来源。** [components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py:L304-L322](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L304-L322)：`register_link` 对每个 P 侧 cluster 解码出 `remote_host_ip, port`（即 `15567 + local_rank`），`append_remote_ip_info` 填入 `LLMClusterInfo` 后调用 `link_clusters(clusters, timeout=LINK_TIMEOUT)`；建链失败直接抛异常。成功则登记进 `registered_link_infos`（L319-L321），这正是心跳线程轮询的那张表。

**（5）矩阵第 6 行原文。** [components/omni-npu/README.md:L79](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/README.md#L79)：地址 `<prefill_ip>:<port + prefill_local_rank>`、端口 `LLMDATADIST_BASE_PORT`（默认 15567）+ local_rank、触发时机「Decode 首次 pull_kv → register_link(link_clusters) → pull_blocks；prefill 经 listen_ip_info 监听」——与上述源码一一对应。

**（6）建链超时与容错码。** [components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py:L59-L70](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L59-L70)：`SYNC_KV_TIMEOUT = LINK_TIMEOUT = 5000ms`；`RETRYABLE_CODES` 列出可重试的 llm_datadist 错误码（建链进行中、设备 OOM、超时、链路忙等），配合 u4-l2 讲过的「可重试→重建链路→心跳清尸」三层容错。

#### 4.2.4 代码实践

**实践目标**：手工复现 cluster_id 的编解码，做到能看懂日志里的 int64「地址牌」。本实践只需一台装有 Python 3 的机器（无需 NPU）。

**操作步骤**：

1. 把 `ip_port_to_int` 的位布局抄成一个独立脚本（示例代码，逻辑照抄源码 L714-L728）：

   ```python
   # 示例代码：复现 omni-npu 的 cluster_id 编码（源码见 llmdatadist_manager_v1.py:708）
   import socket, struct
   def ip_port_to_int(ip_port, tp_size, pp_size):
       ip, port_str = ip_port.split(':')
       return ((struct.unpack('!I', socket.inet_aton(ip))[0]) << 32) \
              | (int(port_str) << 16) | ((tp_size - 1) << 8) | (pp_size - 1)
   def int_to_ip_port(cid):
       return (socket.inet_ntoa(struct.pack('!I', (cid >> 32) & 0xFFFFFFFF)),
               (cid >> 16) & 0xFFFF, ((cid >> 8) & 0xFF) + 1, (cid & 0xFF) + 1)
   ```

2. 用你环境里的 P 节点 IP 模拟：`ip_port_to_int("192.168.1.10:15567", tp_size=16, pp_size=1)`，打印 int64 值；再调用 `int_to_ip_port` 验证往返一致。
3. 推算 1P1D 下 P 节点（TP16）16 个进程的监听端口序列，以及 D 侧 16 个进程的 cluster_id port 字段（提示：D 用 `rank` 不用 `local_rank`）。

**需要观察的现象 / 预期结果**：编解码往返还原出相同的 ip/port/tp/pp；P 侧监听端口为 15567～15582 连续 16 个；D 侧 cluster_id 的 port 字段为 0～15。步骤 2 的结果可与真机日志 `init ... success, cluster_id=...`（[llmdatadist_manager_v1.py:L289](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L289) 打印）对照（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 P 侧监听端口用 `local_rank` 偏移，而 cluster_id 里 D 侧用全局 `rank`？

**参考答案**：P 侧的偏移量本质是「同机第几个进程」——同一台机器上 16 个进程必须各占一个端口才能被分别建链，所以用 `local_rank`（0～15 对应 15567～15582）。D 侧不监听，port 字段只需充当进程身份编号供控制面寻址（心跳超时时 rank0 靠它定位 IPC 通知目标），全局 `rank` 能在多机 D 实例里保持唯一，故用它。

**练习 2**：`ip_port_to_int` 要求 `1 <= tp_size <= 256`、`0 <= port <= 65535`，为什么？

**参考答案**：位布局总共 64 位：IP 占 32 位、port 占 16 位、tp/pp 各占 8 位（存的是「减 1」的值，所以上限 256）。port 上限 65535 正是 16 位能表示的最大值；这也解释了为什么 base port 选在 15567——16 个进程加偏移后仍远离 65535，且 15566（心跳）与 5568（回执）互不重叠。

**练习 3**：矩阵第 6 行说建链发生在「Decode 首次 pull_kv」，从端口角度怎么印证？

**参考答案**：D 侧的 `LLMClusterInfo` 里 remote 地址来自 P 的 cluster_id（含 15567+local_rank 的端口），`link_clusters` 是 D 发起的主动连接；P 侧引擎一初始化就把 `listen_ip_info` 设为 `ip:15567+local_rank` 开始被动监听。所以在 P 节点 `ss -tnp` 上会看到大量由 D 节点 IP 发起的、落在 15567～15582 上的 ESTABLISHED 连接，而这些连接在首次拉 KV 之前不存在（惰性建链）。

### 4.3 IP 发现：默认路由与三级回退

#### 4.3.1 概念说明

端口解决了「第几个门牌」，IP 解决「在哪条街」。本模块回答两个问题：进程怎么知道自己的 IP，以及 D 怎么知道 P 的 IP。

**本机 IP 发现**用的是 2.4 节介绍的「UDP connect 探路」技巧：不发包、只查路由。优点是零配置；代价是多网卡机器上结果取决于默认路由——如果你希望 KV 走专用高速网卡（RoCE 网段），而默认路由指向业务网卡，发现的 IP 就不在 RoCE 网段里，建链会失败或绕路。这与 u1-l5 讲过的 `HCCL_SOCKET_IFNAME` 问题同源：**多机多网卡环境下，「哪块网卡」永远是要显式确认的第一件事**。

**P 侧 IP 列表**（用于构造 `host_cluster_id`，即「一个 TP 组跨几台机器」时每台机器各出一个 cluster_id）有三级回退：显式配置 `p_node_list` → Ray 集群自动发现 → 只用本机 IP。本仓库 ansible 部署不装 Ray，实际走的是第三级；理解前两级是为了读懂代码，也是为了知道在 K8s/Ray 环境下行为会变。

#### 4.3.2 核心流程

```text
启动时（两侧相同）：
    local_ip = UDP connect('8.8.8.8:80') 取本地端点 IP   # 默认路由决定
    P 侧额外：worker_ips = p_node_list 配置
                        或 Ray 存活节点 IP（head 在前）
                        或 [local_ip]
    host_cluster_id = (时间戳ms, ip_int_1, ip_int_2, ...)
                      # 时间戳用于识别「P 重启后是新的身份」（u4-l2 已讲）
运行期：
    D 从请求的 kv_transfer_params 里拿到 host_cluster_id → 解码出全部 P 侧 IP
```

#### 4.3.3 源码精读

**（1）默认路由发现本机 IP。** [components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py:L956-L963](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L956-L963)：`get_local_ip` 创建 UDP socket、`connect(('8.8.8.8', 80))`、读 `getsockname()[0]`。manager 里有逐字相同的副本 [llmdatadist_manager_v1.py:L515-L522](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L515-L522)（`_get_local_ip`）。门面类在 [llmdatadist_connector_v1.py:L141-L145](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L141-L145) 用它初始化 `host_ip`。README 的说明只有一句「Local IP is discovered by default route of the OS system」（[components/omni-npu/README.md:L81-L83](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/README.md#L81-L83)），指的就是这里。

**（2）三级回退取 P 侧 IP 列表。** [components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py:L170-L192](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L170-L192)：`_get_worker_ips` 按「config → ray → default」优先级取列表，并保证本机 IP 排在首位——L178-L184 的注释解释了原因：这个列表只用于 prefill 调度器维护 `remote_cluster_id`，而该 ID 必须与「心跳接收者（rank0 worker）」的 IP 一致，rank0 与调度器同机，所以本机 IP 必须是第一个元素。第一级 `_ips_from_config` 在 [L119-L123](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L119-L123)，读 `kv_connector_extra_config` 的 `p_node_list`；第二级 `_ips_from_ray`（L125-L167）连接 Ray GCS 拉存活节点、head 节点排最前。

**（3）带时间戳的身份元组。** [components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py:L107-L117](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L107-L117)：`host_cluster_id = (timestamp_ms, ip_int_1, ...)`，多机 TP 组每个 IP 各编码一个 cluster_id（端口统一用 base 15567，不带 local_rank）。时间戳让 D 侧能识别「同 IP 但重启过的 P」并关闭旧链路（u4-l2 的 `get_real_remote_cluster_ids` 旧键回收逻辑）。

**（4）佐证：prefill 的 DP 恒为 1。** [components/omni-npu/src/omni_npu/connector/utils.py:L182-L192](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/utils.py#L182-L192)：`get_p_start_rank` 开头即断言 `p_dp_size must be 1`。这就是 4.1.3 结论「订阅端口实际恒为 15566、回执端口实际恒为 5568」的源码依据——README 矩阵里的 `+ prefill_dp` 偏移是留给未来多 DP prefill 的通用公式，当前部署形态下恒加 0。

#### 4.3.4 代码实践

**实践目标**：在任意一台 Linux 机器上直观验证「默认路由决定被发现 的 IP」，并据此写一份多网卡检查步骤。本实践无需 NPU 与部署环境。

**操作步骤**：

1. 执行 `ip route get 8.8.8.8`，记下输出中的 `src <IP>`——这就是 `get_local_ip()` 将返回的地址。
2. 用 Python 复现（示例代码，等价于源码 L956-L963）：

   ```bash
   python3 -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('8.8.8.8',80)); print(s.getsockname()[0]); s.close()"
   ```

   确认两步结果一致，且 `tcpdump`/抓包观察不到任何实际发出的报文。
3. 列出机器所有网卡候选：`ip -brief addr`。若存在 RoCE 专用网段网卡而 `src` 不是它，写下结论：需要在系统路由或 `p_node_list` 配置层面纠正，否则 KV 链路会落在错误网卡上。

**需要观察的现象 / 预期结果**：`ip route get` 的 `src` 与 Python 输出完全一致；步骤 2 无报文发出（UDP connect 仅查路由表）。多网卡机器上若 `src` 与期望的 KV 传输网卡不符，即命中本模块描述的风险点（待本地验证：在真实多网卡环境复现一次错误网卡导致的建链失败更佳）。

#### 4.3.5 小练习与答案

**练习 1**：`get_local_ip` 为什么用 `connect` 一个公网地址而不是 `gethostbyname(gethostname())`？

**参考答案**：hostname 解析依赖 `/etc/hosts` 或 DNS，常返回 127.0.0.1 或管理网地址，且与「流量实际从哪块网卡出去」无关；UDP connect 让内核查路由表，返回的正是「去往公网默认网关时使用的源 IP」——这个地址对跨机对端来说是可达的，正适合做监听地址。局限是它只反映默认路由，多网卡时未必是你想要的那张卡。

**练习 2**：`_get_worker_ips` 为什么强制把本机 IP 挪到列表首位？

**参考答案**：注释（L178-L184）写明：该列表的唯一消费方是 prefill 调度器维护的 `remote_cluster_id`，它必须指向「心跳接收者」所在的 IP；心跳接收者是 P 侧 rank0 的 worker，而 rank0 与调度器同进程组同机。列表首位编码出的 cluster_id 恰是 rank0 的地址，保证 D 侧解码后连到正确的心跳/回执端口。

**练习 3**：在 Ray 集群上部署与在 ansible 裸机部署，`host_cluster_id` 有何不同？

**参考答案**：三级回退不同层级生效。裸机（无 Ray、未配 `p_node_list`）时列表只有本机 IP，多机 TP 组时每个进程只知道自己这台机器——这正是本仓库形态。Ray 环境下 `_ips_from_ray` 返回全部存活节点（head 在前），多机 TP 组的身份元组自然完整。若两者都不满足需求，还可以在 `kv_connector_extra_config` 里显式配 `p_node_list` 强制指定。

## 5. 综合实践

**任务**：在运行中的部署上完成「端点矩阵 → 实际网络状态 → 部署文档」的闭环核对。这是本讲的指定实践任务，需要一套已按 u1-l4 拉起的 1P1D BF16 服务。

**步骤**：

1. **准备预期清单**。把 4.1.4 推算的 ZMQ 清单与 4.2.4 推算的 RoCE 清单合并成一张「预期表」：P 节点应 LISTEN 5568、15566、15567～15582；D 节点应无这些 LISTEN，但有指向 P 的 ESTABLISHED 连接；P 节点 `/tmp` 下应有 15 个 `prefill_llmdatadist_connector_ipc_*` 文件。
2. **核对监听端口**。在 P 节点容器内执行：

   ```bash
   ss -tlnp | grep -E ':(5568|15566|1556[7-9]|155[7-8][0-9])\b'
   ```

   逐行记录：端口、进程名/PID、监听地址。对照预期表打勾。
3. **核对连接方向**。在 P 节点执行 `ss -tnp | grep -E ':(1556[7-9]|155[7-8][0-9])'`，确认 ESTABLISHED 连接的对端 IP 全部来自 D 节点；再到 D 节点反向执行，确认本机只有 connect 出去的连接。
4. **核对 IPC 与日志证据链**。`ls -l /tmp/prefill_llmdatadist_connector_ipc_*`；然后在 `LOG_PATH` 的 server 日志中 grep 四个关键串：`ConnectWorker bind`（5568 bind）、`Prefill create heartbeat publisher`（15566 bind）、`subscribe to`（D 侧 SUB）、`linked to`（llm_datadist 建链成功），把每条日志的端口与 `ss` 结果互相对应。
5. **写成部署文档**。按 README 矩阵的六行格式，把你环境中每行的实际 IP、端口、触发进程填成一张新表，注明「bind/由谁 connect」，存入团队部署文档，作为后续防火墙开通与故障排查的基准。

**预期结果**：`ss` 看到的监听端口集合与源码推算完全一致；任何多出的端口或缺失的端口都能定位到对应环节（例如缺 1557x 说明某 worker 未完成 llm_datadist 初始化，去查该 rank 的 server_N.log）。**待本地验证**：本讲义在静态源码环境编写，步骤 2～4 的具体输出需在真机部署上执行确认。

## 6. 本讲小结

- **控制面与数据面分离**：ZMQ 负责回执、双向心跳与超时通知（小消息），llm_datadist 负责 KV 张量的 RoCE 搬运（大流量）；两套链路端口互不重叠。
- **ZMQ 端口三件套**：D→P 回执与心跳复用 PUSH/PULL 5568（P 的 rank0 监听）；P→D 心跳走 PUB/SUB 15566（= 15567 − 1）；P 节点内部超时通知走 IPC `ipc:///tmp/prefill_llmdatadist_connector_ipc_<rank>`。
- **心跳参数**：间隔 5 秒、超时 60 秒；超时后 D 侧 `close_link`、P 侧 `force_unlink`，双向清尸。
- **RoCE 数据面**：P 侧每个进程监听 15567 + local_rank（TP16 即 15567～15582），D 侧端口为 0 不监听、永远是建链发起方；链路在首次 pull_kv 时才惰性建立。
- **cluster_id 是 int64 地址牌**：`ip(32bit) | port(16bit) | tp−1(8bit) | pp−1(8bit)`，P 侧 port 带 15567 偏移、D 侧 port 即全局 rank，控制面消息靠它寻址。
- **IP 靠默认路由发现**：UDP connect 探路零配置但受多网卡影响；P 侧 IP 列表按 `p_node_list` → Ray → 本机三级回退，且本仓库部署形态下 prefill DP 恒为 1，端口偏移实际恒为 0。

## 7. 下一步学习建议

下一讲（u4-l4）将逐参数精读 `pd_run.sh`——本讲看到的 `VLLM_LLMDATADIST_ZMQ_PORT` 正是它注入的，届时会把 ranktable 组网与多 API server 参数补全，拼出完整的拉起命令链路。若你想横向对比「另一套 KV 传输实现」，可提前浏览 u7 单元的 omni-cache：它的 connector 复用了本讲的 ZMQ 心跳设计，并把 KV 卸载到主机内存（其 `omni_cache/connector/decode/worker.py` 中存在 `on_fast_path_req` 的完整定义，可印证 4.1.3 第（8）点的推断）。日常排障时，建议把本讲综合实践产出的端口对照表放在手边：KV 传输类故障的第一步，永远是确认这三组端口（5568/15566/15567 段）的监听与连接状态。
