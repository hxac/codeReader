# PD 分离端到端：Prompt/Decoder 双进程样例

## 1. 本讲目标

本讲是单元七的收官实战课。前面两讲分别学习了 HIXL 引擎的 Python 绑定（u7-l1）和 LLM-DataDist 的 Python 接口层（u7-l2），单元六则从上到下讲完了 LLM-DataDist 的内部实现。本讲把视角拉回**用户侧**，通过仓库自带的三组成对样例，把「PD 分离场景下 KV Cache 跨集群传输」的完整流程从头到尾串起来。

学完本讲，你应该能够：

1. 说清 PD 分离架构中 Prompt 集群与 Decoder 集群各自承担什么职责，以及为什么需要在它们之间传 KV Cache。
2. 独立跑通 `prompt_push_cache_and_blocks` + `decoder_pull_cache_and_blocks` 双进程样例，并理解每一步接口调用在两端如何配对。
3. 对比 Cache 粒度（连续 batch）与 Blocks 粒度（离散块）两种传输方式在接口与适用场景上的差异。
4. 理解 `switch_roles` 样例中 `SetRole` 角色切换的时序约束。
5. 掌握 `llm.TransferBackend="hixl"` 时 Python 端到端样例的接入方式。

## 2. 前置知识

- **PD 分离（Prompt-Decode Disaggregation）**：大模型推理被拆成两个阶段——Prefill（Prompt）阶段一次性算完输入序列的全部 attention，产出 KV Cache；Decode（Decoder）阶段逐 token 生成。PD 分离架构让两个阶段跑在不同的集群上，各自按负载独立扩缩容。代价是：Decode 开始前必须把 Prefill 算好的 KV Cache 从 Prompt 集群搬到 Decoder 集群，这个搬运正是 LLM-DataDist 的核心用途。
- **Push 与 Pull**：方向相反的两类传输。Push 由持有数据的一端（通常是 Prompt）主动写入对端；Pull 由需要数据的一端（通常是 Decoder）主动从对端读取。KV Cache 传输普遍采用 Pull 模式——Decoder 什么时候准备好、需要哪些层，由 Decoder 自己决定。
- **cluster_id 与 CacheIndex**：每个 LlmDataDist 实例有一个集群号（cluster_id），远端 Cache 用 `CacheIndex{cluster_id, cache_id, batch_index}` 三级寻址（见 u6-l1/u6-l4）。
- **业务自建通知**：LLM-DataDist 没有跨集群的业务通知原语。样例里「对端注册完成没」「对端断链了没」这类同步，C++ 样例用裸 TCP socket 自建控制通道，Python 样例用 torch.distributed（gloo）barrier。这是从 u6-l4 延续下来的一个关键工程事实。
- 建议先回顾 u6-l4（Push/Pull 接口语义）与 u7-l2（Python LLMConfig 与 CacheManager），本讲直接使用这些结论。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `examples/cpp/prompt_push_cache_and_blocks.cpp` | Prompt 侧样例：注册 Cache、建链、PushKvBlocks + PushKvCache、断链通知 |
| `examples/cpp/decoder_pull_cache_and_blocks.cpp` | Decoder 侧样例：注册 Cache、建链、PullKvBlocks + PullKvCache、数据校验 |
| `examples/cpp/prompt_switch_roles.cpp` | 角色切换样例（Prompt 侧）：SetRole 从 kPrompt 切到 kDecoder |
| `examples/cpp/decoder_switch_roles.cpp` | 角色切换样例（Decoder 侧）：Unlink 后 SetRole 从 kDecoder 切回 kPrompt |
| `examples/python/hixl_transfer_backend_sample.py` | Python 端到端样例：`llm.TransferBackend="hixl"` + torch.npu + gloo 同步 |
| `examples/run_example.sh` | 官方冒烟脚本：给出各样例的标准配对方式与启动命令 |
| `examples/README.md` | 环境要求与 device 连通性检查（hccn_tool） |
| `include/llm_datadist/llm_datadist.h` | LlmDataDist 公开接口与选项键定义 |

## 4. 核心概念与源码讲解

### 4.1 PD 分离双进程协作模型

#### 4.1.1 概念说明

样例把 PD 分离浓缩成**两个进程、四个端口、一条链路**的最小可运行模型。两端的一切差异只体现在三处：

1. 构造 `LlmDataDist` 时传入的 cluster_id 与 `LlmRole`；
2. `Initialize` 选项中的监听端口；
3. 传输方向（Push 还是 Pull）。

其余调用序列完全对称。端口分配是一个值得记住的约定：

| 端口 | 用途 | 归属 |
|---|---|---|
| 26000 | Prompt 侧 `llm.ListenIpInfo` 监听端口 | LLM-DataDist 控制面 |
| 26001 | Decoder 侧 `llm.ListenIpInfo` 监听端口 | LLM-DataDist 控制面 |
| 26002 | Prompt 侧业务控制通道（接收 decoder 的 unlink-done 信号） | 业务自建 |
| 26003 | Decoder 侧业务控制通道（接收 prompt 的 unlink-done 信号） | 业务自建 |

