# u9-l2 OmniPlanner 与专家重排实现

## 1. 本讲目标

上一讲（u9-l1）我们弄清了 OmniPlacement「为什么存在」：路由是模型学出来的，热门专家被过度选中导致 EP 组内负载不均，而 MoE 层耗时由最慢的 rank 决定。本讲深入源码，回答「它怎么做到的」。读完本讲，你应该能够：

1. 追踪一次完整的重排调用链：激活统计 → 计算新排布 → 生成变更指令 → 权重搬运 → 映射表热更新。
2. 解释 physical→logical 专家映射的三张核心数据结构：placement pattern、`pos_to_ep`、`selector`。
3. 说清重排为什么能不中断服务：Python 推理主线程与 C++ 重排 worker 线程如何用 `buf_ready_flag` 握手、用分批指令限制单卡同时只有一份权重在搬家。
4. 指出 placement_handler 与 vLLM 侧补丁 `patch_eplb_parallel` 的衔接点在哪里。

## 2. 前置知识

本讲默认你已读过 u9-l1（OmniPlacement 原理与启用）与 u2-l4（PatchManager 补丁机制），并了解 u3-l3 的 MoE 专家并行。在此基础上补充三个概念：

- **逻辑专家 vs 物理专家**：模型 checkpoint 里有 256 个「逻辑专家」（expert id 0~255）；开启 EPLB 后每个 rank 需要部署「基础额度 + 冗余额度」个「物理槽位」，冗余槽位是热门逻辑专家的副本。路由打分永远在逻辑空间进行，真正执行计算时必须换算到物理槽位。
- **pybind11 C++ 扩展**：omni-eplb 不纯是 Python。`setup.py` 把 `omni_placement/cpp/` 下的 C++ 源码编译成名为 `omni_placement.omni_placement` 的扩展模块，Python 侧 `from . import omni_placement` 拿到的 `Placement`、`ClusterActivation`、`do_placement_optimizer` 等都是 C++ 对象的绑定。重活（HCCL 通信、权重搬运、贪心求解）都在 C++ 里。
- **单例（singleton）**：`OmniPlanner` 用元类实现单例——同一进程内无论构造多少次，拿到的都是同一个实例。这保证了一个推理进程只有一份激活计数张量、一个重排线程。

另外回忆 u2-l4 的结论：omni-npu 的运行时补丁用 `setattr` 替换 vLLM 的类方法，本讲的 `patch_eplb_parallel.py` 正是用这套机制把 vLLM 的 `EplbState`「掏空」、换成驱动 OmniPlanner 的实现。

## 3. 本讲源码地图

本讲涉及两类代码：omni-eplb 组件本体（Python 规划层 + C++ 执行层），以及 omni-npu 组件中的两个挂载点。

| 文件 | 层次 | 作用 |
| --- | --- | --- |
| `components/omni-eplb/omni_placement/omni_planner.py` | Python 规划层 | OmniPlanner 单例：装配配置、专家映射、激活计数与 C++ Placement；向 MoE 层提供路由改写与激活记录接口 |
| `components/omni-eplb/omni_placement/expert_mapping.py` | Python 规划层 | 加载/校验三维 pattern，构建 `selector` 与物理位置表 |
| `components/omni-eplb/omni_placement/placement_handler.py` | Python↔C++ 桥 | 工厂函数：创建 C++ `Placement`/`ClusterActivation`、移交 MoE 权重、广播 HCCL rootinfo |
| `components/omni-eplb/omni_placement/cluster_status.py` | Python 规划层 | 集群状态的容器与「状态-动作」队列（当前主要作数据持有者） |
| `components/omni-eplb/omni_placement/cpp/placement_manager.cpp` | C++ 执行层 | 重排 worker 线程主循环、指令分批执行、与主线程握手 |
| `components/omni-eplb/omni_placement/cpp/placement_optimizer.cpp` | C++ 执行层 | 由「当前排布 + 激活」求「目标排布」，并生成变更指令序列 |
| `components/omni-eplb/omni_placement/cpp/include/placement_mapping.h` | C++ 执行层 | 物理位置↔逻辑专家映射表及 `selector` 热更新接口 |
| `components/omni-eplb/omni_placement/cpp/include/expert_activation.h` | C++ 执行层 | 集群激活采集：AllReduce 归约、滑动窗口 |
| `components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_eplb_parallel.py` | vLLM 挂载层 | 补丁替换 `EplbState`，把 vLLM 引擎步进钩到 OmniPlanner |
| `components/omni-npu/src/omni_npu/layers/quantization/compressed_tensors/compressed_tensors_moe.py` | omni-npu 挂载层 | MoE 量化方法里构造 planner，在前向中调用 `plan()` 与 `record_activation()` |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**planner 调度**（重排的时序与线程模型）、**专家映射**（physical↔logical 的表示）、**分布式算子**（权重搬运与激活归约）。

### 4.1 模块一：planner 调度 —— 双线程协作的重排引擎

#### 4.1.1 概念说明

OmniPlacement 的核心设计是**两个线程、两把尺子**：

- **Python 推理主线程**：跑 vLLM 引擎循环，每个调度步调用一次 `place_experts()`。它的职责只有一个——在「安全时机」把 C++ 线程已经搬进接收缓冲区的权重刷进正式权重区，并刷新映射表。判断安全时机的依据是原子标志 `buf_ready_flag_`。
- **C++ 重排 worker 线程**：由 C++ `Placement` 对象持有，周期性地（每轮睡眠 60 秒，源码注释写 "6 mins"，以代码 `collect_times = 60` 秒为准）做一轮完整的「采集激活 → 求解新排布 → 分批搬运权重 → 置起 `buf_ready_flag_` 等主线程确认」。

