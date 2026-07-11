# 分布式通信与集合通信

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 LightLLM 为什么用一套**纯 Python 的 `ctypes` 封装**直接调 NCCL 库，而不是直接用 `torch.distributed.all_reduce`，并理解这与 **CUDA Graph 捕获**的强绑定关系。
- 看懂 `PyNcclCommunicator` 如何在多 rank 间用 `ncclUniqueId` 握手建立通信域、`all_reduce/send/recv` 如何把一个 CUDA stream 上的张量送出去，以及 `StatelessP2PProcessGroup` 为什么是 PD 分离跨节点传输的基石。
- 掌握 `communication_op.py` 提供的**统一集合通信接口**（`all_reduce / all_gather / reduce_scatter / broadcast`）：它如何对 `world_size==1` 短路、如何按 `group_size` 建多套通信域以支撑 microbatch overlap、以及 `CustomProcessGroup` 的三档派发链 `FlashInfer → SymmMem → NCCL`。
- 说清 TP（张量并行）下 `all_reduce` / `reduce_scatter` 究竟在**哪些算子之后**被调用（行并行的 `o_proj`、`down_proj` 之后），并能把「每个 rank 建立 NCCL 通信域」的全过程串成一条链路。

## 2. 前置知识

本讲是 advanced 阶段内容，建议你已经学完：

- **u3-l1 TpPartBaseModel 推理框架**：知道每个 GPU 上跑着一个 `ModeBackend` 进程、一个 `TpPartBaseModel`（每张卡只持有「张量并行的一片」）。
- **u3-4 权重加载与张量并行切分**：知道 TP 下 `q/kv/gate/up` 沿输出维切（列并行，无需通信）、`o/down` 沿输入维切（行并行，结果需 all-reduce 求和）。
- **u2-4 Model Backend 推理后端与 RPC**：知道 `ModeBackend.init_model` 的初始化流水线，本讲正是其中「建分布式环境」那一步的展开。
- **u6-1 CUDA Graph 捕获与重放**：知道 CUDA Graph 对「地址固定、形状固定」的硬约束——这是 LightLLM 自建 NCCL 封装的核心动机。
- **u6-2 microbatch overlap 与 TPSP 混合并行**：知道 TPSP 把 all-reduce 拆成 all-gather / reduce-scatter、并用两套独立通信域让通信可重叠——本讲讲的正是这两套通信域从哪来、由谁建。

下面用通俗语言补几个本讲要用到的概念：

- **NCCL（NVIDIA Collective Communications Library）**：NVIDIA 提供的 GPU 间集合通信库，是事实上的 GPU 分布式训练/推理通信后端。它的核心对象是 **communicator（通信域）**：一组 rank（进程/GPU）通过同一个 `ncclUniqueId` 握手，形成一个通信域，之后域上的 `all_reduce / all_gather / send / recv` 等集合操作就能在这组 GPU 间同步执行。
- **rank / world_size**：在一个通信域里，每个进程有一个编号 `rank`，进程总数叫 `world_size`。集合通信要求**所有 rank 都调用同一操作**才能推进（集体同步语义）。
- **`ncclUniqueId`**：NCCL 建通信域的「房间号」。rank 0 调 `ncclGetUniqueId()` 生成一个 128 字节的唯一标识，**广播给所有 rank**，所有 rank 拿着同一个 id + 自己的 rank 调 `ncclCommInitRank`，才能「对上暗号」建成同一个域。
- **all-reduce**：把每个 rank 上同形状的张量按某种算子（通常 SUM）归约，**结果在每个 rank 上都得到完整副本**。行并行层（如 `o_proj`）每个 rank 只算部分和，必须 all-reduce 才能得到正确输出：

\[
  \text{out}[i] = \sum_{r=0}^{W-1} x_{r}[i], \quad \text{每个 rank 都拿到这份求和结果}
\]

