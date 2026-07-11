# PD 分离部署与 KV 迁移

## 1. 本讲目标

本讲是第七单元（分布式部署与扩展特性）的第一篇，聚焦 LightLLM 的 **PD 分离（Prefill / Decode Disaggregation）** 部署模式，以及该模式下最核心的难题：**把 prefill 节点算出的 KV Cache 跨节点搬运到 decode 节点**。

学完本讲，你应当能够：

1. 说清 PD 分离架构中 prefill 节点、decode 节点、pd_master 三者的角色与请求流转。
2. 读懂 `PDChunckedTransTask` 这一贯穿整条 KV 迁移链路的核心数据结构，理解一段 KV 是如何被「切块 → 装页 → 传输 → 拆页」搬到对端的。
3. 区分 `nixl` 与 `nccl` 两种 KV 传输后端的实现差异与适用场景，知道如何通过环境变量切换。

本讲依赖你已经掌握 [u2-l4（Model Backend 与 RPC）](u2-l4-model-backend-and-rpc.md) 中「每 GPU 一个 backend 进程」的推理后端模型，以及 [u4-l1（KV Cache 内存管理）](u4-l1-kv-cache-memory-manager.md) 中「kv_buffer 是一张 `(layer_num, size, 2*head_num, head_dim)` 的大张量、靠 mem_indexes 索引」的显存模型。这两块是本讲的地基。

## 2. 前置知识

### 2.1 为什么要把 prefill 和 decode 拆开

LLM 推理有两个阶段，二者的算力特征截然不同：

- **prefill（预填充）**：一次性吃进整条 prompt，计算量极大、属于**计算密集型**，吃满 GPU 的矩阵乘法算力。
- **decode（解码）**：逐 token 生成，每步只算一个 token、却要读取全部历史 KV Cache，属于**显存带宽密集型**，算力往往跑不满。

如果把两阶段塞进同一个 GPU 池（即 `normal` 模式），prefill 的大计算会抢占 decode 的显存带宽，二者互相干扰。**PD 分离**的核心思想是：用两套独立的 GPU 节点分别服务 prefill 和 decode，各自把硬件利用率拉满，再用「KV Cache 迁移」把 prefill 节点算出的 KV 搬到 decode 节点，让 decode 节点直接接着生成 token。

### 2.2 搬运 KV 的难点

KV Cache 不是一段连续的、可以直接 `send` 的内存。回顾 u4-l1：每个请求实际拿到的 KV 散落在 `kv_buffer` 的不同 token 槽位里（由 `mem_indexes` 决定），而且 prefill 节点和 decode 节点的 `mem_indexes` 完全不同（两边的内存分配器各自独立分配）。所以搬运 KV 必须解决三个问题：

1. **收集（gather）**：把散落的若干 token 的 KV 从 `kv_buffer` 拢成一段连续的「页（page）」。
2. **传输（transfer）**：跨节点把这一页 GPU 显存搬过去（RDMA 或 NCCL）。
3. **分发（scatter）**：对端把这一页拆开，写回 decode 节点自己的 `kv_buffer` 槽位。

这正是本讲三条主线「PD 分离 / KV 迁移任务 / 传输后端」要回答的。

### 2.3 关键术语速览

| 术语 | 含义 |
|------|------|
| pd_master | PD 分离模式下的「调度中枢」进程，负责把请求路由到一对 prefill+decode 节点 |
| prefill 节点（P 节点） | 执行 prefill、产出 KV Cache 的节点 |
| decode 节点（D 节点） | 接收 KV Cache、执行逐 token decode 的节点 |
| page（页） | 一段固定大小、连续的 KV 缓冲区，是跨节点传输的「集装箱」 |
| agent | 传输后端里的一个端点，名字形如 `{node_id}_{tp_idx}` |
| NIXL | NVIDIA Inference Transfer Library，基于 RDMA 的 GPU 显存跨节点传输库 |
| NCCL | NVIDIA 集合通信库，这里被「借用」做点对点 send/recv |

## 3. 本讲源码地图

本讲涉及的核心源码分三层，正好对应三个最小模块：

| 文件 | 所属层 | 作用 |
|------|--------|------|
| [lightllm/server/pd_io_struct.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/pd_io_struct.py) | 协议层 | 定义 PD 分离所有跨进程/跨节点传递的数据结构，重点是 `PDChunckedTransTask` |
| [lightllm/common/kv_trans_kernel/kv_trans.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_trans_kernel/kv_trans.py) | 算子层 | 最基础的「按索引 gather/scatter」Triton kernel，是页内数据搬运的原型 |
| [lightllm/server/router/model_infer/mode_backend/pd/kv_transporter.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/kv_transporter.py) | 后端工厂 | `create_kv_transporter` 按 env 变量选择 nixl 或 nccl 后端 |

为了把链路讲透，本讲还会引用以下「编排层」源码（它们驱动上述协议与算子）：

- [lightllm/server/router/model_infer/mode_backend/pd/prefill_node_impl/prefill_impl.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/prefill_node_impl/prefill_impl.py)：P 节点每步 prefill 后生成传输任务。
- [lightllm/server/router/model_infer/mode_backend/pd/prefill_node_impl/prefill_trans_process.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/prefill_node_impl/prefill_trans_process.py)：P 节点的传输子进程主循环。
- [lightllm/server/router/model_infer/mode_backend/pd/decode_node_impl/decode_trans_process.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/decode_node_impl/decode_trans_process.py)：D 节点的传输子进程主循环。
- [lightllm/server/router/model_infer/mode_backend/pd/nixl_kv_transporter.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/nixl_kv_transporter.py) / [nccl_kv_transporter.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/nccl_kv_transporter.py)：两种传输后端实现。
- [lightllm/common/kv_cache_mem_manager/mem_manager.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/mem_manager.py) 中的 `alloc_paged_kv_move_buffer` / `write_mem_to_page_kv_move_buffer` / `read_page_kv_move_buffer_to_mem`：页缓冲区的分配与 gather/scatter。

---

## 4. 核心概念与源码讲解

### 4.1 PD 分离

#### 4.1.1 概念说明

PD 分离把一个原本「一体」的推理服务拆成三类角色：