前两个端口由 LLM-DataDist 自己使用（建链握手，见 u6-l2）；后两个是**样例业务代码自建的裸 TCP 通道**，用于弥补「断链后如何告诉对端我退出了」这一通知缺口。

#### 4.1.2 核心流程

两端的主流程（编号与源码注释一致）：

```text
Prompt 进程                                Decoder 进程
────────────────                          ────────────────
1. Initialize (cluster 0, kPrompt,        1. Initialize (cluster 1, kDecoder,
   监听 ip:26000)                            监听 ip:26001)
2. aclrtMalloc × 4 + iota 填充数据        2. aclrtMalloc × 4（不填数据）
   RegisterKvCache → cache_id               RegisterKvCache → cache_id
3. sleep 5s（等 decoder 注册）   ←— 时间窗 —→  3. sleep 5s（等 prompt 写完 cache）
4. LinkLlmClusters(远端=cluster 1)  ←— 建链握手 —→ LinkLlmClusters(远端=cluster 0)
5. PushKvBlocks / PushKvCache      —— 数据面 —→ PullKvBlocks / PullKvCache
                                             （+ CheckBuffers 校验）
6. UnlinkLlmClusters                          6. UnlinkLlmClusters
   NotifyUnlinkDone(对端 26003)  ——TCP'1'——→  NotifyUnlinkDone(对端 26002)
7. UnregisterKvCache / aclrtFree             7. UnregisterKvCache / aclrtFree
   Finalize                                   Finalize
```

注意两处 `sleep 5s`：真实业务里「对端注册好了没」应当用可靠通知（生产系统由推理框架的调度面完成），样例为求自包含用了定时等待——这是样例与生产代码的第一个显著差别。

#### 4.1.3 源码精读

先看 Prompt 侧的常量与角色声明：