- **all-gather / reduce-scatter**：all-gather 把各 rank 的切片**拼接**成完整张量（不归约）；reduce-scatter 先归约再**切片**，每个 rank 只拿到 `1/W` 的结果。TPSP 用它们替代一次大 all-reduce，使通信更小更可拆。
- **`ctypes`**：Python 标准库里直接调用 C 动态链接库（`.so`）的模块。LightLLM 用它把 `libnccl.so` 的 C 函数逐一映射成 Python 可调用对象，无需写 C++ 扩展、换 NCCL 版本只改环境变量即可。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `lightllm/distributed/pynccl_wrapper.py` | **NCCL 的纯 Python ctypes 绑定**。定义 `NCCLLibrary`（按名字表加载 `libnccl.so` 并映射 `ncclAllReduce/ncclSend/...`）、`ncclDataTypeEnum`/`ncclRedOpTypeEnum`（torch↔nccl 类型映射）、`ncclUniqueId` 结构体。 |
| `lightllm/distributed/pynccl.py` | 在 wrapper 之上封装 `PyNcclCommunicator`（建通信域 + `all_reduce/send/recv`）与 `StatelessP2PProcessGroup`（基于 TCPStore 的无状态元数据通道，给 PD 跨节点 NCCL 建域用）。 |
| `lightllm/distributed/communication_op.py` | **集合通信统一接口层**。`CustomProcessGroup`（持有 NCCL 域 + 可选 FlashInfer/SymmMem 自定义归约）、`DistributeGroupManager`（按 `group_size` 建多套域）、模块级 `all_reduce/all_gather/reduce_scatter/broadcast`（短路 + 派发）。 |
| `lightllm/distributed/flashinfer_all_reduce.py` | 小消息 all-reduce 的 FlashInfer（trtllm oneshot lamport）后端。 |
| `lightllm/distributed/symm_mem_all_reduce.py` | 基于 torch 对称内存（NVLink SHARP / NVLS multimem）的 all-reduce 后端。 |
| `lightllm/utils/dist_utils.py` | `init_distributed_env`（调 `dist.init_process_group("nccl")` 建全局域）、`create_new_group_for_current_dp`（为每个 DP 组切子域），并定义 rank 语义。 |
| `lightllm/server/router/model_infer/mode_backend/base_backend.py` | 启动期 `init_distributed_env` → `dist_group_manager.create_groups(group_size)` 的调用点，以及节点内 `node_nccl_group` 的建立。 |
| `lightllm/common/basemodel/layer_infer/base_layer_infer.py` | TPSP 三个通信原语 `_tpsp_allgather / _tpsp_reduce / _tpsp_sp_split`，是 all-reduce/reduce-scatter 在层级的具体调用点。 |
| `lightllm/models/llama/layer_infer/transformer_layer_infer.py` | 真实调用 `_tpsp_reduce` 的位置：`_get_o`（o_proj 后）、`_ffn`（down_proj 后）。 |
| `lightllm/server/router/model_infer/mode_backend/pd/nccl_kv_transporter.py` | PD 分离 NCCL 传输后端：用 `StatelessP2PProcessGroup` + `PyNcclCommunicator` 在 P/D 节点间点对点 `send/recv` KV 页。 |

## 4. 核心概念与源码讲解

### 4.1 NCCL 封装：用 ctypes 直连 libnccl

#### 4.1.1 概念说明