- **pd_master**：对外暴露 HTTP 接口、对内做调度。客户端只和它打交道；它为每个请求挑选一对 P 节点 + D 节点，并撮合二者完成 KV 交接。
- **prefill 节点（P 节点）**：`run_mode=prefill`，专门做 prefill，算完 KV 就把 KV 推送给 D 节点。
- **decode 节点（D 节点）**：`run_mode=decode`，专门做逐 token decode，先接收 P 节点送来的 KV，再开始生成。

之所以需要一个 master 而不是 P、D 直连，是因为在多对多的大规模部署里，「哪个 P 配哪个 D」是一个需要全局视角的调度决策（轮询 / 随机 / 按负载），并且 master 还要充当 P、D 之间交换「握手元数据」的中转站（见 4.1.2）。

#### 4.1.2 核心流程

一次完整请求在 PD 分离模式下的流转如下（注意 KV 迁移相关的握手）：

```
        客户端
          │ HTTP /generate
          ▼
     ┌──────────┐  1. select_p_d_node(p_node, d_node)        ┌──────────┐
     │ pd_master│───────────────────────────────────────────▶│   (路由) │
     └──────────┘                                            └──────────┘
          │ 2a. 把 prompt_ids + sampling_params 发给 P 节点 ───────▶ P 节点(prefill)
          │ 2b. 把 REQ 通知发给 D 节点 ────────────────────────────▶ D 节点(decode)
          │
          │ ◀── 3. D 节点上报 PDUpKVStatus(含 PDDecodeNodeInfo: agent_name/metadata/page_reg_desc)
          │ 4. master 把 D 节点信息下发给 P 节点(PD_REQ_DECODE_NODE_INFO)
          │                                            └──▶ 现在 P 节点知道 KV 该发给谁了
          │
          │            5. P 节点 prefill 产出 KV，按页切块，经 transporter 传给 D 节点
          │                          (nixl WRITE / nccl send，见 4.3)
          │
          │ ◀── 6. D 节点收齐 KV 后开始 decode，逐 token 把结果回流给 master
          ▼
        流式返回给客户端
```

第 3、4 步是关键握手：D 节点必须先把自己的「传输地址」（NIXL agent 元数据或 NCCL 控制端口）告诉 P 节点，P 节点才能发起传输。这个握手不是 P、D 直连完成的，而是经 master 中转——因为请求到达时 P 节点还不知道自己会被配到哪个 D 节点。

pd_master 在代码里还做了一件重要的事：**按 `max_new_tokens` 把请求分段**。PD 分离只能用保守调度，若用户设了过大的 `max_new_tokens`，会预留过多显存、拖垮吞吐；master 把长请求切成多段分别推理，使「分段推理」成为极少触发的兜底路径：