- [examples/cpp/prompt_push_cache_and_blocks.cpp:L24-L42](https://github.com/gitcode.com/cann/hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/prompt_push_cache_and_blocks.cpp#L24-L42)：定义 26000/26001/26003 三个端口、cluster_id（Prompt=0）、4 个 shape 为 {8,16} 的 int32 tensor（每个 128 个元素）、以及业务控制通道的重试参数（600 次 × 100ms）。
- [examples/cpp/prompt_push_cache_and_blocks.cpp:L233-L238](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/prompt_push_cache_and_blocks.cpp#L233-L238)：构造 `LlmDataDist llm_datadist(kPromptClusterId, LlmRole::kPrompt)`——cluster_id 与角色在构造时一次性给定。

Decoder 侧与之镜像：

- [examples/cpp/decoder_pull_cache_and_blocks.cpp:L24-L42](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/decoder_pull_cache_and_blocks.cpp#L24-L42)：多定义了 `kPromptControlPort = 26002`（业务控制通道对端端口），cluster_id 为 1。
- [examples/cpp/decoder_pull_cache_and_blocks.cpp:L264-L266](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/decoder_pull_cache_and_blocks.cpp#L264-L266)：构造 `LlmDataDist llm_datadist(kDecoderClusterId, LlmRole::kDecoder)`。

业务自建通知通道的实现：

- [examples/cpp/prompt_push_cache_and_blocks.cpp:L133-L163](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/prompt_push_cache_and_blocks.cpp#L133-L163)：`NotifyUnlinkDone` 用裸 TCP 向对端控制端口反复 connect（最多 600 次、每次间隔 100ms），连上后只发 1 个字节 `'1'`。对端收到即知道「这一轮结束了，可以安全退出」。这就是业务通知的全部实现——LLM-DataDist 不提供这件事。

官方脚本给出的标准配对方式（注意它如何组织双进程）：

- [examples/run_example.sh:L242-L244](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/run_example.sh#L242-L244)：跨机冒烟把 `prompt_pull_cache_and_blocks` 与 `decoder_pull_cache_and_blocks` 配对、`prompt_push_cache_and_blocks` 与 `decoder_push_cache_and_blocks` 配对（还有一对 switch_roles），用 `run_pair` 同时拉起两个进程并分别收集日志。
- [examples/run_example.sh:L307-L310](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/run_example.sh#L307-L310)：单机（127.0.0.1）版本的同一组命令。

需要说明：仓库里 push/pull 各有一个 Prompt 版和一个 Decoder 版，源码结构完全同构。本讲以 `prompt_push_...`（Push 方向的主动端）与 `decoder_pull_...`（Pull 方向的主动端）为代表精读——把这两个文件读懂，另外两个文件只是同一模板的变体。

#### 4.1.4 代码实践

1. **实践目标**：在不运行的情况下，从源码推出双进程样例的端口依赖关系。
2. **操作步骤**：
   - 打开 `prompt_push_cache_and_blocks.cpp` 与 `decoder_pull_cache_and_blocks.cpp`，列出每个文件用到的全部端口号及其用途；
   - 检查两端的 `Link` 函数：Prompt 的 `remote_ip_infos` 端口是多少？Decoder 的呢？确认它们指向对端的监听端口。
3. **需要观察的现象**：Prompt 的 Link 填 `kDecoderListenPort`（26001），Decoder 的 Link 填 `kPromptListenPort`（26000），即**各自连向对端的监听端口**，而本端 `local_ip_infos` 填本端监听端口。
4. **预期结果**：得到一张 4 端口表（如 4.1.1 所示）；若两端把远端端口填成了自己的监听端口，建链会一直等到 5000ms 超时失败。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `NotifyUnlinkDone` 整个删掉，样例还能跑通吗？会出现什么现象？

**答案**：大概率仍能跑通，但变得脆弱。这个通知解决的是退出同步问题：Prompt 侧 Unlink 完成后如果立刻 `Finalize` 退出，Decoder 侧可能还在做后续清理或校验，对端进程消失可能导致其打印错误甚至异常退出。样例中 Decoder 收到 '1' 才放心走第 7 步 Finalize（见 `RunDecoderSample` 中 [decoder_pull_cache_and_blocks.cpp:L306-L313](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/decoder_pull_cache_and_blocks.cpp#L306-L313)）。

**练习 2**：两端各 sleep 5 秒的作用是什么？为什么说这不是生产可用的做法？

**答案**：Prompt 等 Decoder 完成 RegisterKvCache、Decoder 等 Prompt 完成数据准备。固定 sleep 无法保证对端真的就绪（慢机器上 5 秒可能不够），也浪费时间（快机器上等了多余时间）；生产中应由调度面或消息通知替代（Python 样例用 gloo barrier 就是改进版）。

### 4.2 端到端时序：初始化、注册、建链、断链与清理

#### 4.2.1 概念说明

这一模块逐段精读两份样例的主干调用。所有接口都是 u6-l1 讲过的 `LlmDataDist` 公开 API，这里关注的是**它们在双进程里如何配对出现**。

#### 4.2.2 核心流程

一次完整生命周期的接口合同（任何一步失败都跳到统一的 `Finalize` 清理函数）：

```text
Initialize(options)                       // llm.DeviceId + llm.ListenIpInfo (+可选 llm.TransferBackend/llm.LocalCommRes)
  → RegisterKvCache(cache_desc, addrs, {}, &cache_id)   // 4 块 device 内存 → cache_id
  → LinkLlmClusters(clusters, rets, 5000)               // 与对端建链，5s 超时
  → Push*/Pull*                                          // 数据面传输
  → UnlinkLlmClusters(clusters, rets, 5000)             // 断链
  → UnregisterKvCache(cache_id)
  → Finalize()
```

`Finalize` 清理函数内部按「先断链、再注销 Cache、再释放 ACL 内存、最后 Finalize」的顺序执行，且用 `linked`、`cache_id > 0` 两个标志避免重复清理——这是把 u6-l4 讲过的「顺序合同」落成了代码。

#### 4.2.3 源码精读

**初始化与选项**：

- [examples/cpp/prompt_push_cache_and_blocks.cpp:L67-L89](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/prompt_push_cache_and_blocks.cpp#L67-L89)：`Initialize` 组装选项 map：`OPTION_DEVICE_ID`、`OPTION_LISTEN_IP_INFO`（本机 ip:26000）。两个可选命令行参数：`transfer_backend`（只接受 `"hixl"`，写入 `OPTION_TRANSFER_BACKEND`，即 `llm.TransferBackend`）与 `local_comm_res`（写入 `OPTION_LOCAL_COMM_RES`，透传给 HIXL 引擎的通信资源配置）。不传 transfer_backend 时走默认 HCCL 后端。选项键常量定义在 [include/llm_datadist/llm_datadist.h:L34-L41](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L34-L41)。

**注册 KV Cache**：

- [examples/cpp/prompt_push_cache_and_blocks.cpp:L240-L265](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/prompt_push_cache_and_blocks.cpp#L240-L265)：`CacheDesc` 声明 4 个 int32、shape {8,16} 的 tensor；循环 `aclrtMalloc` + `aclrtMemcpy`（iota 序列 0..127 填充 device 内存，供对端校验），把 4 个地址打包成 `tensor_addrs`，`RegisterKvCache` 返回 `cache_id`。Decoder 侧对应 [decoder_pull_cache_and_blocks.cpp:L234-L259](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/decoder_pull_cache_and_blocks.cpp#L234-L259)，区别只是不填初始数据。接口签名为 [include/llm_datadist/llm_datadist.h:L311-L312](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L311-L312)。

**建链**：

- [examples/cpp/prompt_push_cache_and_blocks.cpp:L91-L112](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/prompt_push_cache_and_blocks.cpp#L91-L112)：组装一个 `ClusterInfo`：`remote_cluster_id = 1`（Decoder 的集群号），local/remote IpInfo 分别指向本端与对端监听端口，`LinkLlmClusters(clusters, rets, 5000)` 超时 5 秒。Decoder 侧 [decoder_pull_cache_and_blocks.cpp:L91-L112](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/decoder_pull_cache_and_blocks.cpp#L91-L112) 镜像地填 `remote_cluster_id = 0`。两端都调用 Link——内部握手在 u6-l2 已讲过（kConnect/kDisconnect/kStatus 消息 + LLMExchangeInfo 交换）。

**断链与顺序清理**：

- [examples/cpp/prompt_push_cache_and_blocks.cpp:L206-L228](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/prompt_push_cache_and_blocks.cpp#L206-L228)：`Finalize` 清理函数，按 linked → cache → buffers → Finalize 的顺序兜底；主流程 [L288-L301](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/prompt_push_cache_and_blocks.cpp#L288-L301) 中每一步失败都会带着已完成的步骤状态进入清理。

#### 4.2.4 代码实践

1. **实践目标**：跑通双进程样例并观察端到端时序。
2. **操作步骤**（需已按 u1-l2 用 `bash build.sh --examples` 构建，产物在 `build/examples/cpp`）：
   1. 按 [examples/README.md:L37-L59](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/README.md#L37-L59) 用 `hccn_tool` 确认两个 device 互通（A3 环境注意单卡双 die 不互通，参照 README 的提示选 device id）；
   2. 终端 A 启动 Prompt：`./prompt_push_cache_and_blocks <dev_id> <local_ip> <remote_ip>`；
   3. 终端 B 启动 Decoder：`./decoder_pull_cache_and_blocks <dev_id> <local_ip> <remote_ip>`（单机双卡时 ip 均填本机 ip）；
   4. 也可用 `hixl` 后端重跑一遍：两个进程的第 4 个参数都传 `hixl`。
3. **需要观察的现象**：两端依次打印 Initialize → RegisterKvCache → LinkLlmClusters → Push*/Pull* → Unlink → UnregisterKvCache → Finalize 的 `[INFO]` 日志；Decoder 侧额外打印 `CheckBuffers success`。
4. **预期结果**：两个进程退出码均为 0，Decoder 校验通过。两种后端（缺省 HCCL 与 hixl）均应跑通。若无法在昇腾环境执行，记录阻塞原因，时序结论以上述源码行号为准（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么两端都要调用 `LinkLlmClusters`？只让 Prompt 一端调用行不行？

**答案**：Link 内部是两端对称的握手（u6-l2 的 ExchangeInfoProcess）：双方交换 cache table 地址、通信资源等并各自创建 CommEntity。只有一端调用时，另一端的监听 daemon 也能完成被动侧握手（Link 请求经控制面到达对端），但对端不会有本地 Link 返回值；样例为拿到各自的状态与 `rets`，两端都显式调用。实际上两端 Link 描述的是同一条链路（remote_cluster_id 互指），重复对同一集群 Link 会得到 LLM_ALREADY_LINK（u6-l2）。

**练习 2**：样例把 `transfer_backend` 参数做成可选，不传时行为有什么不同？

**答案**：不传时选项 map 里没有 `llm.TransferBackend`，`TransferEngineFactory` 走缺省 HCCL 后端（u6-l7）；传 `hixl` 则建链、内存注册、传输全部改走 HixlTransferEngine 适配层，最终落到 HIXL Engine 的 Connect/TransferSync 上。样例的其他代码一行不改——这正是后端可插拔设计的直观体现。

### 4.3 Cache 粒度与 Blocks 粒度传输

#### 4.3.1 概念说明

KV Cache 传输有两种粒度（u6-l4 讲过接口语义，这里看真实调用姿势）：

- **Cache 粒度**（`PushKvCache`/`PullKvCache`）：按 batch 维度搬一段**连续**数据。适合「整批搬走」的场景，如 PD 交接时一次性迁移全部 prefill KV。
- **Blocks 粒度**（`PushKvBlocks`/`PullKvBlocks`）：按 block 编号列表搬**离散**块，`src_blocks[i] → dst_blocks[i]` 按下标配对。适合 page-attention 类内存管理：远端显存碎片化、源与目标块号不连续时，Blocks 模式免去整块搬运与二次搬移。

样例里每个 tensor 是 128 个 int32（shape {8,16}），Decoder 侧按每 block 16 个元素理解，即 8 个 block。

#### 4.3.2 核心流程

Prompt 侧 `PushCache` 的两段式演示：

```text
第一段：Blocks 粒度 —— 逐层推 3 个离散块
  for i in 0..3:                                   # 4 个 tensor 逐个作为"层"
    param.src_layer_range = (i, i)                 # 层区间 = 单层
    param.tensor_num_per_layer = 1
    PushKvBlocks(cache{cache_id}, CacheIndex{1,1},
                 src_blocks={5,6,7}, dst_blocks={5,6,7}, param)

第二段：Cache 簿度 —— 一个调用推连续 batch
  PushKvCache(cache{cache_id}, CacheIndex{1,1,batch=4},
              src_batch_index=4, size=-1,
              param2.tensor_num_per_layer=4)       # 4 个 tensor 视为一层的 4 份张量
```

Decoder 侧 `PullCache` 同样两段：先 `PullKvBlocks(CacheIndex{0,1}, cache, {1,2,3}, {1,2,3})`，再 `PullKvCache(CacheIndex{0,1}, cache, batch=0)`（size 取默认 -1，即整批拉取）。

#### 4.3.3 源码精读

- [examples/cpp/prompt_push_cache_and_blocks.cpp:L165-L204](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/prompt_push_cache_and_blocks.cpp#L165-L204)：`PushCache` 全貌。注意两点：`CacheIndex{cluster_id=1, cache_id=1}` 硬编码指向 Decoder 侧注册的第一个 Cache（两端各自注册的首个 cache_id 都是 1）；Blocks 模式在层区间循环里**每层调一次**，Cache 模式**一次调用**借助 `tensor_num_per_layer=4` 覆盖 4 个 tensor。
- [examples/cpp/decoder_pull_cache_and_blocks.cpp:L183-L208](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/decoder_pull_cache_and_blocks.cpp#L183-L208)：`PullCache`——`PullKvBlocks` 用默认 ext_param（整层），`PullKvCache` 第三参 batch_index=0、size 默认 -1。
- [examples/cpp/decoder_pull_cache_and_blocks.cpp:L164-L181](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/decoder_pull_cache_and_blocks.cpp#L164-L181)：`CheckBuffers` 把 device 内存拷回 host，逐元素对比 iota 期望值——传输正确性的最终裁判。`kTensorBlockElementNum = 16` 把 block 概念落到实处：check_index × 16 就是每个 block 的起始元素下标。
- 接口签名的权威出处：[include/llm_datadist/llm_datadist.h:L250-L252](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L250-L252)（PullKvBlocks）、[L287-L288](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L287-L288)（PushKvCache）、[L299-L301](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L299-L301)（PushKvBlocks）。

#### 4.3.4 代码实践

1. **实践目标**：量化对比 Cache 粒度与 Blocks 粒度的调用开销与耗时。
2. **操作步骤**：
   1. 在本地副本（不是仓库源码）把 `PushCache` 第一段 Blocks 循环临时注释掉，只保留 `PushKvCache`，用 `std::chrono::steady_clock` 包住计时并打印微秒数；
   2. 再反过来：只保留 Blocks 循环，同样计时；
   3. 更有说服力的做法是把 `kTensorSize` 与 `kTensorShape` 调大（例如 shape {8, 4096}），让单次传输达到 MB 级，此时两种粒度的耗时差才有意义；
   4. 双进程各跑 5 次取平均。
3. **需要观察的现象**：数据量很小时（样例默认仅 2KB/tensor），两种粒度耗时都被固定开销（请求下发、标志位轮询、同步等待）主导，差异不明显；数据量增大后，Blocks 逐层多次调用的累计下发次数多于 Cache 单次调用。
4. **预期结果**：得到一张「粒度 × 数据量 → 平均耗时」的小表。具体数值依赖硬件与后端，待本地验证；可结合 u8-l1 的 benchmark 方法论分析。

#### 4.3.5 小练习与答案

**练习 1**：`PushKvBlocks` 的 `src_blocks` 与 `dst_blocks` 长度必须满足什么关系？想让 Prompt 的 block 5 落到 Decoder 的 block 0，参数怎么写？

**答案**：两列表等长且非空（u6-l4 的 Blocks 合同），第 i 个源块对应第 i 个目标块。写 `src_blocks={5}`、`dst_blocks={0}` 即可——块号不必相同，这正是离散管理的价值。

**练习 2**：样例 Cache 模式里 `size=-1` 是什么含义？哪些接口支持正数 size？

**答案**：`-1` 表示传输源 cache 的完整数据（头文件注释见 [llm_datadist.h:L283](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L283)）。PullKvCache 支持正数 size 做部分字节传输（唯一支持部分字节传输的接口，见 u6-l4）；PushKvCache 的 size 仅支持 -1。

### 4.4 角色切换：switch_roles 样例

#### 4.4.1 概念说明

PD 分离集群存在角色互换的现实需求（例如调度器把一批机器从 Prefill 池划给 Decode 池）。`switch_roles` 样例演示 `SetRole` 的正确时序。回顾 u6-l6 的结论：SetRole 的实质是管理监听 daemon 的停启，且**必须在没有链路时调用**（否则返回 LLM_EXIST_LINK）。

#### 4.4.2 核心流程

```text
prompt_switch_roles                     decoder_switch_roles
─────────────────                      ────────────────────
Initialize(kPrompt, 监听 26000)         Initialize(kDecoder)   # 注意：无监听选项
RegisterKvCache + iota 填充             RegisterKvCache
sleep 10s                               sleep 5s
SetRole(kDecoder)   # 无链路，可切      Link(对端 cluster 0)
Link(对端 cluster 1)                    PullKvBlocks/PullKvCache + Check{0..3}
PushKvBlocks/PushKvCache                Unlink
Finalize                                SetRole(kPrompt, 监听 ip:26001)  # 断链后才能切
                                       sleep 30s（等对端 push）
                                       Check{4..7}   # 校验 push 到来的块
                                       Finalize
```

关键点有两个：角色切换发生在 Unlink 之后；切到 kPrompt（要监听）必须通过 options 传入 `OPTION_LISTEN_IP_INFO`，而切到 kDecoder 用空 options 即可——头文件注释明确写了这一点（[llm_datadist.h:L188](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/include/llm_datadist/llm_datadist.h#L188)）。

#### 4.4.3 源码精读

- [examples/cpp/prompt_switch_roles.cpp:L64-L73](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/prompt_switch_roles.cpp#L64-L73)：Prompt 侧 `SetRole`——切到 kDecoder，options 为空 map。
- [examples/cpp/prompt_switch_roles.cpp:L232-L243](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/prompt_switch_roles.cpp#L232-L243)：主流程第 3-4 步——sleep 后、**Link 之前**完成 SetRole，即保证切换时无链路。
- [examples/cpp/decoder_switch_roles.cpp:L64-L74](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/decoder_switch_roles.cpp#L64-L74)：Decoder 侧 `SetRole` 的重载版本——切到 kPrompt 时把 `OPTION_LISTEN_IP_INFO`（ip:26001）放进 options。
- [examples/cpp/decoder_switch_roles.cpp:L251-L270](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/cpp/decoder_switch_roles.cpp#L251-L270)：Unlink（第 6 步）→ SetRole(kPrompt)（第 7 步）→ sleep 30s 等对端 push → `CheckBuffers({4,5,6,7})` 校验后半个 tensor（正好覆盖 Prompt 推来的 blocks 5,6,7 对应的元素区间 80..127，加上 block 4 的 64..79 属于 PullKvCache 整批拉取覆盖过的区域）。样例刻意让两次校验的分界与两种传输粒度对应：{0,1,2,3} 来自 Pull 阶段，{4,5,6,7} 来自角色切换后收到的 Push。

#### 4.4.4 代码实践

1. **实践目标**：验证「有链路时 SetRole 失败」这一约束。
2. **操作步骤**：
   1. 在本地副本把 `decoder_switch_roles.cpp` 的第 7 步 SetRole 移到第 6 步 Unlink 之前（即保持 linked 状态调用）；
   2. 重新编译该样例（`bash build.sh --examples` 增量构建即可）；
   3. 与 `prompt_switch_roles` 配对运行，观察 SetRole 的返回值。
3. **需要观察的现象**：SetRole 不再打印 `SetRole success`，而是打印错误码（按 u6-l6 的结论应为 LLM_EXIST_LINK）。
4. **预期结果**：确认角色切换的前置条件是链路已断开。具体错误码数值待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`decoder_switch_roles` 的 Initialize 没有传 `OPTION_LISTEN_IP_INFO`，为什么它能作为 Decoder 正常工作？

**答案**：Decoder 角色不需要监听（监听是给「被连接方」用的）。它主动 Link 到 Prompt 的监听端口；等到第 7 步切换为 kPrompt、需要提供监听服务时，才通过 SetRole 的 options 补上 ip:26001。

**练习 2**：角色切换后，双方 FSM（u6-l6）发生了什么？

**答案**：SetRole 触发监听 daemon 停启与 CommEntity 重建；旧实体销毁、新实体建立后，同一个 IDLE→RECEIVE→SEND 三态循环在新角色下重新运行——本例中 Decoder 实体从「Pull 请求方」变为「被 Push 的服务方」，即换方向再跑同一套状态机。

### 4.5 HIXL 传输后端接入：Python 端到端样例

#### 4.5.1 概念说明

`hixl_transfer_backend_sample.py` 是 Python 侧的 PD 分离端到端样例，它同时示范了三件事：

1. **HIXL 后端的启用**：`llm_config.transfer_backend = "hixl"`，经 `generate_options()` 变成 `llm.TransferBackend` 选项——对应 u6-l7 的 HixlTransferEngine 适配层。
2. **torch_npu 内存直接注册**：用 `tensor.data_ptr()` 的整数值作为注册地址（u7-l1 讲过 torch_npu data_ptr 驱动 HIXL 的手法），CacheManager 的 `register_blocks_cache` 完成注册。
3. **gloo barrier 替代 sleep**：用 `torch.distributed` 的 barrier 做「注册完成」「push 完成」「断链完成」三处同步，比 C++ 样例的 sleep 与裸 TCP 更接近生产形态。

有意思的是方向设计：与 C++ 样例相反，这里 **Decoder 是 Push 方、Prompt 是 Pull 方**——说明 Push/Pull 与角色标签并无绑定，`LlmRole` 只是业务语义（u6-l1）。

#### 4.5.2 核心流程

```text
公共：init_process_group(gloo, 2 进程)     # 业务通知面
      LLMDataDist(role, cluster_id) + LLMConfig
      (device_id, transfer_backend="hixl", listen_ip_info=ip:26000/26001)
      datadist.init(options)

Decoder 侧                                Prompt 侧
register_blocks_cache(全 0 tensor)        register_blocks_cache(全 1 tensor)
barrier                                   barrier
link_clusters(对端 cluster 0)             link_clusters(对端 cluster 1)
push_blocks(src=[0,1] → dst=[0,1])        pull_blocks(src=[2] → dst=[2])
barrier                                   barrier
unlink_clusters                           unlink_clusters
barrier                                   barrier
                                          校验两个 tensor 全为 0
unregister_cache / finalize               unregister_cache / finalize
```

Push 与 Pull 并存于同一条链路，最终 Prompt 的两个 tensor 都应变成 Decoder 写入的全 0。

#### 4.5.3 源码精读

- [examples/python/hixl_transfer_backend_sample.py:L56-L72](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/python/hixl_transfer_backend_sample.py#L56-L72)：`init_llm_datadist`——`LLMConfig` 逐属性赋值（device_id、local_comm_res、**transfer_backend="hixl"**、按角色区分 26000/26001 监听端口），`generate_options()` 生成选项 dict 后 `datadist.init(...)`。这正是 u7-l2 讲过的属性风格配置。
- [examples/python/hixl_transfer_backend_sample.py:L40-L53](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/python/hixl_transfer_backend_sample.py#L40-L53)：`init_process_group`——gloo 后端、30 秒超时，为后续 barrier 建立通知面。
- [examples/python/hixl_transfer_backend_sample.py:L143-L177](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/python/hixl_transfer_backend_sample.py#L143-L177)：Decoder 侧——`register_blocks_cache(cache_desc, [addr, addr2], BlocksCacheKey(1, 0))` 注册两个 torch tensor，barrier 后建链、`push_blocks(BlocksCacheKey(0, 0), cache, src_blocks=[0,1], dst_blocks=[0,1])` 推两个 block。
- [examples/python/hixl_transfer_backend_sample.py:L75-L136](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/python/hixl_transfer_backend_sample.py#L75-L136)：Prompt 侧——注册后 `pull_blocks(BlocksCacheKey(1, 0), cache, src_blocks=[2], dst_blocks=[2])`，断链 barrier 后把 tensor 拉回 CPU 与全 0 期望值 `torch.equal` 比对。
- [examples/run_example.sh:L315](https://github.com/gitcode.com/cann-hixl/blob/a5dd1de0a30a0ebbe7e90b84fa9a9bb6c8929477/examples/run_example.sh#L315)：官方启动命令——两进程同机对跑，环境变量 `HCCL_INTRA_ROCE_ENABLE=1`，`--role p` 与 `--role d` 区分角色。

#### 4.5.4 代码实践

1. **实践目标**：跑通 HIXL 后端的 Python 端到端样例，理解 barrier 同步点。
2. **操作步骤**：
   1. 确认已安装 torch_npu 与 llm_datadist whl（u7-l2 的安装方式），并完成 u1-l2 的构建；
   2. 单机双卡执行（与 run_example.sh 一致）：
      ```bash
      cd examples/python
      HCCL_INTRA_ROCE_ENABLE=1 python3 hixl_transfer_backend_sample.py \
          --device_id 0 --role p --local_host_ip 127.0.0.1 --remote_host_ip 127.0.0.1
      HCCL_INTRA_ROCE_ENABLE=1 python3 hixl_transfer_backend_sample.py \
          --device_id 2 --role d --local_host_ip 127.0.0.1 --remote_host_ip 127.0.0.1
      ```
   3. 把 `run_prompt_sample` 中三处 `dist.barrier()` 注释掉一处重跑，观察现象。
3. **需要观察的现象**：正常跑通时日志依次输出 init、register_blocks_cache、link、push/pull、unlink、`check tensor val success`、`[finalize] success`；去掉 barrier 后可能出现 link 超时或校验失败（对端还没注册/还没 push 完就开始校验）。
4. **预期结果**：两个进程正常退出且 Prompt 侧校验通过；去 barrier 的行为差异直观印证「业务通知由业务自建」。若无昇腾环境，以上待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：这个样例里 Decoder push 的 blocks 是 [0,1]，Prompt pull 的 blocks 是 [2]，两者搬运的是同一批数据吗？

**答案**：不是。push 把 Decoder tensor 的 block 0、1 写到 Prompt tensor 的 block 0、1；pull 把 Decoder tensor 的 block 2 写到 Prompt tensor 的 block 2。两次传输覆盖 Prompt tensor 的 block 0、1、2。由于 Decoder 的 tensor 全为 0，Prompt 最终全 0 校验通过——两个操作各自独立，靠 barrier 保证先后完成。

**练习 2**：C++ 样例用 sleep + 裸 TCP，Python 样例用 gloo barrier，它们解决的是同一个什么问题？

**答案**：跨进程的「阶段完成」通知——LLM-DataDist 只管数据面，不提供跨集群业务通知原语。任何真实系统（如 vLLM/SGLang 的 PD 调度）都必须在 LLM-DataDist 之外自建这一层，样例给了两种最简实现。

## 5. 综合实践

**任务：把 PD 分离端到端样例改造成可计时的 Push 性能小实验。**

要求在本地副本（勿改仓库源码）完成以下改造并撰写一页实验报告：

1. 以 `prompt_push_cache_and_blocks.cpp` + `decoder_pull_cache_and_blocks.cpp` 为基线跑通（步骤见 4.2.4）。
2. 把 tensor 规模从 shape {8,16}（512B）提升到 MB 级（例如 shape {8, 1048576}，同时同步修改 Decoder 侧 `kTensorSize`/`kTensorShape` 及 `CheckBuffers` 的遍历范围或改为抽样校验）。
3. 在 Prompt 侧为传输段计时（`std::chrono::steady_clock`），分别测量：
   - 仅 Cache 粒度（单次 `PushKvCache`，size=-1）；
   - 仅 Blocks 粒度（逐层 `PushKvBlocks`，把全部 8 个 block 分 3 组推完）。
4. 各跑 5 次取平均，记录：数据量、后端（HCCL / hixl 第 4 参数切换）、粒度、平均耗时与折算带宽（\( \text{带宽} = \frac{\text{字节数}}{\text{耗时}} \)）。
5. 回答一个问题：随着数据量从 KB 级增到 MB 级，两种粒度的耗时差距是扩大还是缩小？为什么？（提示：固定开销 vs 线性搬运开销的占比。）

预期产物：一张数据表 + 一段结论分析。若本机无昇腾硬件，提交改造后的完整 diff 与「预期观察方案」，并标注待本地验证。

## 6. 本讲小结

- PD 分离双进程样例 = 两个进程 + 四个端口（26000/26001 是 LLM-DataDist 控制面监听，26002/26003 是业务自建的 unlink-done TCP 通道）+ 一条对称建链；两端代码除 cluster_id、角色、监听端口与传输方向外完全同构。
- 端到端接口合同：Initialize → RegisterKvCache → LinkLlmClusters → Push/Pull → UnlinkLlmClusters → UnregisterKvCache → Finalize，任何一步失败进入顺序化兜底清理。
- Cache 粒度（连续 batch、`size=-1` 整批）与 Blocks 粒度（`src_blocks[i]→dst_blocks[i]` 离散配对）是两种正交选择；样例用 `tensor_num_per_layer` 与层区间把 4 个 tensor 组织成两种形态分别演示。
- `SetRole` 角色切换必须在无链路时进行（Unlink 之后），切到 kPrompt 需通过 options 补 `OPTION_LISTEN_IP_INFO`；switch_roles 样例用两次 `CheckBuffers` 分界点验证了切换前后的两个传输阶段。
- LLM-DataDist 不提供跨集群业务通知，样例分别用 sleep + 裸 TCP（C++）与 gloo barrier（Python）自建通知面——这是所有真实 PD 系统必须自己解决的一层。
- `llm.TransferBackend="hixl"` 只是多传一个选项，C++/Python 样例的其他代码一行不改即可切换到 HIXL 传输后端，这是可插拔后端设计最直观的验证。

## 7. 下一步学习建议

本讲完成了单元七，也走完了从公开 API 到端到端样例的全部主线。接下来：

- **单元八（u8-l1）性能基准测试**：本讲综合实践中手工计时的做法，在 `benchmarks/` 里有工业级实现（comm_benchmark 与 kv_benchmark），学习如何系统化测量带宽与时延。
- **u8-l3 Profiling 与统计**：想知道一次 Push 内部时间花在哪，学习两级统计管理器与 prof_api_reg 埋点机制。
- **源码延伸阅读**：`examples/third_parties/` 目录下的对接样例展示了 HIXL/LLM-DataDist 与社区推理框架的真实集成方式，是通向二次开发的最佳参考。