GPU 间做集合通信，最自然的 Python 入口是 `torch.distributed.all_reduce`。但 LightLLM 在 [pynccl_wrapper.py:22-41](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/pynccl_wrapper.py#L22-L41) 开头写了一段很重要的注释，说明了为什么不直接用它，而是自己用 `ctypes` 包一层：

- 试过 `cupy`：调 NCCL 没问题，但 `cupy` 初始化 communicator 经常卡住。
- 试过 `torch.distributed`：它的 `all_reduce` 内部会夹带**很多额外的 CUDA API 调用**，而这些调用在 **CUDA Graph 捕获期间是不允许的**（会破坏图的可重放性）。
- 也考虑过写 C/C++ 扩展：但 NCCL 版本经常要切换，C++ 绑定每次换版本都要重编译，太死板。

结论是：**用纯 Python 的 `ctypes` 直接调 `libnccl.so` 的 C 函数**，既能在 CUDA Graph 捕获期间安全地发起通信（因为只走最纯粹的 NCCL 调用 + 一个 stream），又能通过环境变量 `VLLM_NCCL_SO_PATH` 或代码里的 `so_file` 灵活切换 NCCL 版本，无需重编译。

这层封装分为两段：`pynccl_wrapper.py` 负责「把 C 函数搬进 Python」（纯绑定，不含业务逻辑），`pynccl.py` 负责「用这些绑定建通信域、做通信」（业务封装）。此外，`pynccl.py` 还提供了 `StatelessP2PProcessGroup`——一个不污染 PyTorch 全局状态、基于 `TCPStore` 的元数据通道，专门用于跨节点（PD 分离）建立 NCCL 通信域时交换 `ncclUniqueId`。

> 注意：本模块讲的是「LightLLM 自己持有的、可用于 CUDA Graph 的 NCCL 封装」。它并不是日常 TP 推理 all-reduce 的唯一通路——常规 all-reduce 走的是 `communication_op.py` 里基于 `torch.distributed` 的 `CustomProcessGroup`（见 4.2/4.3）。`PyNcclCommunicator` 主要用于 PD 分离的跨节点 KV `send/recv`，以及任何需要在 CUDA Graph 内嵌通信的场景。

#### 4.1.2 核心流程

建立一个 `PyNcclCommunicator` 的流程是一次经典的「握手建域」：

```text
rank 0:  ncclGetUniqueId()       → 得到 128 字节的 unique_id
          (通过 torch broadcast 或 StatelessP2P.send_obj 广播给其它 rank)
所有 rank: ncclCommInitRank(world_size, unique_id, rank)  → 得到 communicator
rank 0 在自己的 device 上建 communicator，并做一次小 all_reduce 预热
```

通信域建好后，`all_reduce` 的调用就是把输入张量的 `data_ptr()`（显存指针）连同元素数、数据类型、归约算子、communicator、CUDA stream 一股脑塞给 NCCL 的 C 函数 `ncclAllReduce`：

```text
all_reduce(in_tensor):
    断言 in_tensor.device == self.device      # 通信域绑定在特定 GPU 上
    out_tensor = empty_like(in_tensor)
    ncclAllReduce(in_ptr, out_ptr, numel, dtype, op, comm, stream)
    return out_tensor
```

`send/recv` 同理，只是换成点对点的 `ncclSend/ncclRecv` 并指明 `dst/src`。

#### 4.1.3 源码精读

**(1) 找到 NCCL 动态库**。`find_nccl_library` 按「先环境变量、再随 PyTorch 自带的库」的顺序定位 `libnccl.so.2`（CUDA）或 `librccl.so.1`（ROCm）：

[文件路径:L56-L76](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/pynccl_wrapper.py#L56-L76) —— 该函数返回 `libnccl.so.2`，由后续 `ctypes.CDLL` 加载。

**(2) torch↔nccl 类型映射**。NCCL 用整数枚举标识数据类型与归约算子，`ncclDataTypeEnum.from_torch` 把 `torch.bfloat16` 映射成 `ncclBfloat16=9` 等：

[文件路径:L97-L133](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/pynccl_wrapper.py#L97-L133) —— 这张映射表保证 LightLLM 的 bf16/fp16 张量能被 NCCL 正确解释。

**(3) 用 ctypes 加载并映射函数**。`NCCLLibrary` 把每个 NCCL C 函数声明成一个 `Function(name, restype, argtypes)`，再用 `ctypes.CDLL` 加载库后逐个 `getattr` 设上 `restype/argtypes`：

[文件路径:L258-L289](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/pynccl_wrapper.py#L258-L289) —— 用类级字典 `path_to_library_cache` / `path_to_dict_mapping` 做缓存，避免重复加载同一份 `.so`。

每个调用都经过 `NCCL_CHECK` 把非 0 返回值翻译成异常：

[文件路径:L294-L297](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/pynccl_wrapper.py#L294-L297) —— 统一的错误码检查，把 NCCL 的 `ncclResult_t` 转成可读字符串。

**(4) 建通信域**。`PyNcclCommunicator.__init__` 完成握手建域：rank 0 取 uniqueId、广播给所有 rank、各自 `ncclCommInitRank`、并做一次小 all_reduce 预热：

[文件路径:L182-L220](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/pynccl.py#L182-L220) —— 关键点：`with torch.cuda.device(device)` 确保 communicator 绑定到指定 GPU；`ncclCommInitRank` 传入 `world_size/unique_id/rank`；末尾 `all_reduce(data)` + `stream.synchronize()` 是预热，让 NCCL 提前建好连接。

注意它对 `world_size == 1` 直接置 `disabled=True` 返回（[pynccl.py:164-L167](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/pynccl.py#L164-L167)），单卡场景不建域。

**(5) all_reduce / send / recv**。`all_reduce` 校验张量与本通信域同设备后，直接调 C 函数 `ncclAllReduce`，传 `data_ptr()`、`numel`、类型枚举、算子枚举、communicator、stream：

[文件路径:L225-L249](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/pynccl.py#L225-L249) —— 注意它是**非原地**的（out_tensor 是新分配的），且 `stream` 默认取 `current_stream()`，这正是它能在 CUDA Graph 里安全重放的关键——通信被钉在一条确定的 stream 上。

`send/recv` 是点对点版本，用于 PD 分离的 KV 页传输：

[文件路径:L251-L285](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/pynccl.py#L251-L285) —— `ncclSend/ncclRecv` 需要显式指定对端 `dst/src`。

**(6) StatelessP2PProcessGroup：跨节点建域的元数据通道**。PD 分离时，prefill 节点与 decode 节点分属不同 PyTorch 进程组，没法用 `torch.distributed` 的全局域。`StatelessP2PProcessGroup` 用一个 `TCPStore` 在两个节点间传 `ncclUniqueId` 等元数据（`send_obj`/`recv_obj` 用 pickle 序列化后写进 store），从而让两端各建一个 NCCL 通信域：

[文件路径:L109-L128](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/pynccl.py#L109-L128) —— `create(...)` 是个无状态工厂：不调 `init_process_group`、不污染全局状态，所以可以按需为任意两个节点配对建域。

它在 PD NCCL 传输器里的真实用法见 [nccl_kv_transporter.py:385-L392](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/nccl_kv_transporter.py#L385-L392)：用 `StatelessP2PProcessGroup.create` 拿到 group，再 `PyNcclCommunicator(group, tp_idx)` 建通信域，之后 `comm.send(page_tensor, dst=1)` / `comm.recv(page_tensor, src=0)` 把 KV 页在 P/D 节点间搬运（[nccl_kv_transporter.py:305](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/nccl_kv_transporter.py#L305) 与 [nccl_kv_transporter.py:362](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/nccl_kv_transporter.py#L362)。

#### 4.1.4 代码实践

1. **实践目标**：验证 `pynccl_wrapper.py` 能正确加载 NCCL 库并取到 `ncclUniqueId`，理解「纯 ctypes 绑定」的最小可运行形态。
2. **操作步骤**：
   - 打开 [pynccl_wrapper.py:420-L424](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/pynccl_wrapper.py#L420-L424) 的 `if __name__ == "__main__"` 块，它调 `torch.cuda.set_device(0)` 后跑 `test_ncclGetUniqueId()`。
   - 在装有 GPU 与 `libnccl.so.2` 的环境执行：
     ```bash
     python -m lightllm.distributed.pynccl_wrapper
     ```
3. **需要观察的现象**：脚本打印一串 128 个字节（如注释所示形如 `[34, -16, 23, 83, ...]`），不抛异常即表示 `NCCLLibrary` 成功加载 NCCL 库、`ncclGetUniqueId` 调用成功。
4. **预期结果**：见到 128 字节的 unique id 打印且无异常。若无 GPU 或缺 NCCL 库，会在 `ctypes.CDLL(so_file)` 处抛错并打印「Failed to load NCCL library ...」日志（[pynccl_wrapper.py:268-L279](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/pynccl_wrapper.py#L268-L279)）。
5. 若环境无 GPU，无法运行，请标注「待本地验证」，但可静态阅读 `test_ncclGetUniqueId`（[pynccl_wrapper.py:405-L417](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/pynccl_wrapper.py#L405-L417)）确认它只做「取 id → 断言非 None」两件事。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `PyNcclCommunicator.all_reduce` 要 `assert in_tensor.device == self.device`？不检查会怎样？

**参考答案**：NCCL communicator 在创建时绑定到一块特定 GPU（`ncclCommInitRank` 在 `torch.cuda.device(device)` 上下文里调用），它只能操作同设备上的张量。若张量在别的 GPU 上，NCCL 会触发「illegal memory access」。这个断言把这类错误从「难定位的 GPU 崩溃」前置成清晰的 Python 异常（见 [pynccl.py:231-L234](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/pynccl.py#L231-L234)）。

**练习 2**：`StatelessP2PProcessGroup` 相比 `torch.distributed.init_process_group` 解决了什么问题？

**参考答案**：`init_process_group` 是全局调用，一旦 A、B 已建组，就无法再让 C、D 加入或重新配对。`StatelessP2PProcessGroup.create` 是无状态的，每次返回一个独立对象，可按需为任意两个节点（如 PD 分离的某对 P/D）配对建 NCCL 域，互不污染（见 [pynccl.py:113-L128](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/pynccl.py#L113-L128) 的 docstring）。

### 4.2 集合通信统一接口：communication_op

#### 4.2.1 概念说明

`pynccl.py` 提供的是「能塞进 CUDA Graph 的底层 NCCL 调用」，但日常 TP 推理的 all-reduce 并不需要这么「原始」——它只需要在行并行层尾把各 rank 的部分和加起来。LightLLM 在 `communication_op.py` 里再包一层**统一集合通信接口**，动机有三：

1. **短路 `world_size==1`**：单卡或单 DP 组时直接返回，避免无谓的集合调用。`_is_single_group` 专门做这个判断。
2. **可替换的 all-reduce 后端**：在 NVLink 直连的小规模节点上，自定义归约（FlashInfer / SymmMem）比通用 NCCL 更快，需要一条派发链。
3. **多通信域管理**：microbatch overlap 需要两套独立通信域让通信并发，需要一个 `DistributeGroupManager` 按 `group_size` 批量建域。

这一层的核心抽象是 `CustomProcessGroup`：它持有一个 NCCL `device_group`（来自 `torch.distributed`），外加两个可选的自定义归约器 `symm_mem_reduce` / `flashinfer_reduce`；对外暴露统一的 `all_reduce / all_gather_into_tensor`。模块级函数 `all_reduce / all_gather / reduce_scatter_tensor / broadcast` 则是面向调用方的稳定入口，内部按 group 类型派发。

#### 4.2.2 核心流程

「每个 rank 建立 NCCL 通信域」的完整链路如下（从启动期到一次 all-reduce）：

```text
ModeBackend.init_model
  ├── init_distributed_env(kvargs)          # dist_utils.py
  │     ├── 设置 global_rank/world_size/dp 等环境变量
  │     └── dist.init_process_group("nccl", tcp://host:port, rank, world_size)  # 建全局 NCCL 域
  │           └── 末尾一次 dist.all_reduce 预热
  ├── dist_group_manager.create_groups(group_size)   # communication_op.py
  │     └── for i in range(group_size):
  │           CustomProcessGroup()
  │             ├── device_group = create_new_group_for_current_dp("nccl")  # 为本 DP 组切子域
  │             ├── (可选) dp_prefill_balance_group = create_dp_special_inter_group("nccl")
  │             ├── autotune_group = new_group(全 rank, backend="gloo")
  │             ├── init_symm_mem_reduce()     # 有 nvlink 且 ws∈{2,4,6,8} 时启用
  │             └── init_flashinfer_reduce()   # 同条件，建 gloo cpu 组
  └── 推理时：infer_state.dist_group = dist_group_manager.get_group(microbatch_index)
        ↓
层内 _tpsp_reduce(input, infer_state)
  └── all_reduce(input, group=infer_state.dist_group)   # 模块级函数
        └── _is_single_group? → 短路返回
        └── isinstance(group, CustomProcessGroup)? → group.all_reduce(input)
              └── 派发链: FlashInfer → SymmMem → NCCL
```

`create_new_group_for_current_dp` 的关键在于：它遍历每个 DP 组的 rank 列表，调 `dist.new_group(ranks, backend="nccl")`，只把「当前进程所属的那个组」返回给本 rank：

[文件路径:L244-L251](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/dist_utils.py#L244-L251) —— 这样每个 rank 拿到的 `device_group` 恰好是「本 DP 组（即本 TP 组）」的通信域，TP all-reduce 就在这组内进行。

`init_distributed_env` 建全局域并预热：

[文件路径:L127-L158](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/dist_utils.py#L127-L158) —— 末尾的 `_a = torch.zeros([1]).to(...); dist.all_reduce(_a)` 是经典预热，触发 NCCL 懒建立的连接。

#### 4.2.3 源码精读

**(1) CustomProcessGroup 的构造与自定义归约开关**：

[文件路径:L55-L69](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/communication_op.py#L55-L69) —— `_support_custom_allreduce` 要求 `has_nvlink()` 且 `dp_world_size in [2,4,6,8]`：自定义归约只在「NVLink 直连的偶数小规模节点」上有收益，跨机或大 world_size 仍走 NCCL。

**(2) 派发链 all_reduce**：

[文件路径:L93-L101](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/communication_op.py#L93-L101) —— 顺序是 `FlashInfer → SymmMem → NCCL`：小消息优先 FlashInfer（oneshot lamport），中等消息走 SymmMem（NVLS），都不命中或被禁用则回退 `dist.all_reduce`（NCCL）。

**(3) 模块级 all_reduce 的短路与派发**：

[文件路径:L223-L235](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/communication_op.py#L223-L235) —— 先 `_is_single_group` 短路（单组直接 return），再按 `op==SUM` 走 `group.all_reduce`（享受自定义后端）或退回 `dist.all_reduce`（带 `op` 参数的路径不走自定义后端）。`all_gather_into_tensor` / `reduce_scatter_tensor` / `broadcast` 结构对称（[communication_op.py:238-L298](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/communication_op.py#L238-L298)）。

**(4) _is_single_group**：

[文件路径:L301-L305](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/communication_op.py#L301-L305) —— 对 `CustomProcessGroup` 用 `dp_world_size==1` 判定，对原生 `ProcessGroup` 用 `dist.get_world_size`，单组时让上层直接跳过通信。

**(5) DistributeGroupManager.create_groups**：

[文件路径:L118-L127](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/communication_op.py#L118-L127) —— 按 `group_size`（overlap 开启时为 2，否则 1）建 N 个 `CustomProcessGroup`，受 `--disable_symm_mem_allreduce` / `--disable_flashinfer_allreduce` 两个开关控制是否初始化自定义后端。模块末尾的 `dist_group_manager = DistributeGroupManager()`（[communication_op.py:308](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/communication_op.py#L308)）是全局单例。

**(6) backend 的调用点**：`base_backend.py` 在 `init_model` 里调 `init_distributed_env(kvargs)` 与 `dist_group_manager.create_groups(group_size)`：

[文件路径:L118-L123](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L118-L123) —— `group_size` 由是否开启 microbatch overlap 决定（2 或 1）。

此外 backend 还为「节点内多 rank 协同读取共享内存命令」建了 `node_nccl_group`：

[文件路径:L210](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L210) —— 用 `create_new_group_for_current_node("nccl")` 建节点内 NCCL 域，配合 `broadcast` 让非 master rank 跟随 master 读取命令缓冲（[base_backend.py:394](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L394)）。

#### 4.2.4 代码实践

1. **实践目标**：把「每个 rank 建立 NCCL 通信域」的链路在源码里走一遍，画出从 `init_process_group` 到 `infer_state.dist_group` 的完整调用链。
2. **操作步骤**：
   - 在 [base_backend.py:118-L123](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L118-L123) 标出 `init_distributed_env` 与 `create_groups` 两个调用点。
   - 跟进 `init_distributed_env`（[dist_utils.py:127-L158](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/dist_utils.py#L127-L158)），找到 `dist.init_process_group("nccl", ...)` 与预热 `all_reduce`。
   - 跟进 `create_groups`（[communication_op.py:118-L127](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/communication_op.py#L118-L127)）→ `CustomProcessGroup.__init__` → `create_new_group_for_current_dp("nccl")`（[dist_utils.py:244-L251](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/dist_utils.py#L244-L251)）。
   - 在 [basemodel.py:386](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L386) 找到 `infer_state.dist_group = dist_group_manager.get_group(microbatch_index)`，确认层内通信用的就是这个 group。
3. **需要观察的现象**：链路上「全局域（init_process_group）→ 本 DP 组子域（new_group）→ CustomProcessGroup 包装 → infer_state.dist_group」四段逐级缩小，每段各管一类通信。
4. **预期结果**：能写出一张「通信域 → 建立函数 → 用途」对照表，例如：全局 NCCL 域（init_process_group，预热/兜底）/ 本 DP 组域（create_new_group_for_current_dp，TP all-reduce）/ 节点内域（create_new_group_for_current_node，协同读命令）/ overlap 第二域（get_group(1)，第二 microbatch 通信）。
5. 多机/多卡环境如不便实跑，标注「待本地验证」，但源码阅读部分可独立完成。

#### 4.2.5 小练习与答案

**练习 1**：`dist_group_manager.create_groups(group_size=2)` 会建出几个通信域？为什么是 2？

**参考答案**：建 2 个 `CustomProcessGroup`，每个内部又各自 `create_new_group_for_current_dp("nccl")` 形成一个独立的 NCCL 子域。`group_size=2` 是因为开启了 microbatch overlap（`enable_decode_microbatch_overlap` 或 `enable_prefill_microbatch_overlap`），需要两套**独立通信域**才能让两个 microbatch 的集合通信在同一批 GPU 上并发（同一通信域上的集合操作会串行排队），见 [base_backend.py:120-L123](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L120-L123)。

**练习 2**：模块级 `all_reduce` 为什么对 `op != ReduceOp.SUM` 的情况退回 `dist.all_reduce` 而不走 `group.all_reduce`？

**参考答案**：`CustomProcessGroup.all_reduce` 只实现了 SUM 的自定义后端派发（FlashInfer/SymmMem 都只做求和），非 SUM（如 MAX/MIN）没有自定义实现，故退回 `dist.all_reduce(input_, op, group.device_group, async_op)` 保证正确性（见 [communication_op.py:231-L235](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/communication_op.py#L231-L235)）。

### 4.3 all-reduce 多后端实现：flashinfer / symm_mem / NCCL

#### 4.3.1 概念说明

NCCL 是通用集合通信库，覆盖各种拓扑与规模，但在「**单节点内、NVLink 直连、小到中等消息**」这个细分场景下，它不是最快的。原因有二：

- NCCL 的 all-reduce 走多步 ring/tree 算法，对小消息的「启动开销」摊销不划算。
- 现代 NVLink GPU（Hopper/Blackwell）支持 **NVLS / multimem** 硬件归约——多张卡的显存被映射成一块「多播内存」，硬件一次写入即可被多卡读到并求和，省去多步软件通信。

于是 LightLLM 在 `CustomProcessGroup.all_reduce` 里设了一条派发链：**FlashInfer（最小消息）→ SymmMem（中等消息）→ NCCL（大消息/兜底）**。三者职责互补：

- **FlashInferAllReduce**：用 flashinfer 的 trtllm oneshot lamport 内核，专为**很小**的消息优化，受 `max_bytes` 上限约束（按算力×world_size 查表）。
- **SymmMemAllreduce**：用 torch 的对称内存（`torch.distributed._symmetric_memory`），走 NVLink SHARP / NVLS multimem 硬件归约，覆盖**中等**消息，受 `max_size` 上限约束。
- **NCCL**：兜底，处理大消息或不满足自定义前置条件的场景。

「是否启用」「是否对当前张量使用」由两层判断：构造期的 `_support_custom_allreduce`（节点级硬条件）与运行期的 `should_use`（逐张量的大小/dtype/连续性判断）。

#### 4.3.2 核心流程

派发决策发生在每一次 `CustomProcessGroup.all_reduce(input_)` 调用：

```text
all_reduce(input_):
  if flashinfer_reduce is not None and flashinfer_reduce.should_use(input_):
      input_.data = flashinfer_reduce.all_reduce(input_)    # 小消息快路径
      return
  if symm_mem_reduce is not None and symm_mem_reduce.should_use(input_):
      symm_mem_reduce.all_reduce(input_)                    # 中等消息，原地
      return
  return dist.all_reduce(input_, group=device_group)        # NCCL 兜底
```

`should_use` 的核心是「消息字节数是否落在该后端的阈值区间」。两个后端都按 `(compute_capability, world_size)` 查一张表，得到本后端能处理的最大字节数：

- FlashInfer 表 `_FI_ALLREDUCE_MAX_BYTES`：例如 `9.0`/`world_size=2` 上限 512KiB，`8` 时降到 128KiB（[flashinfer_all_reduce.py:31-L35](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/flashinfer_all_reduce.py#L31-L35)）。
- SymmMem 表 `SYMM_MEM_ALL_REDUCE_MAX_SIZES`：例如 `9.0`/`world_size=2` 上限 64MiB（[symm_mem_all_reduce.py:21-L25](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/symm_mem_all_reduce.py#L21-L25)）。

由于 FlashInfer 先判、阈值更小，小消息被它截走；超过 FlashInfer 阈值但小于 SymmMem 阈值的中等消息走 SymmMem；再大就 NCCL。

> **TP 下 all-reduce 在哪些算子之后被调用？** 这才是本模块与实践任务的落点。在行并行层（沿输入维切分的矩阵乘）之后必须 all-reduce，因为每个 rank 只算了部分和。具体到 Llama：`o_proj`（注意力输出投影）之后、`down_proj`（FFN 第二层）之后。在 TPSP 模式下，这两次 all-reduce 被替换为 `reduce_scatter`（求和后再切片）；非 TPSP 模式下才是真正的 all-reduce。调用入口是 `_tpsp_reduce`，它内部按 `enable_tpsp_mix_mode` 二选一。

#### 4.3.3 源码精读

**(1) 派发链本体**：

[文件路径:L93-L101](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/communication_op.py#L93-L101) —— 注意 FlashInfer 是**非原地**（`input_.data = fi.all_reduce(input_)`，赋回 data 保持张量身份不变），SymmMem 是**原地**（直接改 input_），NCCL 走 `dist.all_reduce` 也是原地。

**(2) FlashInfer 后端**。构造期按算力表决定是否启用、查 `max_bytes`；`should_use` 判 dtype∈{bf16,fp16}、2D、连续、且字节数 < `max_bytes`；`all_reduce` 调 `flashinfer_comm.allreduce_fusion`：

[文件路径:L118-L136](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/flashinfer_all_reduce.py#L118-L136) —— `_ensure_workspace` 按hidden_dim 懒建 workspace，dtype/hidden 变了才重建。

**(3) SymmMem 后端**。构造期 `torch_symm_mem.empty` 建一块对称内存 buffer 并 `rendezvous` 完成多卡映射，按 world_size 选 `multimem`（NVLS 硬件归约）或 `two_shot`（软件两段）；`all_reduce` 把 input 拷进 buffer、调 `multimem_all_reduce_`/`two_shot_all_reduce_`、再拷回：

[文件路径:L75-L92](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/symm_mem_all_reduce.py#L75-L92) —— `should_use` 要求 `nbytes % 4 == 0` 且 `< max_size`，下界由派发顺序隐式保证（FlashInfer 先截小消息，见注释 [symm_mem_all_reduce.py:81-L83](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/symm_mem_all_reduce.py#L81-L83)）。

**(4) all-reduce 在 TP 算子之后的真实调用点**。Llama 层推理里，`_get_o` 在 `o_proj.mm(input)` 之后调 `_tpsp_reduce`，`_ffn` 在 `down_proj.mm(...)` 之后调 `_tpsp_reduce`：

[文件路径:L99-L116](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L99-L116) —— 这正是「行并行层尾必须归约」的体现：`o_proj`（行并行）后归约、`down_proj`（行并行）后归约；而 `_get_qkv` 里的 `q_proj`/`kv_proj`、`_ffn_tp` 里的 `gate_up_proj` 是列并行，无需归约，只在入口做 `_tpsp_allgather`。

**(5) _tpsp_reduce 的二选一**：

[文件路径:L63-L83](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/base_layer_infer.py#L63-L83) —— 开 TPSP 时走 `reduce_scatter_tensor`（求和后切片，输出 shape 是输入的 `1/W`），不开时走 `all_reduce`（每个 rank 拿完整求和结果）。二者都经模块级函数派发，最终落到 `CustomProcessGroup`/`dist`。

**(6) _tpsp_allgather 与 _tpsp_sp_split**（配合理解通信点全貌）：

[文件路径:L53-L61](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/base_layer_infer.py#L53-L61) 与 [文件路径:L85-L98](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/base_layer_infer.py#L85-L98) —— `_tpsp_allgather` 在列并行算子入口把 SP 切片拼回完整 token；`_tpsp_sp_split` 在层首把 token 按 sp_rank 切成 `1/W`。三者共同把一次大 all-reduce 拆成「层首 split + 层内 allgather + 行并行尾 reduce_scatter」。

#### 4.3.4 代码实践

1. **实践目标**：说清 TP 下 all-reduce（或 TPSP 下的 reduce_scatter）究竟跟在哪些算子后面，并验证派发链的短路行为。
2. **操作步骤**：
   - 在 [llama/transformer_layer_infer.py:99-L116](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L99-L116) 标出两处 `_tpsp_reduce` 调用，分别紧跟 `o_proj.mm` 与 `down_proj.mm`。
   - 在 [base_layer_infer.py:63-L83](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/base_layer_infer.py#L63-L83) 确认：`enable_tpsp_mix_mode=True` 走 `reduce_scatter_tensor`，`False` 走 `all_reduce`。
   - 用 `--tp 2` 启动一个小模型（如 llama），分别加 `--enable_tpsp_mix_mode` 与不加，在 [communication_op.py:93-L101](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/communication_op.py#L93-L101) 的三档派发处加临时日志（`print` 命中哪个分支），观察 decode 时小张量命中 FlashInfer/SymmMem 的频率。
3. **需要观察的现象**：decode 阶段单 token、hidden 维较小的行并行输出，消息字节数往往落在 FlashInfer 或 SymmMem 阈值内，命中自定义后端；prefill 阶段大 batch 时消息变大，回退 NCCL。
4. **预期结果**：能回答「TP 下 all-reduce 调用在 `o_proj` 与 `down_proj` 之后（行并行层尾）；TPSP 模式下替换为 `reduce_scatter`，并在层首/列并行入口配 `allgather`/`sp_split`」。若无法实跑多卡，标注「待本地验证」，但可静态完成调用点标注。
5. 注意：临时日志属于示例代码，验证后请还原，勿提交。

#### 4.3.5 小练习与答案

**练习 1**：为什么 FlashInfer 后端要 `input_.data = fi.all_reduce(input_)` 而不是直接 `input_ = fi.all_reduce(input_)`？

**参考答案**：因为上层（`_tpsp_reduce` / 模块级 `all_reduce`）持有的张量引用是 `input_` 本身。若直接 `input_ = ...` 只会改局部变量、调用方的引用不变；而 `input_.data = ...` 在原地替换底层存储，保持张量对象身份不变，调用方立刻看到归约结果（见 [communication_op.py:95-L97](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/communication_op.py#L95-L97) 与 FlashInfer 类 docstring [flashinfer_all_reduce.py:45-L48](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/flashinfer_all_reduce.py#L45-L48)）。

**练习 2**：`_support_custom_allreduce` 为什么要求 `dp_world_size in [2, 4, 6, 8]`？

**参考答案**：自定义归约依赖 NVLink 直连与 NVLS/multimem 硬件，这些只在**单节点内偶数小规模**（典型 2/4/6/8 卡）拓扑下有收益且可用。超出此范围（如跨机或大 world_size）时，NVLink 拓扑不保证、硬件归约也不适用，应回退 NCCL（见 [communication_op.py:68-L69](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/communication_op.py#L68-L69)）。

**练习 3**：`_tpsp_reduce` 在不开 TPSP 时调用的 `all_reduce`，与开 TPSP 时的 `reduce_scatter_tensor`，输出形状有何不同？

**参考答案**：不开 TPSP 时 `all_reduce` 是每个 rank 拿到**完整**求和结果（形状不变）；开 TPSP 时 `reduce_scatter_tensor` 是求和后**切片**，每个 rank 只拿到 `1/W` 的结果（输出 token 维 = 输入 token 维 / world_size），见 [base_layer_infer.py:67-L82](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/base_layer_infer.py#L67-L82)。后者之所以可行，是因为 TPSP 下一层入口会用 `_tpsp_allgather` 把切片拼回完整。

## 5. 综合实践

**任务**：以一次 `tp=2` 的 Llama decode 前向为例，把「通信域建立 → 层内通信点 → 后端派发」三段串成一份带行号的追踪报告。

要求完成以下三件事：

1. **通信域建立链**（对应 4.2）：从 [base_backend.py:118-L123](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L118-L123) 出发，写出 `init_distributed_env` → `dist.init_process_group("nccl")` → `create_new_group_for_current_dp` → `CustomProcessGroup` → `infer_state.dist_group` 的链路，标注每一步建立的通信域用途。若 `tp=2` 且不开 overlap，`group_size` 是多少？建了几个 `CustomProcessGroup`？

2. **层内通信点**（对应 4.3）：在 [llama/transformer_layer_infer.py:79-L116](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L79-L116) 标出一层 transformer 里**所有**集合通信点，分清：
   - 列并行算子入口的 `_tpsp_allgather`（`_get_qkv`、`_ffn`）；
   - 行并行算子尾的 `_tpsp_reduce`（`_get_o` 的 `o_proj` 后、`_ffn` 的 `down_proj` 后）；
   - 分别说明开/不开 `--enable_tpsp_mix_mode` 时它们落到 `all_reduce` 还是 `reduce_scatter/all_gather`。

3. **后端派发**（对应 4.1/4.3）：在 [communication_op.py:93-L101](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/communication_op.py#L93-L101) 标注三档派发链，估算 decode 下 `o_proj` 输出（shape 约 `[1, hidden]`，bf16）的字节数，对照 [flashinfer_all_reduce.py:31-L35](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/flashinfer_all_reduce.py#L31-L35) 与 [symm_mem_all_reduce.py:21-L25](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/symm_mem_all_reduce.py#L21-L25) 判断会命中哪一档；并说明若该张量改走 `PyNcclCommunicator.all_reduce`（例如被包进 CUDA Graph 的 PD 传输路径），它为什么不经过这条派发链（提示：`PyNcclCommunicator` 直接调 `ncclAllReduce`，不经过 `CustomProcessGroup`）。

**预期产出**：一份含三段的追踪报告，每段引用具体文件与行号，并能回答上述括号内的小问。多卡环境不可用时，第 1、2 段可纯静态完成，第 3 段的「实际命中哪一档」标注「待本地验证」即可。

## 6. 本讲小结

- LightLLM 用**纯 Python `ctypes`** 封装 NCCL（`pynccl_wrapper.py` → `pynccl.py`），核心动机是让通信能塞进 **CUDA Graph 捕获**——`torch.distributed.all_reduce` 内夹带的额外 CUDA API 会破坏图重放，而直接调 `ncclAllReduce` + 固定 stream 则安全。
- `PyNcclCommunicator` 完成「rank 0 取 `ncclUniqueId` → 广播 → 各 rank `ncclCommInitRank` → 预热」的握手建域，提供 `all_reduce/send/recv`；`StatelessP2PProcessGroup` 用 TCPStore 做无状态元数据通道，是 PD 分离跨节点建域的基石。
- `communication_op.py` 是面向调用方的**统一接口层**：`CustomProcessGroup` 持有 NCCL 子域 + 可选自定义归约器，`DistributeGroupManager` 按 `group_size` 建多套通信域（支撑 microbatch overlap），模块级函数对 `world_size==1` 短路、再按 group 类型派发。
- all-reduce 有**三档派发链** `FlashInfer → SymmMem → NCCL`，按消息大小与节点拓扑选后端：小消息走 FlashInfer trtllm lamport、中等消息走 SymmMem NVLS/multimem 硬件归约、大消息或不满足条件回退 NCCL；自定义后端只在 NVLink 直连的 `{2,4,6,8}` world_size 下启用。
- **TP 下 all-reduce 跟在行并行层之后**：Llama 的 `o_proj`、`down_proj` 之后调 `_tpsp_reduce`；开 TPSP 时它替换为 `reduce_scatter`，并在层首/列并行入口配 `_tpsp_allgather`/`_tpsp_sp_split`，把一次大 all-reduce 拆成更小更可重叠的通信。
- 每个 rank 的 NCCL 通信域是分层建立的：`init_process_group` 建全局域 → `create_new_group_for_current_dp` 切本 DP 组子域 → `CustomProcessGroup` 包装（含自定义后端）→ 经 `dist_group_manager.get_group(microbatch_index)` 挂到 `infer_state.dist_group` 供层内使用。

## 7. 下一步学习建议

- **u6-2 microbatch overlap 与 TPSP 混合并行**：本讲只讲了「两套通信域从哪来」，那篇讲义讲「两套通信域怎么被用来重叠通信与计算」，建议紧接着读，把 `_tpsp_*` 三个原语放进 overlap 的双 infer_state 上下文里理解。
- **u7-1 PD 分离部署与 KV 迁移**：本讲提到的 `PyNcclCommunicator` + `StatelessP2PProcessGroup` 的真实用武之地在那篇讲义，可对照 [nccl_kv_transporter.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/pd/nccl_kv_transporter.py) 看一次 KV 页的 `send/recv` 全流程。
- **u7-3 数据并行与负载均衡**：本讲的 `create_new_group_for_current_dp` / `create_dp_special_inter_group` 为 DP 模式建域，那篇讲义讲这些域如何配合 `DpQueue` 与均衡器分发请求，可把通信域与调度策略对上。
- **继续阅读源码**：`lightllm/distributed/communication_op.py` 的 `DistributeGroupManager.new_deepep_group`（[L135-L195](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/distributed/communication_op.py#L135-L195)）展示了 MoE 专家并行（EP）下 DeepEP buffer 的建立，是通信与 MoE 的交汇点，可作为进阶阅读。