为什么不让主线程直接做？因为重排涉及跨卡 HCCL 权重拷贝，耗时且不可控；而推理步进是毫秒级的。把重排放到独立线程、用指令分批保证任意时刻每张卡最多只有一份专家权重处于「搬运中」，推理流就几乎无感——这就是「不中断服务」的来源。

#### 4.1.2 核心流程

启动与每步驱动的时序：

```
MoE 层构造(omni-npu)
  └─ init_eplb() ──> OmniPlanner(...)          # 单例首建：读 config.yaml、建 ExpertMapping、
                                               # 建 npu_activation_count 张量、建 C++ Placement
vLLM EplbState.add_model（被补丁替换）
  └─ OmniPlanner()                             # 单例复用
     └─ init_dram_weights()                    # 把 MoE 权重指针移交给 C++ MoEWeights

vLLM 引擎第一步（EplbState.step，被补丁替换）
  └─ planner.start_dynamic_optimize_expert_load_balance()   # 启动 C++ worker 线程（仅一次）
vLLM 引擎每一步（EplbState.step）
  └─ planner.place_experts()
       └─ C++ do_placement_optimizer(placement)
            ├─ buf_ready_flag_ 为 False → 直接返回（本轮没有新排布）
            └─ 为 True → copy_from_queue_to_hbm()      # 接收缓冲 → 正式权重
                        update_selector(layer_update)   # 刷新逻辑→物理映射表
                        buf_ready_flag_ 置回 False      # 下降沿，通知 worker 线程
```

C++ worker 线程每轮主循环（伪代码）：

```
loop until should_stop:
    if should_pause: sleep; continue
    dump_and_collect()                       # 归约全集群激活增量，落盘（可选）
    changeInstructions = optimizer_->optimize()   # 当前排布+激活 → 变更指令列表
    if not empty and check_instructions(通过):
        placement_handle_instrucions(changeInstructions)  # 分批搬运（见 4.3）
        # 每批结束后置 buf_ready_flag_ = True，自旋等待主线程 place_experts() 清零
    collect()                                # 清零本轮已计入的激活，开始下一窗口
    sleep(60s)
```

#### 4.1.3 源码精读