[lightllm/server/httpserver_for_pd_master/manager.py:124-131](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver_for_pd_master/manager.py#L124-L131) —— 注释明确解释了 PD 模式只能保守调度、故需分段。

#### 4.1.3 源码精读

**启动模式**由 `--run_mode` 决定，PD 分离用到 `prefill` / `decode` / `pd_master` 三种取值：

[lightllm/server/api_cli.py:7-23](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L7-L23) 定义了 `run_mode` 的全部取值，help 文本说明 `prefill/decode/pd_master` 三者共同构成 PD 分离模式。

PD 模式专属的命令行参数有 master 地址、节点选择策略、页缓冲区规格：

[lightllm/server/api_cli.py:44-55](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L44-L55) —— `--pd_master_ip` / `--pd_master_port`，P/D 节点靠它们连上 master。

[lightllm/server/api_cli.py:56-62](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L56-L62) —— `--select_p_d_node_strategy`，master 选 P/D 对的策略：`random` / `round_robin` / `adaptive_load`。

[lightllm/server/api_cli.py:83-95](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L83-L95) —— `--pd_kv_page_num`（默认 16）与 `--pd_kv_page_size`（默认 1024），分别决定页缓冲区的页数和每页能装多少 token。这两个参数直接决定一次 KV 迁移的并发度与单次传输大小。

master 侧的节点选择入口：

[lightllm/server/httpserver_for_pd_master/manager.py:100-103](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver_for_pd_master/manager.py#L100-L103) —— `select_p_d_node` 委托给 `pd_manager`，返回一对 `(p_node, d_node)`。

「D 节点信息回传 master、再由 master 转交 P 节点」的握手落点：

[lightllm/server/httpserver_for_pd_master/manager.py:254-260](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver_for_pd_master/manager.py#L254-L260) —— master 从 D 节点上报的 `PDUpKVStatus` 里反序列化出 `PDDecodeNodeInfo`，再用 `PD_REQ_DECODE_NODE_INFO` 下发给 P 节点。注释一句话点题：「将 decode 节点上报的当前请求使用的 decode 节点的信息下发给 p 节点，这样 p 节点才知道将 kv 传输给那个 d 节点。」

#### 4.1.4 代码实践

**实践目标**：搞清 PD 分离模式下各进程的启动形态，建立「谁是 master、谁是 P、谁是 D」的直觉。

**操作步骤**：

1. 阅读上面的 [api_cli.py:7-23](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L7-L23)，列出 `run_mode` 的 6 个取值。
2. 在仓库根目录执行 `python -m lightllm.server.api_server --help 2>/dev/null | grep -A3 "run_mode"`，确认本地实际可选项与本讲一致。
3. 追踪一次请求在 master 内的走向：从 [manager.py:142](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver_for_pd_master/manager.py#L142) 的 `select_p_d_node` 调用开始，向下读到分段循环（L150 起），再跳到 L254-260 的握手。

**需要观察的现象**：master 在收到请求后，先选 P/D 对，再把请求拆段；每一段都要经历「发给 P → 等 D 上报 → 把 D 信息回传 P」的完整握手。

**预期结果**：你能用一句话回答「为什么 P 节点不能在请求到达时立刻就知道 KV 该发给谁」——因为 P、D 配对是 master 当场决定的，且 D 的传输地址（agent 元数据）要等 D 节点收到请求后才能产生并上报。

> 待本地验证：步骤 2 的 `--help` 输出取决于当前安装版本；若环境未安装 LightLLM，可跳过，直接阅读源码完成步骤 3。

#### 4.1.5 小练习与答案

**练习 1**：PD 分离模式下，客户端把请求发给谁？P 节点和 D 节点谁先收到请求？

**参考答案**：客户端只发给 pd_master。master 先 `select_p_d_node` 选定一对节点，再把 prompt 发给 P 节点、把 REQ 通知发给 D 节点（二者几乎同时发出，见流程 2a/2b）。D 节点收到后并不立即推理，而是先上报自己的传输地址。

**练习 2**：为什么 master 要把长请求按 `max_new_tokens` 分段？

**参考答案**：PD 分离只能用保守调度（必须为最坏情况预留显存）。若用户设了非常大的 `max_new_tokens`，会为单个请求预留过多显存，导致系统吞吐骤降。分段后只要分块合理，真正触发「分段多次推理」的概率极低，吞吐不受影响（见 [manager.py:124-131](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver_for_pd_master/manager.py#L124-L131) 的注释）。

---

### 4.2 KV 迁移任务

#### 4.2.1 概念说明

PD 分离的核心数据结构是 `PDChunckedTransTask`（注意作者把 Chunked 拼成了 Chuncked，源码里全文统一，下面沿用源码拼写）。它描述「把某请求 `[start_kv_index, end_kv_index)` 这一段 KV，从 P 节点搬到 D 节点」这一**最小传输单元**。

之所以要「切块（chunked）」而不是整条 KV 一次性传，有两点原因：

1. **prefill 本身就是 chunked 的**：长 prompt 在 P 节点上会被分多步 prefill（见 [u2-l6](u2-l6-reqqueue-chunked-prefill.md)）。每 prefill 出一段 KV，就可以立刻启动这段的传输，让「计算」和「传输」重叠，不必等整条 prompt 算完。
2. **页大小固定**：传输以「页（page）」为单位，一页最多装 `pd_kv_page_size`（默认 1024）个 token。一段 KV 会被切成多个页，每页是一个 `PDChunckedTransTask`，多个任务可并行（占用不同页槽）。

#### 4.2.2 核心流程

一个 `PDChunckedTransTask` 的完整生命周期跨越 P、D 两侧的多个线程，状态机如下（这一段务必先读懂，再看源码）：

```
                  ┌─ P 节点 (prefill_trans_process.py) ─┐          ┌─ D 节点 (decode_trans_process.py) ─┐
                  │                                      │          │                                     │
分配 src_page ────▶│ recv_task_loop                       │          │                                     │
                  │   │ 拷本卡 KV → src_page (page_io write)        │ dispatch_task_loop                  │
                  │   ▼                                  │          │   │ 注册 waiting_dict               │
                  │ local_copy_kv_loop                   │          │   │ 上报 PDDecodeNodeInfo 给 master │
                  │   │ send WRITE request ────────────────┼──────────▶│   ▼                               │
                  │   ▼                                  │  notif   │ accept_peer_task_loop              │
                  │ ready_transfer_loop ──等 ready─────  │ "request"│   │ 分配 dst_page                   │
                  │                                      │◀─────────│ request_page_loop                  │
                  │   │ 收到 ready(含 dst_page)          │  notif   │   │ send WRITE ready ────────────────│
                  │   ▼                                  │ "ready"  │                                     │
                  │ write_peer_kv_loop                   │          │                                     │
                  │   │ 跨节点传页(nixl WRITE/nccl send) ──┼──────────▶│ (nccl: _recv_page 收页)           │
                  │   ▼                                  │  notif   │                                     │
                  │ update_task_status_loop ──等 DONE──  │ "done"   │   │ 收到 done → ready_page_task     │
                  │   │ send WRITE done ──────────────────┼──────────▶│   ▼                               │
                  │   ▼                                  │          │ read_page_to_mems_loop              │
                  │ success_loop (回收 src_page)         │          │   │ 拆页 → D 卡 kv_buffer (page_io read)
                  │                                      │          │   ▼                               │
                  │                                      │          │ success_loop (回收 dst_page)       │
                  └──────────────────────────────────────┘          └─────────────────────────────────────┘
```

整个协议是一个 **4 阶段 WRITE 握手**：`request` → `ready` → 实际传输 → `done`。两个方向都有通知：P→D 发 `request` 和 `done`，D→P 发 `ready`。之所以要先 `request`/`ready` 再传，是因为 **D 节点需要先为本任务分配一个空闲的 dst 页槽**，把 dst 页号告诉 P 节点，P 节点才知道往哪个对端页写。

页的「装/拆」用到 `kv_buffer` 与 `kv_move_buffer` 两块显存。一页对应的 token 数与字节量为：

\[
\text{page\_bytes} = \text{page\_size} \times \text{layer\_num} \times 2 \times \text{kv\_head\_num} \times \text{head\_dim} \times \text{dtype\_byte\_size}
\]

一个请求要切的页数（即任务数）为：

\[
N_{\text{tasks}} = \left\lceil \frac{\text{transfer\_kv\_len}}{\text{page\_size}} \right\rceil
\]

#### 4.2.3 源码精读

**(1) 协议核心 `PDChunckedTransTask`**

[lightllm/server/pd_io_struct.py:132-191](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/pd_io_struct.py#L132-L191) 定义了这个 dataclass。字段可分四组来记：

- **定位组**：`request_id`（哪个请求）、`start_kv_index`/`end_kv_index`（该请求 KV 的哪一段）、`page_kind`（`"kv"` 或 `"linear_att_state"`，后者用于线性注意力混合模型的状态）。
- **路由组**：`src_device_id`/`dst_device_id`（P/D 各自用哪张卡传）、`prefill_dp_index`/`decode_dp_index`（数据并行分组）。
- **数据组**：`mem_indexes`（这段 KV 在本端 `kv_buffer` 里的 token 槽位索引列表，P、D 各自不同）。
- **传输握手组**：`prefill_agent_name/metadata/num_pages/page_reg_desc` 与 `decode_*`（双方的传输地址）、`src_page_index`/`dst_page_index`（用哪个页槽）、`xfer_handle`（传输句柄）、`write_stage`（当前握手阶段）。

`__post_init__` 里有一条重要校验（[pd_io_struct.py:178-191](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/pd_io_struct.py#L178-L191)）：对 `"kv"` 类型的任务，`len(mem_indexes)` 必须等于 `end_kv_index - start_kv_index`；对 `"linear_att_state"`，则要求起止相等且无 mem_indexes。任务的唯一键由请求 id、page_kind 与 kv 区间拼成：

[lightllm/server/pd_io_struct.py:210-211](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/pd_io_struct.py#L210-L211) —— `get_key` 返回 `f"{request_id}_{page_kind}_{start_kv_index}_{end_kv_index}"`，P、D 两侧用它在 `waiting_dict` 里配对同一任务。

任务还有超时机制（[pd_io_struct.py:193-202](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/pd_io_struct.py#L193-L202)）：未开始传输前用 `time_out_secs`（默认 182s）卡，开始传输后用 `transfer_time_out_secs`（默认 66s）卡，防止对端失联时任务永久挂起。

**(2) P 节点如何产生任务**

[lightllm/server/router/model_infer/mode_backend/pd/prefill_node_impl/prefill_impl.py:50-92](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/prefill_node_impl/prefill_impl.py#L50-L92) —— `_prefill_chuncked_handle_func` 在每一步 chunked prefill 之后被调用。它从 `pd_trans_kv_start_index` 开始，按 `page_size` 切出尽可能多的整页任务；若 prefill 已完成（`cur_kv_len == input_len`）且 `output_len == 1`，则把 prefill 自己产生的**首个生成 token**（`first_gen_token_id`/`first_gen_token_logprob`）挂在最后一个任务上一并送给 D 节点——这是一个重要优化，省掉 D 节点重算首 token。

`_create_pd_trans_task`（[prefill_impl.py:94-147](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/prefill_node_impl/prefill_impl.py#L94-L147)）构造任务时，`mem_indexes` 直接取自请求映射表 `req_to_token_indexs[req_idx, start:end]`（u4-l1 讲过的那张表），`decode_agent_*` 来自 `req_obj.sampling_param.pd_decode_node`——正是 4.1 握手里 master 下发的 D 节点信息。

**(3) P 节点传输子进程：收页 → 装页 → 握手 → 传输 → 收尾**

[lightllm/server/router/model_infer/mode_backend/pd/prefill_node_impl/prefill_trans_process.py:182-204](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/prefill_node_impl/prefill_trans_process.py#L182-L204) —— `local_copy_kv_loop`：在专用 `copy_cuda_stream` 上调用 `write_mem_to_page_kv_move_buffer`，把 `mem_indexes` 指向的散落 KV 收集到 `src_page_index` 这一页里，再用一个 CUDA event 标记完成。

[lightllm/server/router/model_infer/mode_backend/pd/prefill_node_impl/prefill_trans_process.py:299-321](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/prefill_node_impl/prefill_trans_process.py#L299-L321) —— `write_peer_kv_loop`：收到 D 节点 `ready`（含 `dst_page_index`）后，调用 `transporter.write_blocks_paged(trans_task)` 真正发起跨节点传输，并把返回的 `xfer_handle` 存回任务、记下 `start_trans_time`。

[lightllm/server/router/model_infer/mode_backend/pd/prefill_node_impl/prefill_trans_process.py:323-373](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/prefill_node_impl/prefill_trans_process.py#L323-L373) —— `update_task_status_loop`：轮询每个在途任务的 `check_task_status`，返回 `"DONE"` 则发 `done` 通知并回收 src 页；`"ERR"` 或超时则进失败队列。

**(4) D 节点传输子进程：注册 → 分页 → 收页 → 拆页**

[lightllm/server/router/model_infer/mode_backend/pd/decode_node_impl/decode_trans_process.py:204-237](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/decode_node_impl/decode_trans_process.py#L204-L237) —— `dispatch_task_loop`：把任务组登记进 `waiting_dict`，并构造 `PDDecodeNodeInfo`（含本端 agent 元数据与页描述）包进 `PDUpKVStatus` 上报给 master（对应 4.1 流程的第 3 步）。

[lightllm/server/router/model_infer/mode_backend/pd/decode_node_impl/decode_trans_process.py:352-375](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/decode_node_impl/decode_trans_process.py#L352-L375) —— `request_page_loop`：收到 P 节点 `request` 后，为本任务从 `page_index_queue` 领一个空闲 dst 页，回发 `ready`。

[lightllm/server/router/model_infer/mode_backend/pd/decode_node_impl/decode_trans_process.py:377-397](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/decode_node_impl/decode_trans_process.py#L377-L397) —— `read_page_to_mems_loop`：收到 `done` 后，调用 `read_page_kv_move_buffer_to_mem` 把 dst 页拆散写回 D 节点 `kv_buffer` 的 `mem_indexes` 槽位，并用 timing event 记录 GPU 拷贝耗时。

**(5) 页缓冲区与 gather/scatter**

页缓冲区在 [mem_manager.py:88-96](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/mem_manager.py#L88-L96) 分配，注意它的 5 维形状与 `kv_buffer` 维度顺序不同：

```python
self.kv_move_buffer = torch.empty(
    (page_num, page_size, self.layer_num, 2 * num_kv_head, self.head_dim), ...)
```

其中 `num_kv_head = get_num_key_value_heads(model_dir)` 是**未按 TP 切分的全量 KV 头数**，所以一页装下了所有 TP rank 的 KV 分片。`write_mem_to_page_kv_move_buffer`（[mem_manager.py:98-128](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/mem_manager.py#L98-L128)）对每个 TP rank 调一次 `page_io(mode="write")`，把本 rank 负责的那部分头写到页的对应分区；`read_page_kv_move_buffer_to_mem`（[mem_manager.py:130-160](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/mem_manager.py#L130-L160)）则对称地用 `mode="read"` 拆回。

**(6) 底层 gather/scatter 算子 `kv_trans`**

上面用到的 `page_io` 是「分页、跨 TP」的专用 gather/scatter；而 [lightllm/common/kv_trans_kernel/kv_trans.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_trans_kernel/kv_trans.py) 给出的是它最朴素的原型——一段「按索引搬 token」的逻辑。

[lightllm/common/kv_trans_kernel/kv_trans.py:48-78](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_trans_kernel/kv_trans.py#L48-L78) —— `kv_trans(input, input_idx, output, output_idx)`：把 `input` 中 `input_idx[i]` 位置的 token 搬到 `output` 的 `output_idx[i]` 位置。语义等价于「按 `input_idx` gather，再按 `output_idx` scatter」。

其 Triton kernel（[kv_trans.py:7-45](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_trans_kernel/kv_trans.py#L7-L45)）的核心循环很直白：

```python
while tid < token_num:
    input_token_idx  = tl.load(input_token_idx_ptr + tid)
    output_token_idx = tl.load(output_token_idx_ptr + tid)
    for block_idx in tl.range(0, tl.cdiv(head_num_dim, BLOCK_SIZE), ...):
        cur_offs = block_idx * BLOCK_SIZE + offs
        in_datas = tl.load(input_ptr + input_stride_0 * input_token_idx + cur_offs, ...)
        tl.store(output_ptr + output_stride_0 * output_token_idx + cur_offs, in_datas, ...)
    tid += grid_count
```

注意两个有意为之的细节：`grid_count = 20`（[kv_trans.py:58](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_trans_kernel/kv_trans.py#L58)）注释写明「用较少的资源来做数据传输，防止占用过多的 sm 计算单元」——KV 搬运是辅助路径，不能和推理抢 SM；`num_warps=1` 也是同理。生产环境真正走的 PD 分页搬运是 `page_io`（处理 K/V 拆半、TP 分区），数据并行场景则用 `kv_trans_v2`；但它们的核心思想与这里的 `kv_trans` 完全一致：**靠一张索引表把散落的 token 搬到连续目标**。

> 说明：`kv_trans.py` 是该家族最朴素的原型与教学参照，运行时 PD 分页路径实际调用的是同目录下 `nixl_kv_trans.page_io` 与 `kv_trans_v2`。本讲引用它是为了把「gather/scatter」这一最底层动作讲清楚。

#### 4.2.4 代码实践

**实践目标**：跟读一段 KV 从 P 节点 `kv_buffer` 到 D 节点 `kv_buffer` 的完整搬运路径，验证「装页 → 传输 → 拆页」三段论。

**操作步骤**（纯源码阅读型实践，无需 GPU）：

1. 从 [prefill_impl.py:62-72](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/prefill_node_impl/prefill_impl.py#L62-L72) 的 `while` 循环看 P 节点如何按 `page_size` 把一段 KV 切成多个任务，记下每个任务的 `start_kv_index`/`end_kv_index` 与 `mem_indexes` 来源。
2. 跳到 [prefill_trans_process.py:186-199](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/prefill_node_impl/prefill_trans_process.py#L186-L199)，确认 `write_mem_to_page_kv_move_buffer` 的入参就是上一步的 `mem_indexes` 与 `src_page_index`——这是「装页」。
3. 跳到 D 节点 [decode_trans_process.py:387-395](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/decode_node_impl/decode_trans_process.py#L387-L395)，确认 `read_page_kv_move_buffer_to_mem` 的 `mem_indexes` 来自 D 节点自己 `alloc` 出的槽位（见 [decode_impl.py:125-128](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/decode_node_impl/decode_impl.py#L125-L128)）——这是「拆页」，且与 P 侧索引不同。
4. 中间的「传输」步骤由 4.3 讲解；这里先在草稿上画出：同一个 token 在 P 侧的 `mem_indexes[i]` 与 D 侧的 `mem_indexes[j]` 一般不相等，连接二者的是 page 里相同的相对位置 `tid`。

**需要观察的现象**：`mem_indexes` 在 P、D 两侧是两套完全独立的索引；唯一保证数据对齐的是「page 内的相对 token 序号」。

**预期结果**：你能解释「为什么 KV 不能直接 memcpy」——因为两端 `kv_buffer` 的 token 布局不同，必须经 page 这一中间连续缓冲做 gather/scatter。

#### 4.2.5 小练习与答案

**练习 1**：`PDChunckedTransTask.get_key()` 为什么要把 `page_kind` 也拼进 key？

**参考答案**：同一个请求同一段 kv 区间，可能既有 `"kv"` 类型的任务，也有 `"linear_att_state"` 类型的任务（线性注意力混合模型场景，见 [prefill_impl.py:76-85](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/prefill_node_impl/prefill_impl.py#L76-L85)）。后者 `start_kv_index == end_kv_index`（即区间为空），若不以 `page_kind` 区分，会与前者 key 冲突。`get_key` 用 `request_id + page_kind + 区间` 三元组保证唯一。

**练习 2**：4 阶段 WRITE 握手里，为什么不能省掉 `request`/`ready`，让 P 节点直接传？

**参考答案**：因为 D 节点必须先为本任务分配一个空闲的 dst 页槽（`page_index_queue` 里领取），P 节点才有有效的 `dst_page_index` 可写。若直接传，P 节点不知道往 D 节点的哪一页写，会覆盖 D 节点正在用的页。`request` 让 D 知道有任务要来、`ready` 把 D 分配好的 `dst_page_index` 回传 P，才安全。

**练习 3**：`kv_trans.py` 里 `grid_count = 20`、`num_warps = 1` 的用意是什么？

**参考答案**：KV 搬运是辅助路径，不应与推理主路径争抢 GPU 的 SM 算力。用很少的 block（20 个）和最少的 warp（1），把搬运 kernel 的资源占用压到最低，避免拖慢同卡上正在跑的 prefill/decode 计算。

---

### 4.3 传输后端

#### 4.3.1 概念说明

「装页」之后，要把这一页 GPU 显存从 P 节点搬到 D 节点。LightLLM 把这一步抽象成可替换的**传输后端（transport backend）**，由环境变量 `LIGHTLLM_PD_KV_TRANSPORT_BACKEND` 选择，默认 `nixl`，可选 `nccl`：

[lightllm/server/router/model_infer/mode_backend/pd/kv_transporter.py:15-40](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/kv_transporter.py#L15-L40) —— `create_kv_transporter` 是后端工厂，读 env 后返回 `NixlKVTransporter` 或 `NcclKVTransporter`。

两种后端的本质区别在于「谁能做 GPU 显存的跨节点搬运」：

- **nixl（默认）**：NVIDIA 的 RDMA 传输库，支持**单边 WRITE（one-sided write）**和**远程通知（remote notification）**。P 节点可以直接把数据写进 D 节点的显存，并用 NIXL 自带的通知机制告知对方。性能最佳，但依赖 NIXL 库与 RDMA 网络。
- **nccl**：集合通信库，原生没有单边 WRITE 和远程通知。LightLLM 用 `comm.send`/`comm.recv` 做点对点传页，并**额外用一条 rpyc 控制通道**来传 `request`/`ready`/`done` 等通知。无需 NIXL，部署门槛低，但需要 P/D 双方各起一对收发线程。

#### 4.3.2 核心流程

两种后端对外暴露**完全相同的接口**（这是策略模式的关键），P/D 两侧的编排代码无需感知后端差异：

| 接口方法 | 含义 |
|----------|------|
| `agent_name` / `agent_metadata` / `local_page_mem_desc` | 本端「传输地址」与页内存描述，握手时发给对端 |
| `connect_add_remote_agent(PDAgentMetadata)` | 登记对端的传输地址 |
| `send_write_request_task_to_decode_node(task)` | P→D 通知：`write_stage="request"` |
| `send_write_ready_task_to_prefill_node(task)` | D→P 通知：`write_stage="ready"`（nccl 还附带启动接收线程） |
| `write_blocks_paged(task) → handle` | 真正发起传页，返回传输句柄 |
| `check_task_status(task) → "PROC"/"DONE"/"ERR"` | 查询传输状态 |
| `send_write_done_task_to_decode_node(task)` | P→D 通知：`write_stage="done"` |
| `get_new_notifs() → Dict[agent_name, list[bytes]]` | 拉取对端发来的所有通知 |
| `release_xfer_handle(handle)` / `shutdown()` | 释放句柄 / 关闭连接 |

由于接口一致，4.2 讲的 4 阶段握手状态机对两种后端完全成立；区别只在这几个方法的**内部实现**。

#### 4.3.3 源码精读

**(1) nixl 后端**

[lightllm/server/router/model_infer/mode_backend/pd/nixl_kv_transporter.py:25-43](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/nixl_kv_transporter.py#L25-L43) —— `NixlKVTransporter.__init__`：创建一个 NIXL agent，名字为 `f"{node_id}_{tp_idx}"`，并立即把自己的 `kv_move_buffer` 注册为可被远程读写的显存：

[lightllm/server/router/model_infer/mode_backend/pd/nixl_kv_transporter.py:60-73](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/nixl_kv_transporter.py#L60-L73) —— `_register_kv_move_buffer` 把整块页缓冲注册成 NIXL 内存描述，并 `_create_paged_xfer_handles` 为每一页预生成传输描述符（按 `page_len` 算好每页的基地址偏移）。

真正的「传页」是一条 NIXL WRITE：

[lightllm/server/router/model_infer/mode_backend/pd/nixl_kv_transporter.py:238-268](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/nixl_kv_transporter.py#L238-L268) —— `write_blocks_paged`：用 `make_prepped_xfer("WRITE", src_handle, [src_page_index], dst_handle, [dst_page_index], ...)` 准备一次从本端 src 页到对端 dst 页的单边写，`transfer(handle)` 异步发起，返回 handle 给上层轮询。

状态查询直接映射到 NIXL 的 xfer state：

[lightllm/server/router/model_infer/mode_backend/pd/nixl_kv_transporter.py:270-276](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/nixl_kv_transporter.py#L270-L276) —— `check_task_status` 返回 `"PROC"/"DONE"/"ERR"`。

NIXL 自带通知通道，所以 `send_write_*_task_*` 系列就是 `nixl_agent.send_notif(peer, pickle.dumps(task))`，例如：

[lightllm/server/router/model_infer/mode_backend/pd/nixl_kv_transporter.py:139-158](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/nixl_kv_transporter.py#L139-L158) —— `send_write_request_task_to_decode_node`：先把任务里的临时字段清空、把**本端** prefill agent 信息填进去，再 `send_notif` 发给 D 节点。`ready`/`done`/`error` 各方向的发送方法结构对称，区别只在 `write_stage` 和填的是 prefill 还是 decode 的 agent 信息。

**(2) nccl 后端**

[lightllm/server/router/model_infer/mode_backend/pd/nccl_kv_transporter.py:32-71](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/nccl_kv_transporter.py#L32-L71) —— `NcclKVTransporter.__init__`：它不依赖 NIXL，而是启动一条 rpyc 控制通道 `_NcclControlChannel`（[nccl_kv_transporter.py:423-461](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/nccl_kv_transporter.py#L423-L461)），在 `[control_port_min, control_port_max]` 区间探测空闲端口监听。类的 docstring（[nccl_kv_transporter.py:33-40](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/nccl_kv_transporter.py#L33-L40)）开门见山说明了它存在的原因：**NCCL 不提供远程通知与单边 WRITE，所以用一条小的 rpyc 控制通道来传通知和引导通信域建立**，从而复用与 nixl 相同的 request/ready/done/error 接口。

「传页」退化为 `comm.send(page_tensor)` / `comm.recv(page_tensor)`：

[lightllm/server/router/model_infer/mode_backend/pd/nccl_kv_transporter.py:299-313](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/nccl_kv_transporter.py#L299-L313) —— `_NcclPeer.send_page`：取出 `kv_move_buffer[src_page_index]` 这一页张量，经 NCCL communicator `send(dst=1)`，并用一个 CUDA event 记录完成（这就是它的「传输句柄」`_NcclXferHandle`，状态靠 `event.query()` 判定）。

[lightllm/server/router/model_infer/mode_backend/pd/nccl_kv_transporter.py:357-371](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/nccl_kv_transporter.py#L357-L371) —— `_NcclPeer._recv_page`：D 节点侧在独立线程里 `recv(src=0)` 收进 `kv_move_buffer[dst_page_index]`。这个接收循环由 `send_write_ready_task_to_prefill_node` 触发（[nccl_kv_transporter.py:151-164](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/nccl_kv_transporter.py#L151-L164)）：D 在发 `ready` 通知的同时 `start_recv(trans_task)` 把任务塞进接收队列。

NCCL 通信域的建立用 `StatelessP2PProcessGroup`（[nccl_kv_transporter.py:373-393](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/nccl_kv_transporter.py#L373-L393)），其 bootstrap store 走的正是那条 rpyc 控制通道（`_NcclControlStore`，[nccl_kv_transporter.py:532-555](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/nccl_kv_transporter.py#L532-L555)）。控制端口按 tp_idx 错开分配（[kv_transporter.py:22-38](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/kv_transporter.py#L22-L38)）：`port_min = 20000 + tp_idx*100`，避免同机多卡冲突。

**(3) 两种后端的关键差异对照**

| 维度 | nixl 后端 | nccl 后端 |
|------|-----------|-----------|
| 传输原语 | NIXL 单边 WRITE（RDMA） | NCCL `send`/`recv`（点对点） |
| 通知机制 | NIXL 自带 `send_notif`/`get_new_notifs` | 额外的 rpyc 控制通道（`_NcclControlChannel`） |
| 页内存注册 | `register_memory(kv_move_buffer)` | 仅 pickle 元信息（形状/dtype），无需注册 |
| 状态判定 | `check_xfer_state(handle)` | CUDA `event.query()` |
| 接收方 | 被动接收（NIXL 写入其显存） | 需主动起 `_recv_page_loop` 线程 `recv` |
| 依赖 | 必须装 NIXL + RDMA 网络 | 仅需 NCCL（部署更简单） |
| 性能 | 更优（RDMA 直写、无需对端参与收） | 略逊（需双方各起收发线程、走集合通信栈） |

切换方式只需在启动 P/D 节点前设置环境变量：`export LIGHTLLM_PD_KV_TRANSPORT_BACKEND=nccl`（不设则默认 nixl，见 [kv_transporter.py:16](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/kv_transporter.py#L16)）。两种后端都额外要求 GPU 支持 P2P 直连，P/D 节点的 backend 在 `init_custom` 里 `assert kv_trans_use_p2p()`（见 [prefill_impl.py:22-24](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/prefill_node_impl/prefill_impl.py#L22-L24) 与 [decode_impl.py:20-24](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/decode_node_impl/decode_impl.py#L20-L24)）。

#### 4.3.4 代码实践

**实践目标**：对比两种后端的「传页」实现，直观理解单边 WRITE 与 send/recv 的差别。

**操作步骤**：

1. 打开 [nixl_kv_transporter.py:238-268](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/nixl_kv_transporter.py#L238-L268)，注意 P 节点一次 `make_prepped_xfer("WRITE",...) + transfer(handle)` 即完成发起，D 节点侧**没有任何主动 recv 代码**——数据被 RDMA 直接写进 D 的显存。
2. 再打开 [nccl_kv_transporter.py:299-313](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/nccl_kv_transporter.py#L299-L313) 与 [nccl_kv_transporter.py:357-371](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/nccl_kv_transporter.py#L357-L371)，注意 D 节点必须在 `_recv_page_loop` 线程里**主动 `comm.recv`**，且这个线程是由 `send_write_ready_task_to_prefill_node` 里 `start_recv` 拉起的。
3. 在两种后端的 `__init__` 里分别找「页内存注册」这一步：nixl 调 `register_memory`（运行时真的注册显存），nccl 只 pickle 形状信息（[nccl_kv_transporter.py:88-99](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/nccl_kv_transporter.py#L88-L99)）。

**需要观察的现象**：nixl 路径里 D 节点的 `accept_peer_task_loop` 收到 `done` 就能直接去读 dst 页；而 nccl 路径在收到 `request` 时就已经在后台 `recv` 了，`done` 到达时数据通常已落盘。

**预期结果**：你能向同伴讲清「为什么 nccl 后端需要 `_recv_page_loop` 线程、而 nixl 后端不需要」——因为 nixl 是单边写、D 节点被动接收；nccl 是双边通信、必须有人主动 `recv`。

> 待本地验证：若本机装有 NIXL 且有 RDMA 网卡，可在两台机器分别以 `run_mode=prefill` 与 `run_mode=decode` 启动，分别用默认（nixl）与 `LIGHTLLM_PD_KV_TRANSPORT_BACKEND=nccl` 各跑一次，对比日志里 `NCCL send page posted` / `NCCL recv page done` 与 NIXL telemetry 行的差异。无硬件时可只做源码对比。

#### 4.3.5 小练习与答案

**练习 1**：`create_kv_transporter` 如何决定用哪个后端？默认是哪个？

**参考答案**：读环境变量 `LIGHTLLM_PD_KV_TRANSPORT_BACKEND`，小写后匹配；默认 `"nixl"`，匹配不到 `nixl`/`nccl` 则抛 `ValueError`（[kv_transporter.py:16-40](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/kv_transporter.py#L16-L40)）。

**练习 2**：为什么 `NcclKVTransporter` 需要一条额外的 rpyc 控制通道，而 `NixlKVTransporter` 不需要？

**参考答案**：4 阶段 WRITE 协议依赖 `request`/`ready`/`done`/`error` 四类**控制通知**。NIXL 原生提供 `send_notif`/`get_new_notifs` 通知机制，可直接复用；NCCL 只能传张量、没有通知原语，所以 nccl 后端额外起一条 rpyc 控制通道来传这些通知，并兼做 NCCL 通信域 bootstrap 的 store（见类 docstring [nccl_kv_transporter.py:33-40](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/nccl_kv_transporter.py#L33-L40)）。

**练习 3**：nccl 后端里，D 节点的 `_recv_page_loop` 线程是何时被拉起的？为什么不能在 `__init__` 时就拉起？

**参考答案**：在 `send_write_ready_task_to_prefill_node` 里调用 `_get_peer(peer_name).start_recv(trans_task)` 触发（[nccl_kv_transporter.py:155](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/nccl_kv_transporter.py#L155)），真正线程在 `_get_recv_queue` 首次访问时惰性创建（[nccl_kv_transporter.py:340-347](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/nccl_kv_transporter.py#L340-L347)）。不在 `__init__` 拉起，是因为该线程要先拿到具体的 `trans_task`（含 `dst_page_index`、peer 名等）才能 `recv`，且要按 peer（对端 agent）维度组织接收队列；提前拉起没有任务可收、也无从知道对端是谁。

---

## 5. 综合实践

**任务**：把本讲三个最小模块串起来，画出「一次请求的 KV 从 P 节点 `kv_buffer` 到 D 节点 `kv_buffer`」的完整时序图，并对照源码标注每一步发生在哪个文件、哪个函数。

**建议产出**：一张时序图 + 一张「数据形态变化表」。数据形态表参考下表填写（以传输 `[0, 1024)` 这一段 KV 为例）：

| 阶段 | 所在节点/进程 | 数据位置 | 形态 | 关键函数 |
|------|---------------|----------|------|----------|
| prefill 产出 KV | P 节点 model 进程 | `kv_buffer[:, mem_indexes_p, :, :]` | 散落，按 P 的 mem_indexes | （u3 prefill 主流程） |
| 生成传输任务 | P 节点 model 进程 | `PDChunckedTransTask(mem_indexes=mem_indexes_p)` | 协议对象 | `_prefill_chuncked_handle_func` |
| 装页 | P 节点 trans 进程 | `kv_move_buffer[src_page_index]` | 连续页（含全 TP 头） | `write_mem_to_page_kv_move_buffer` → `page_io(write)` |
| 握手 request | P→D 通知 | `write_stage="request"` | pickle 后的通知 | `send_write_request_task_to_decode_node` |
| 握手 ready | D→P 通知 | `write_stage="ready"` + `dst_page_index` | pickle 后的通知 | `send_write_ready_task_to_prefill_node` |
| 跨节点传页 | P→D | `kv_move_buffer[src_page]` → `kv_move_buffer[dst_page]` | 整页 GPU 显存 | `write_blocks_paged`（nixl WRITE / nccl send） |
| 握手 done | P→D 通知 | `write_stage="done"` | pickle 后的通知 | `send_write_done_task_to_decode_node` |
| 拆页 | D 节点 trans 进程 | `kv_buffer[:, mem_indexes_d, :, :]` | 散落，按 D 的 mem_indexes | `read_page_kv_move_buffer_to_mem` → `page_io(read)` |
| decode 使用 KV | D 节点 model 进程 | `req_to_token_indexs[req_idx]` | 已可被注意力读取 | （u3 decode 主流程） |

**自检问题**（回答出即算通关）：

1. 为什么表中 P 的 `mem_indexes_p` 和 D 的 `mem_indexes_d` 不一样？连接二者的「同一性」由什么保证？
2. 若把 `--pd_kv_page_size` 从 1024 改成 256，对一次请求的传输任务数量、单页传输字节量分别有什么影响？（用 4.2.2 的两个公式算一下）
3. 若生产环境没有 RDMA 网卡、装不了 NIXL，应如何切换后端？切换后 D 节点为何需要额外起接收线程？

> 这三个问题分别考察你对「KV 迁移任务」「传输后端」「PD 分离」三个最小模块的综合理解。

## 6. 本讲小结

- **PD 分离**把 prefill 与 decode 拆到两套 GPU 节点，由 pd_master 调度配对并中转 D 节点传输地址的握手；客户端只与 master 交互。
- 跨节点搬运 KV 的核心数据结构是 **`PDChunckedTransTask`**，它把一段 `[start_kv_index, end_kv_index)` 的 KV 切成一个「页」级传输单元，承载了定位、路由、数据索引与四阶段握手状态。
- KV 不能直接 memcpy：必须先 **gather 装页**（`page_io` write，散落 `mem_indexes` → 连续页）、跨节点 **传页**、对端再 **scatter 拆页**（`page_io` read，连续页 → 对端独立的 `mem_indexes`）；`kv_trans.py` 是这套 gather/scatter 最朴素的原型。
- 传输过程是一个 **4 阶段 WRITE 握手**（request → ready → 传页 → done），P、D 两侧各自跑一条多线程流水线（`*_trans_process.py`），靠 `get_key()` 在 `waiting_dict` 里配对任务。
- **传输后端**用策略模式抽象，默认 `nixl`（RDMA 单边 WRITE + NIXL 通知），可切 `nccl`（`send`/`recv` + 额外 rpyc 控制通道），二者对外接口一致、由 `LIGHTLLM_PD_KV_TRANSPORT_BACKEND` 切换。
- prefill 节点会顺手把自己生成的**首个 token** 挂在最后一个传输任务上送给 D 节点，省掉 D 重算首 token，是 PD 分离里一个值得留意的小优化。

## 7. 下一步学习建议

1. **继续往下读传输后端的进阶**：本讲只讲了 nixl/nccl 的「普通 KV」。线性注意力混合模型（如 Qwen3-Next）还会用 `page_kind="linear_att_state"` 传额外状态，可阅读 [lightllm/common/kv_cache_mem_manager/qwen3next_mem_manager.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/qwen3next_mem_manager.py) 与 [deepseek2_mem_manager.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/deepseek2_mem_manager.py)（后者用 `mla_page_io` 处理 MLA 的单头潜变量 KV）。
2. **结合数据并行**：[u7-l3（数据并行与负载均衡）](u7-l3-dp-and-load-balance.md) 会讲多 DP 组下的请求分发；本讲里反复出现的 `prefill_dp_index`/`decode_dp_index` 与 `kv_trans_v2` 正是 DP 场景的延伸，建议两讲对照阅读。
3. **回到调度闭环**：PD 模式「只能保守调度」这一限制的根源在 [u2-l6](u2-l6-reqqueue-chunked-prefill.md) 的 chunked prefill 调度；想理解 master 为何必须分段，可重读 u2-l6 的「三道闸门」。
4. **动手验证（若有硬件）**：参照 [unit_tests/common/kv_trans_kernel/test_nixl_kv_trans.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/unit_tests/common/kv_trans_kernel/test_nixl_kv_trans.py) 中的 `test_page_io_roundtrip_with_tp`，跑通 `page_io` 的 write→read 往返测试，亲手验证「装页再拆页」数据不丢。