**① 单例与装配。** `OmniPlanner` 用元类 `OmniPlannerMeta` 保证进程内唯一实例：[omni_planner.py:L30-L45](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/omni_planner.py#L30-L45)。这是「MoE 层先构造一次、vLLM 补丁再取一次」能收敛到同一对象的前提。

构造函数里完成五件事：读 `config.yaml`（默认取组件根目录下那份）→ 初始化分布式身份 → 建 `ExpertMapping`（pattern 加载与校验，见 4.2）→ 建 `ClusterStatus` → 调 `_init_placement_manager()` 建 C++ 对象：[omni_planner.py:L58-L126](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/omni_planner.py#L58-L126)。注意 L95：`enable_dynamic` 决定走动态热更新还是仅静态 pattern / 仅 dump 模式；`config.yaml` 里它默认为 `False`，生产开启动态重排需在 P/D 各自的 config 中置 `True`（见 [config.yaml:L25-L27](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/config.yaml#L25-L27)）。

**② 激活计数张量与 C++ 对象的创建。** `_init_placement_manager` 先在 NPU 上开一块 `(num_layers, max_num_deployed_expert_per_rank)` 的 int64 张量 `npu_activation_count` 并 `mark_static`（防止 torch.compile 把它当动态形状），再把它的裸地址包成 C++ `Tensor` 传给 `ClusterActivation`；`enable_dynamic` 为真时才创建 `Placement`（重排管理器）：[omni_planner.py:L218-L249](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/omni_planner.py#L218-L249)。关键点：**Python 与 C++ 共享同一块 NPU 显存**，MoE 前向在 Python 侧往这张表累加，C++ 线程直接读同一地址做归约，零拷贝。

**③ worker 线程主循环。** C++ 侧 `Placement::placement_manager` 就是 4.1.2 伪代码的实体：[placement_manager.cpp:L523-L611](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/placement_manager.cpp#L523-L611)。逐段看：

- L526-L535：`enable_new_context` 时线程自建 ACL context（避免与主线程争用），否则复用创建线程时的 context——这解释了 config 里 `enable_new_context` 开关的含义。
- L543-L552：用 rootinfo 初始化**独立 HCCL 通信域**并等待权重 HBM 初始化完成（见 4.3）。
- L562 与 L599-L600：循环首尾各一次 `activations_->collect(...)`，后者注释写明用途——**清掉旧排布下的激活**，让下一窗口统计的是新排布的负载。
- L585：`optimizer_->optimize()` 产出全集群的变更指令（见 4.3.2）。
- L590-L597：指令先过一致性校验 `check_instructions`，再交给 `placement_handle_instrucions` 执行。

线程的启动口在 `start_thread()`（L613-L627），由 Python 的 `start_dynamic_optimize_expert_load_balance()` 触发：[omni_planner.py:L254-L257](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/omni_planner.py#L254-L257)。

**④ 主线程侧的「收割」。** `place_experts()` 只有一行——调 C++ 的 `do_placement_optimizer`：[omni_planner.py:L310-L323](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/omni_planner.py#L310-L323)。C++ 实现在 [placement_manager.cpp:L655-L672](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/placement_manager.cpp#L655-L672)：

1. `buf_ready_flag_` 为 False 直接返回——绝大多数调度步都是这种「无事可做」的快路径，开销仅一次原子读。
2. 为 True 时：`copy_from_queue_to_hbm()` 把接收缓冲区里收到的权重拷进正式槽位；`update_selector(...)` 只刷新本轮真正变动的层；最后 CAS 把标志位落回 False，形成对 worker 线程的「确认回执」。

**⑤ 与 vLLM 的衔接点。** 补丁文件头部注释直接给出用法（`OMNI_NPU_VLLM_PATCHES="EPLBEngineConfig,EPLBSharedFusedMoE"`，本补丁注册名是 `EPLBState`）：[patch_eplb_parallel.py:L5-L6](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_eplb_parallel.py#L5-L6)。补丁用 `@register_patch("EPLBState", EplbState)` 注册，声明替换 8 个符号：[patch_eplb_parallel.py:L23-L31](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_eplb_parallel.py#L23-L31)。三个关键方法：

- `build_initial_global_physical_to_logical_map`（L38-L47）：初始物理→逻辑映射就是「前 N 个物理位对应 N 个逻辑专家，冗余位按 `i % num_routed_experts` 回绕」——与 u9-l1 讲的「初始均匀部署」一致。
- `add_model`（L49-L63）：模型注册时构造单例 `OmniPlanner()` 并调用 `init_dram_weights()` 把权重移交 C++；`model_config.runner != "draft"` 保证 MTP draft 模型不参与（与 u3-l5 呼应）。
- `step`（L65-L77）：vLLM 每个引擎步都会调 `EplbState.step`，补丁把它变成「首步启线程、每步 `place_experts()`」——**这就是 vLLM 与 OmniPlanner 的心跳连接**。其余 `rearrange`/`start_async_loop`/`recv_state`/`get_eep_state`（L79-L114）全部置空或返回 None：vLLM 自带的 EPLB 异步重排逻辑被整体旁路，重排决策权完全交给 C++ planner。

#### 4.1.4 代码实践

**实践：追踪「一步重排」的完整调用链并画出双线程时序图**（源码阅读型，无需 NPU）。

1. **实践目标**：把 4.1.2 的时序图落到具体「文件:行号」，验证自己对线程模型的理解。
2. **操作步骤**：
   - 从 [patch_eplb_parallel.py:L65-L77](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_eplb_parallel.py#L65-L77) 的 `step` 出发，写下它调用的两个 planner 方法及其行号。
   - 打开 [placement_manager.cpp:L565-L606](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/placement_manager.cpp#L565-L606)，标出 worker 线程循环中：睡眠位置、`optimize()` 调用、指令执行、以及哪一行之后主线程才可能看到 `buf_ready_flag_ = True`（提示：在 `placement_handle_instrucions` 里找 `compare_exchange_strong`，[placement_manager.cpp:L490-L508](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/placement_manager.cpp#L490-L508)）。
   - 画出两条并行泳道（主线程 / worker 线程），用箭头标出 `buf_ready_flag_` 的置位与清零这两个跨线程事件。
3. **需要观察的现象**：`buf_ready_flag_` 的置位发生在 worker 线程「一批指令搬运完成、还剩后续批次」的间隙，而不是全部批次完成后（L461-L468：最后一条指令才同时置 `need_wait_main`）。思考：为什么每个中间批次都要让主线程刷一次权重？
4. **预期结果**：得出结论——分批 + 逐批确认让「搬运中的权重」始终只占每卡一个槽位，主线程在批间隙刷新已到位的权重，单批阻塞上限受 L498-L507 的 3 秒超时保护。
5. 本实践为纯源码阅读，结论可直接从代码推出；若要在真实服务中验证日志（如 `[EPLB-Info] EPLB Worker thread created`），需 NPU 环境，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果 `place_experts()` 在某步抛异常（未被调用），`buf_ready_flag_` 会怎样？worker 线程会死锁吗？

**答案**：会卡在自旋等待，但有超时保护：worker 线程在 [placement_manager.cpp:L498-L507](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/placement_manager.cpp#L498-L507) 等待超过约 3 秒（600 次 × 5ms）就打 warning 并调用 `sync_round_shakehand(-1, -1)` 尝试推进，不会永久死锁，但该轮重排可能不完整。

**练习 2**：`EplbState` 补丁为什么要把 `rearrange`、`recv_state` 等方法替换成空实现，而不是让它们和 OmniPlanner 共存？

**答案**：vLLM 原生 EPLB 的重排由 scheduler 驱动（收集 `global_expert_loads`、异步 shuffle 权重），与 OmniPlacement 的 C++ 自主线程模型是两套互斥的状态机。若不置空，两套机制会同时搬权重、同时改映射。补丁在 [patch_eplb_parallel.py:L79-L114](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_eplb_parallel.py#L79-L114) 把 vLLM 侧路径全部短路，只保留 `step` 作为每步驱动钩子。

**练习 3**：`npu_activation_count` 为什么要 `torch._dynamo.mark_static`？

**答案**：它由 Python 前向累加（见 4.2.4）、C++ 线程归约，形状永不变。若被图编译当作动态输入，每次捕获都要重新建图；`mark_static`（[omni_planner.py:L222-L227](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/omni_planner.py#L222-L227)）把它标记为静态地址，保证 aclgraph/GE 图可以安全内联这段累加。

### 4.2 模块二：专家映射 —— physical↔logical 的三张表

#### 4.2.1 概念说明

「专家映射」回答两个方向的问题：

- **logical → physical**（路由时）：router 给出逻辑专家 id，本卡哪些物理槽位存着它？——由 `selector` 表回答。
- **physical → logical**（重排时）：第 3 卡第 7 个槽位现在放的是哪个逻辑专家？——由 C++ 侧 `pos_to_ep` 表回答（`update_pos_to_ep` 接口见 [placement_mapping.h:L168](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/include/placement_mapping.h#L168)）。

两张表的「底座」都是 **placement pattern**：三维 0/1 矩阵 `pattern[deviceid][layerid][epid]`，1 表示「层 layerid 的逻辑专家 epid 部署在设备 deviceid」。同一 `(layerid, epid)` 在多台设备上同时为 1 就是冗余副本——语义注释见 [config.yaml:L1-L19](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/config.yaml#L1-L19)。

物理槽位的**全局编号**约定为：`global_physical_id = rank × max_num_deployed_expert_per_rank + 本地槽位号`。这个公式在 `logical_to_all_physical` 里能看到实体：[omni_planner.py:L283-L299](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/omni_planner.py#L283-L299)。

#### 4.2.2 核心流程

**加载期（Python）**：

```
config.yaml 的 pattern_path
  ├─ 为 null  → build_basepattern()：按 world_size 均分专家，生成基准 pattern
  └─ 有路径  → np.load 读 .npy，严格校验 shape == (world_size, 层数, 专家数)
  → placement_pattern（NPU 张量）+ placement_pattern_cpu（CPU 副本）
  → epid_position_init = cumsum(pattern, dim=2) - 1     # 每设备每层的「槽位号」表
  → selector：形状 (层数, 专家数, max_redundant_per_expert) 的 int32 表
  → 把三块内存地址传给 C++ PlacementMapping（C++ 与 Python 共享同一份内存）
```

**推理期（每步，热路径）**：

```
router 输出 topk_ids（逻辑专家 id，shape [num_tokens, top_k]）
  → planner.plan() 查 selector：
     · enable_rank_round_robin=True：embedding(topk_ids, selector[layer]) —— 每专家只留一个物理位
     · 否则：selector[layer][逻辑id, token序号 % 该专家副本数] —— 副本间轮询分摊
  → 得到物理 expert id，交给 prepare_permute/apply_experts（u3-l3 的三段式 MoE）
```

**重排期（C++，低频）**：指令执行时逐条 `update_pos_to_ep` 改物理→逻辑表；整轮结束后 `update_selector` 把变动层的逻辑→物理表整层重算，并 `to_device` 同步到 NPU 上的 `selector` 张量：[placement_mapping.cpp:L409-L435](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/placement_mapping.cpp#L409-L435)。同一块 NPU 内存既是 C++ 的视图也是 Python `planner.selector[layer]` 的视图——所以**映射更新对推理代码零感知**，下一次 `plan()` 查表自然拿到新结果。

#### 4.2.3 源码精读

**① pattern 的加载与兜底。** `_load_placement_pattern_with_validation`：[expert_mapping.py:L66-L104](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/expert_mapping.py#L66-L104)。`pattern_path` 为 null 时用 `build_basepattern` 生成均匀切分的基准矩阵并打 warning（L84-L86）；有路径时 `np.load` 后做严格 shape 断言（L92-L93）——pattern 与物理部署不匹配会在第一时间报错，这正是 u9-l1 强调「pattern 必须匹配物理部署」的实现点。仓库 `patterns/` 目录下提供了 505B 等现成 pattern（如 `pangu505B_8K_decode_64dieA3.npy`）。

**② 三张表的构建。** `_init_expert_mapping`：[expert_mapping.py:L34-L58](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/expert_mapping.py#L34-L58)。

- L40-L43：`selector` 形状二选一——轮询模式下每专家 1 列，副本轮询模式下 `max_redundant_per_expert` 列。
- L47-L48：`epid_position_init = cumsum(pattern, dim=2) - 1`。技巧：对 0/1 矩阵沿专家轴做前缀和再减一，1 的位置恰好得到「该专家在此设备的槽位号」，0 的位置得到 -1。一行向量化替代了逐专家计数循环。
- L49-L58：构造 C++ `PlacementMapping`，传的是 `data_ptr()` 裸地址——C++ 直接读写 Python 张量内存，无序列化开销。

**③ 查询接口。** `is_expert_on_current_rank` 返回 `(是否存在, 本地槽位号)` 二元组：[expert_mapping.py:L110-L133](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/expert_mapping.py#L110-L133)。非 MoE 层回落到默认连续切分检查 `_default_deployment_check`（L135-L146）。omni-npu 的权重加载（决定某逻辑专家的权重是否落到本卡、落到第几个本地槽位）就靠这两个函数。

**④ 冗余额度查询。** `get_num_of_redundant_experts` 有两条来源：动态模式优先读 config 的 `max_redundant_per_rank`；静态模式则从 pattern 实测——该设备该层部署数减去基础额度：[expert_mapping.py:L148-L171](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/expert_mapping.py#L148-L171)。omni-npu 侧拿它扩容本地专家数：`layer.local_num_experts += self.num_of_redundant_experts`（[compressed_tensors_moe.py:L79-L88](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/quantization/compressed_tensors/compressed_tensors_moe.py#L79-L88)），模型层再据此推导 `n_physical_experts` 与 `_expert_map`（[pangu_v2_moe.py:L295-L304](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L295-L304)）——这就是 u9-l1 说的「扩容本地专家数」在代码里的落点。

**⑤ 每层偏移表。** `_calc_expert_offset_each_layer` 预计算「第 j 层第 i 卡之前所有卡部署的专家总数」：[expert_mapping.py:L205-L220](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/expert_mapping.py#L205-L220)。层间不均匀部署时（u9-l1 特性二），同一专家在不同层的全局偏移不同，这张 cumsum 表让权重加载能一次定位。

**⑥ 热路径的查表改写。** `plan_normal_experts` 的两条分支：[omni_planner.py:L325-L359](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/omni_planner.py#L325-L359)。

- 轮询分支（L354-L355）：`torch.nn.functional.embedding(token_expert_ids, expert_mapping)`——把 `selector[layer]`（形状 `(专家数, 1)`）当查找表，一次 embedding 完成全部 token 的逻辑→物理换算。
- 副本轮询分支（L356-L358）：`redundant_bias` 是预生成的 `arange(100000)` 列向量（[omni_planner.py:L129-L132](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/omni_planner.py#L129-L132)），用 `token 序号 % 副本数` 在 `selector` 的副本维里轮选——同一热门专家的多个副本被均匀打到。

**⑦ omni-npu 侧的两个消费点。** MoE 前向中：先 `planner.plan(...)` 改写 `topk_ids`（[compressed_tensors_moe.py:L288-L305](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/quantization/compressed_tensors/compressed_tensors_moe.py#L288-L305)），再在 prepare_permute 得到每专家 token 数后 `planner.record_activation(...)` 累加激活：[compressed_tensors_moe.py:L342-L346](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/quantization/compressed_tensors/compressed_tensors_moe.py#L342-L346)。`record_activation` 的实体是对 `npu_activation_count[layer]` 做模加（模上界取 1e16 防溢出），可选切到 `npu_stream_switch('21')` 旁路流避免阻塞主计算流：[omni_planner.py:L508-L514](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/omni_planner.py#L508-L514)。

#### 4.2.4 代码实践

**实践：在 CPU 上手工复现「pattern → selector/槽位表」的构建**（示例代码，无需 NPU）。

1. **实践目标**：用 20 行 Python 复现 `_init_expert_mapping` 的核心计算，直观理解三张表。
2. **操作步骤**：以下为示例代码（重写自 [expert_mapping.py:L34-L58](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/expert_mapping.py#L34-L58)，仅依赖 numpy/torch CPU）：

   ```python
   import numpy as np, torch

   world_size, layers, experts = 2, 1, 4
   # 手工构造带冗余的 pattern：rank0 多放一份专家 1
   pattern = np.zeros((world_size, layers, experts), dtype=np.int32)
   pattern[0, 0, [0, 1, 2]] = 1   # rank0: 专家 0,1,2
   pattern[1, 0, [1, 3]] = 1      # rank1: 专家 1(冗余), 3
   t = torch.tensor(pattern)

   max_redundant = int(t.to(torch.int64).sum(dim=0).max())   # = 2（专家1有两个副本）
   selector = torch.zeros(layers, experts, max_redundant, dtype=torch.int32)
   num_redundant = torch.zeros(layers, experts, dtype=torch.int32)
   slot = t.cumsum(dim=2) - 1        # epid_position_init：1 处为槽位号，0 处为 -1
   print(slot[0, 0])                 # tensor([-1,  0,  1, -1]) / rank1: [-1, 0, -1, 0]
   print("max_deployed_per_rank =", max(t.sum(dim=2).tolist()))   # 3
   ```
3. **需要观察的现象**：`cumsum - 1` 后每行的非 -1 值恰是 0,1,2… 的连续槽位号；专家 1 的副本数是 2，其他是 1。
4. **预期结果**：对照 [expert_mapping.py:L47-L48](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/expert_mapping.py#L47-L48) 与 L31（`max_num_deployed_expert_per_rank` 的整除取大），你手工算出的值与公式一致。再尝试把 selector 第 `(:, 1, :)` 行按「rank0 副本在槽位 1、rank1 副本在槽位 0 + 1×3（rank1 的全局偏移）」填出来，验证 `global = rank × max_deployed + slot` 的编号公式（`max_deployed` 取 3，即两卡都按 3 槽对齐）。
5. 示例代码未在项目环境运行过，逻辑与源码一一对应，可本地直接执行验证。

#### 4.2.5 小练习与答案

**练习 1**：`pattern_path: null` 时服务还能跑吗？与配置了 pattern 有什么差别？

**答案**：能跑。`build_basepattern` 会生成均匀切分的基准矩阵并打 warning（[expert_mapping.py:L84-L86](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/expert_mapping.py#L84-L86)），相当于「无冗余的初始部署」；后续动态重排照常进行。配 pattern 的意义在于用离线统计预热一个更优起点（静态模式则完全依赖它）。

**练习 2**：`selector` 与 C++ `pos_to_ep` 是互逆关系吗？

**答案**：方向上互逆但不完全对称。`pos_to_ep` 是「物理全局位置 → 逻辑专家 id」一一对应表，用于重排时核对与更新；`selector` 是「逻辑专家 id → 物理全局位置」且带副本维（可能一列多值，或副本轮询下保留多个候选），用于路由查表。二者的维护点也不同：前者在指令执行时逐条 `update_pos_to_ep`，后者在整轮后由 `update_selector` 重建变动层。

**练习 3**：副本轮询分支用 `token 序号 % 副本数` 而不是按负载选副本，会不会不均衡？

**答案**：在单个 batch 内近似均匀（token 序号连续），且重排线程会持续根据激活统计调整副本数量与位置，使「热门专家获得更多副本」；两层机制配合后宏观均衡。真正的按负载细粒度分流属于 `apply_best_load_balance`（强制负载均衡开关，[omni_planner.py:L519-L541](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/omni_planner.py#L519-L541)）的范畴，它直接改写 topk_ids 做轮转分配，是压测工具性质的能力（`BEST_EP` 环境变量开启）。

### 4.3 模块三：分布式算子 —— 权重搬运与激活归约

#### 4.3.1 概念说明

重排的「最后一公里」是把一份专家权重从源卡搬到目标卡。这要求一套**独立于推理通信的分布式设施**：

- **独立 HCCL 通信域**：推理的 TP/EP 集合通信走 vLLM 的进程组；重排搬运不能复用它（会与推理流互相阻塞），因此 C++ `Distribution` 用 rootinfo 单独 `HcclCommInit` 一条通道。rootinfo 的产生与广播由 `get_hccl_root_info` 完成：rank 0 调 `HcclGetRootInfo` 生成，再经 torch.distributed 广播给所有 rank（[placement_manager.cpp:L674-L685](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/placement_manager.cpp#L674-L685) 的 C++ 端 + [placement_handler.py:L135-L156](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/placement_handler.py#L135-L156) 的 Python 广播端）。广播前先做一次 `distribution_warmup()` 预热（一个 int64 的 dummy broadcast，[distributed_ops.py:L42-L45](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/distributed_ops.py#L42-L45)），避免其他 rank 在 warmup 期间访问通信后端。

- **ChangeInstruction（变更指令）**：重排不是「整体重排」而是增量指令流。每条指令描述一次槽位变更：层号、源/目标的 rank 与全局位置、专家 id、操作类型（ADD=目标位放入该专家，REMOVE=目标位清空）、轮次 round。指令生成与执行的解耦，使「计算新排布」（纯 CPU 贪心）与「执行搬运」（HCCL + 显存拷贝）可以独立优化。

- **集群激活归约**：每个 rank 的 `npu_activation_count` 是本地视角；求解需要全集群视角。`ClusterActivation::collect/dump_and_collect` 用独立通信域做 AllReduce/汇聚，并在主机侧维护 `last_count_ptr_`（上次值）与 `delta_experts_counts_`（增量），配合每个逻辑专家一个长度 20 的滑动窗口（`ExpertActivation` 环形缓冲，[expert_activation.h:L30-L53](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/include/expert_activation.h#L30-L53)）平滑毛刺。类接口清单见 [expert_activation.h:L55-L151](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/include/expert_activation.h#L55-L151)。

#### 4.3.2 核心流程

一轮重排的完整数据流（对应实践任务的协作图）：

```
【采集】MoE 前向 record_activation()                      (omni-npu Python)
          │ 累加到 npu_activation_count（共享显存）
          ▼
【归约】ClusterActivation::dump_and_collect()             (C++ worker 线程)
          │ 独立 HCCL 域汇聚 → 全集群每槽位激活增量
          ▼
【求解】PlacementOptimizer::optimize()                     (C++ worker 线程)
          │ extract_input_data: 当前排布(pos_to_ep) + 激活
          │ load_balancer_->optimize_placement: 贪心求目标排布 g
          │ 逐层 generate_layer_instructions: f → g 的差分指令
          │ rank0 打印 Imbalance (Before/After) 对比表
          ▼
【执行】placement_handle_instrucions()                    (C++ worker 线程)
          │ reorder_instructions（同源同目标合并、按轮次排序）
          │ 按「每 rank 每批最多 1 条」切批
          │ 每批: update_pos_to_ep → moe_weight_->replacement 入队
          │       → hccl_batch_send 批量发权重 → sync_round_shakehand 握手
          │       → buf_ready_flag_ = True，等主线程确认
          ▼
【生效】主线程 place_experts() → do_placement_optimizer   (omni-npu Python 触发)
          │ copy_from_queue_to_hbm: 接收缓冲 → 正式权重槽位
          │ update_selector: 刷新变动层的逻辑→物理表（写共享 NPU 内存）
          │ buf_ready_flag_ = False
          ▼
【下一窗口】collect() 清零旧激活，继续统计新排布负载
```

其中「求解」的贪心主体在 [placement_optimizer.cpp:L472-L581](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/placement_optimizer.cpp#L472-L581)：`extract_input_data` 取当前排布与激活（L477），`load_balancer_->optimize_placement` 求目标排布（L480-L481），随后**逐层**切出差分指令（L508-L572），rank 0 还会打印每层的 Imbalance (Before/After) 表（L490-L506、L563-L569）——这张表是验证重排收益最直接的日志证据。

#### 4.3.3 源码精读

**① C++ 对象装配。** `create_placement_manager`：[placement_handler.py:L55-L87](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/placement_handler.py#L55-L87)。L69-L72 先解决 rootinfo，L75-L85 构造 `omni_placement.Placement`。C++ 构造函数里 `initialize_components` 依次 new 出三员大将：`Distribution`（独立 HCCL 通信）、`MoEWeights`（专家权重的宿主内存/HBM 管理）、`PlacementOptimizer`（求解器）：[placement_manager.cpp:L108-L140](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/placement_manager.cpp#L108-L140)。类成员与线程/锁的定义见 [placement_manager.h:L26-L149](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/include/placement_manager.h#L26-L149)。

**② 权重移交。** `init_dram_weights`（Python 侧包装）：先用正则过滤出 `.layers.N.*.experts` 的专家权重、把 dict 按 layer 转成 list、取每张量的地址转成 C 类型，最后调 `MoEWeights::init_weights`：[placement_handler.py:L29-L51](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/placement_handler.py#L29-L51)。此后**专家权重的真身在 C++ 管理的存储里**，Python 侧 `model.named_parameters()` 的张量成为视图——这是重排线程能自由搬权重而不惊动 PyTorch 的前提。补丁的 `add_model` 调的正是它（[patch_eplb_parallel.py:L49-L63](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_eplb_parallel.py#L49-L63)），`OmniPlanner.init_dram_weights` 再包一层并补上接收缓冲初始化：[omni_planner.py:L499-L503](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/omni_planner.py#L499-L503)。

**③ 指令校验与切批。** `check_instructions` 逐条核对源/目标位置与 `pos_to_ep` 的一致性，类型必须是 ADD/REMOVE：[placement_manager.cpp:L357-L373](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/placement_manager.cpp#L357-L373)。`placement_handle_instrucions` 先 `reorder_instructions` 重排指令序，再流式切批：累计每张卡被碰的次数（`rank_used`），一旦下一批会让某卡超过 `max_ins_one_batch_one_rank = 1` 就切批：[placement_manager.cpp:L418-L487](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/placement_manager.cpp#L418-L487)。**「每卡每批最多一条」就是"不中断服务"的量化保证**——任意时刻一张卡最多有一个槽位在换入换出。跨轮次（`inst.round` 变化）时置 `need_wait_main`，先置起 `buf_ready_flag_` 等主线程确认再继续（L461-L468、L488-L519）。

**④ 单批执行。** `placement_handle_one_batch`：[placement_manager.cpp:L375-L416](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/placement_manager.cpp#L375-L416)。REMOVE 只改映射（`update_pos_to_ep(layer, pos, -1)`，L392-L394）；ADD 改映射后，若本 rank 是源或目标，调 `moe_weight_->replacement(...)` 把这次拷贝登记进 HCCL 收发队列（L395-L409），本 rank 是目标时还需要预登记接收缓冲（L403-L404）。批尾统一 `hccl_batch_send()` 批量发出（L412-L415）——先攒后发，减少通信次数。**注意所有 rank 执行的是同一份全局指令列表**（optimize 的结果各 rank 一致），靠「与己无关就 continue」保证语义对齐。

**⑤ 激活采集对象。** `create_cluster_activation` 把共享显存张量包成 C++ `Tensor` 描述（裸地址 + 长度 + dtype 字符串），窗口大小固定 10：[placement_handler.py:L90-L130](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/placement_handler.py#L90-L130)。dump 模式（`enable_dump: true` + `dump_dir`）会把每轮激活落盘，Python 侧在 [omni_planner.py:L136-L140](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/omni_planner.py#L136-L140) 按时间戳建目录——这就是 u9-l1 说的「dump 激活 → 生成 pattern → 重启应用」静态模式的原料。

**⑥ 暂停与恢复。** 权重校准、图捕获等阶段不希望权重变动，`placement_pause/resume` 经 Python 透传到 C++ 的 `should_pause_` 标志：[omni_planner.py:L210-L216](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/omni_planner.py#L210-L216)、[placement_manager.cpp:L643-L653](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/placement_manager.cpp#L643-L653)。

#### 4.3.4 代码实践

**实践：从 C++ 单元测试反向理解分布式算子契约**（源码阅读型，无需 NPU）。

1. **实践目标**：不看 NPU 环境，通过 `cpp/test/` 的测试用例搞清 `Distribution`、`MoEWeights`、`PlacementMapping` 的接口契约。
2. **操作步骤**：
   - 打开 [components/omni-eplb/omni_placement/cpp/test/test_placement_manager.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/test/test_placement_manager.cpp)（目录下还有 `test_distribution.cpp`、`test_moe_weights.cpp`、`test_placement_optimizer.cpp` 等），找出测试如何在不拉起真实集群的情况下构造 `ChangeInstruction` 并断言执行结果。
   - 对照 [placement_optimizer.cpp:L584-L594](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/placement_optimizer.cpp#L584-L594)：`optimize(placement, activations)` 这个重载专供单元测试注入数据。回答：它与生产用的无参 `optimize()`（L472 起）差了哪两步？（提示：无参版多做 `extract_input_data` 和逐层 DebugInfo 统计。）
   - 列出 `Distribution` 在主循环中被调用的方法序列（`init_hccl_comm` → `set_stream` → `init_hccl_buffs` → 每批 `clear_hccl_buffs`/`hccl_batch_send` → `sync_round_shakehand` → 终止时 `release_recv_buffs`），在 [placement_manager.cpp:L537-L610](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/placement_manager.cpp#L537-L610) 逐个标行号。
3. **需要观察的现象**：测试用例构造的指令序列与 `placement_handle_instrucions` 的切批规则之间的关系（例如测试是否直接调 `placement_handle_one_batch` 绕过切批）。
4. **预期结果**：得到一张「Distribution 方法 × 调用时机 × 所在线程」三列表格；能说出收发缓冲（`init_recv_buf`/`copy_from_queue_to_hbm`）与 HCCL 批发送（`hccl_batch_send`）分别属于异步收货与异步上架两阶段。
5. 编译并运行这些 gtest 用例需要 CANN 环境，**待本地验证**；纯阅读部分结论可离线得出。

#### 4.3.5 小练习与答案

**练习 1**：为什么 rootinfo 要在 Python 侧广播，而不是 C++ 各自生成？

**答案**：`HcclGetRootInfo` 必须由一个节点生成、所有成员拿到**同一份** rootinfo 才能建出一条互认的通信域。C++ 侧 `GetPDRootInfo` 只负责生成（[placement_manager.cpp:L674-L685](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/placement_manager.cpp#L674-L685)），Python 侧 `get_hccl_root_info` 用已有的 torch.distributed（HCCL 后端）把 rank0 的 rootinfo 广播出去（[placement_handler.py:L135-L156](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/placement_handler.py#L135-L156)）——用旧通信域引导新通信域。

**练习 2**：指令执行为什么「REMOVE 只改映射、ADD 才搬权重」？

**答案**：REMOVE 表示目标槽位从此不再存放该专家，只需让 `pos_to_ep` 指向 -1，槽位上的旧数据无需物理擦除（下次 ADD 覆盖即可）。ADD 表示目标槽位要开始服务某专家，必须把该专家的权重从源槽位复制过来才算生效。对应代码：[placement_manager.cpp:L392-L409](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/placement_manager.cpp#L392-L409)。

**练习 3**：`init_dram_weights` 的过滤正则是 DeepSeek 命名风格（`.*\.layers\.(\d+)\..*\.experts`，[placement_handler.py:L13-L27](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/placement_handler.py#L13-L27)）。openPangu-2.0 的权重名能匹配上吗？

**答案**：openPangu-2.0 的 MoE 专家权重同样挂在 `model.layers.N.…experts.…` 路径下（u3-l1 讲过 ForCausalLM→Model→DecoderLayer→MOE 的前缀链），且层号语义一致（`layer - first_k_dense_replace` 得 MoE 层号，与 `get_deepseek_v3_moe_layer_idx` 同式，见 [omni_planner.py:L447-L477](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/omni_planner.py#L447-L477)），因此可复用；`first_k_dense_replace` 作为参数从模型配置传入（[patch_eplb_parallel.py:L60](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_eplb_parallel.py#L60)），适配不同模型只需换偏移值。若某新模型命名不同，需要替换过滤/取层函数——这也是把这两个函数做成参数而非硬编码的原因。

## 5. 综合实践

**任务：产出一份「一次重排」的全链路协作图 + 衔接点说明**（本讲 practice_task，源码阅读型为主，可选上机验证）。

1. **准备**：确保已按 u9-l1 完成 omni-eplb wheel 构建认知；打开本讲源码地图中的 5 个关键文件。
2. **画图**：以 4.3.2 的数据流为骨架，画一张包含以下角色的协作图，每个箭头标注「方法名 + 文件:行号」：
   - omni-npu MoE 前向（`record_activation`，[compressed_tensors_moe.py:L342-L346](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/quantization/compressed_tensors/compressed_tensors_moe.py#L342-L346)）；
   - 共享显存 `npu_activation_count`（[omni_planner.py:L222-L238](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/omni_planner.py#L222-L238)）；
   - C++ `ClusterActivation` → `PlacementOptimizer::optimize`（[placement_optimizer.cpp:L472-L581](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/placement_optimizer.cpp#L472-L581)）；
   - C++ `Placement::placement_handle_instrucions` → `MoEWeights::replacement` + `Distribution::hccl_batch_send`（[placement_manager.cpp:L375-L521](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/placement_manager.cpp#L375-L521)）；
   - vLLM `EplbState.step` 补丁 → `place_experts` → `do_placement_optimizer`（[patch_eplb_parallel.py:L65-L77](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_eplb_parallel.py#L65-L77) + [placement_manager.cpp:L655-L672](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/placement_manager.cpp#L655-L672)）；
   - 共享 `selector` 张量被 `update_selector` 刷新、被下一次 `plan()` 读取（[placement_mapping.cpp:L409-L435](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/placement_mapping.cpp#L409-L435)）。
3. **写衔接点说明**：用一段话回答「placement_handler 与 patch_eplb_parallel 在哪里接上」——补丁的 `add_model` 经 `OmniPlanner` 单例（首建于 MoE 层 `init_eplb`）调用 `init_dram_weights` 完成权重移交；补丁的 `step` 是主线程唯一的周期驱动点，经 `place_experts` 与 C++ worker 线程在 `buf_ready_flag_` 上握手。
4. **可选上机验证**（需 NPU 集群，**待本地验证**）：按 u9-l1 的 Guideline 拉起开启 EPLB 的服务，压测一段时间后在 rank0 日志中找 `Placement Optimization Summary` 表（[placement_manager.cpp:L490-L506](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/placement_manager.cpp#L490-L506)），记录某几层 Imbalance (Before/After) 的变化，与你图中「求解」环节对应。

## 6. 本讲小结

- **双线程模型**：Python 推理主线程只做轻量「收割」（`place_experts` → `do_placement_optimizer`），C++ worker 线程周期完成采集-求解-搬运；二者用原子标志 `buf_ready_flag_` 与 `sync_round_shakehand` 握手，这是不中断服务的机制核心。
- **三张映射表**：pattern 三维 0/1 矩阵是部署事实的底座；`pos_to_ep`（物理→逻辑）服务重排，`selector`（逻辑→物理，带副本维）服务路由；两表经共享 NPU 内存热更新，推理路径零改动感知。
- **热路径极轻**：每步 MoE 前向只多一次 `plan()` 查表改写与一次 `record_activation()` 模加；绝大多数调度步的 `place_experts()` 只是一次原子读后直接返回。
- **增量指令流**：重排以 ChangeInstruction（ADD/REMOVE）表达，「每卡每批最多一条 + 批间主线程确认」把搬运开销摊薄到多个调度步。
- **独立通信域**：rootinfo 引导的专用 HCCL 通道承担权重搬运与激活归约，与推理集合通信互不干扰。
- **vLLM 衔接极小化**：补丁只保留 `step` 一个驱动钩子，把原生 EPLB 的其余路径全部置空——决策权完全在 OmniPlanner。

## 7. 下一步学习建议

本讲完成了 omni-eplb 单元的源码精读。接下来建议：

1. 进入 u10-l1（Docker 镜像分层构建），理解 omni-eplb 的 wheel 如何进入推理镜像。
2. 进入 u10-l3（二次开发），把本讲的补丁衔接点知识与 PatchManager 扩展实践打通——试着写一个只在 EPLB 生效时打点的新补丁。
3. 若想继续深挖本组件，可阅读 [components/omni-eplb/omni_placement/cpp/dynamic_eplb_greedy.cpp](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-eplb/omni_placement/cpp/dynamic_eplb_greedy.cpp) 与 `expert_load_balancer.cpp`，弄清 `optimize_placement` 贪心的具体目标函数（本讲只讲了接口与输入输出），并结合 u10-l4 把 EPLB 组合进 505B 生产方案。
