# 硬件光线追踪单元（RTU）

## 1. 本讲目标

本讲拆解 Vortex 的硬件光线追踪单元（Ray-Tracing Unit，RTU，代号 PRISM）。RTU 是一个把 RISC-V 扩展成「异步 BVH 遍历加速器」的固定功能单元：warp 发射一条射线后可以继续跑或挂起，RTU 在旁边离线遍历加速结构，命中结果通过记分板化的寄存器窗口送回。

学完后你应当能够：

1. 说清 RTU「异步遍历 + 寄存器窗口 + 回调陷阱」的执行模型，以及它和图形固定功能栈（u10-l1）共享了什么。
2. 读懂设备侧 API `vx_raytrace.h`：`vx_rt_wtrace` / `vx_rt_wait` 这对「一条射线 + 一次等待」是如何折叠掉十几条 marshalling 指令的。
3. 在 SimX 中定位 RTU 的四个最小模块——SFU 前端 `rtu_unit.cpp`、BVH 遍历器 `rtu_walker.cpp`、相交测试 `rtu_isect.cpp`、集群编排器 `rtu_core.cpp`——并理解它们的协作。
4. 写出 ray-triangle（Möller–Trumbore）与 ray-AABB（slab）相交测试的数学，以及 SimX 如何用 `BoxPe` / `TriPe` 把它们折算成周期代价。
5. 理解 SimX 作为「功能 oracle」与 RTL 逐模块对应、共同维持 model parity（u7-l4）这条主线。

## 2. 前置知识

在进入 RTU 之前，请先建立这几个概念。如果你已经学过本路线的对应讲义，可以快速跳过。

**光线追踪与 BVH。** 光线追踪的核心运算是「一条射线和一堆几何体求交」。朴素做法是把射线和每个三角形都测一遍，复杂度 \(O(N)\)；当场景有百万三角形时不可行。BVH（Bounding Volume Hierarchy，层次包围盒）是一棵树：每个内部节点存一个轴对齐包围盒（AABB）罩住它下面所有几何体；叶节点存实际三角形。遍历时，若射线和某节点的 AABB 不相交，整棵子树直接剪枝。这样平均复杂度降到 \(O(\log N)\)。CW-BVH（Compressed-Wide BVH）是节点宽度为 4 或 6 的变体，子节点 AABB 用量化表示压缩，是 RTU 实际消费的格式。

**TLAS / BLAS。** 两层结构：顶层 TLAS（Top-Level AS）的叶节点是「实例（instance）」，每个实例带一个世界→对象的仿射变换并指向一个 BLAS；底层 BLAS（Bottom-Level AS）才是真正的几何体。射线进入实例时先做仿射逆变换转到对象空间，再遍历对应 BLAS。这让同一几何体可以被多次实例化而不复制顶点。

**从 u10-l1 继承的认知。** RTU 与图形固定功能（RASTER/TEX/OM）同属 `custom1`（`INST_EXT2 = 0x2B`）ISA 扩展，由 `VX_CFG_EXT_RTU_ENABLE` 门控，挂在 SFU 后面、与 TEX/OM 共享同一个「图形寄存器窗口」。u10-l1 讲过的「FF 单元经 SFU 路由、操作数经 per-warp 寄存器窗口交接」在这里完全复用。

**从 u6-l4 / u8-l1 继承的认知。** RTU 是 SFU 类单元（u6-l4 的「SFU 是分派器」结论直接适用：RTU op 经 SFU 扇出）；它通过 RTCache 读 BVH 数据，走的是 u8-l1 的缓存层次。

**Möller–Trumbore 与 slab 方法。** 这是两条经典求交公式，本讲 §4.5 会结合源码给出完整推导，这里只需知道：前者求射线-三角形交点并给出重心坐标 \( (u,v) \)，后者求射线-AABB 的进出区间。

## 3. 本讲源码地图

本讲涉及的文件集中在三处：

| 文件 | 作用 |
|---|---|
| [`docs/designs/ray_tracing_unit.md`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/ray_tracing_unit.md) | RTU 设计总文档：架构、ISA、RTL/SimX 模块清单、CW-BVH 格式、实现状态 |
| [`sw/kernel/include/vx_raytrace.h`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_raytrace.h) | 设备侧 kernel API：`vx_rt_wtrace` / `vx_rt_wait` / `vx_rt_cb_ret` 等内联函数 |
| [`sim/simx/rtu/rtu_unit.cpp`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_unit.cpp) | 每核 SFU 前端：v2 窗口 ABI、宏指令→微操作展开、WAIT 的 park/revive |
| [`sim/simx/rtu/rtu_walker.cpp`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_walker.cpp) | 场景遍历器：`FlatWalker`（平铺三角列表）与 `Bvh4Walker`（CW-BVH4/6 深度优先） |
| [`sim/simx/rtu/rtu_isect.cpp`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_isect.cpp) | 相交数学：ray-triangle / ray-AABB / 仿射逆变换，以及 `BoxPe`/`TriPe` 周期代价模型 |
| [`sim/simx/rtu/rtu_core.cpp`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_core.cpp) | 集群级编排：上下文池、SELECT/EXEC 两相流水、回调重整（ReformationEngine） |
| [`sim/simx/rtu/rtu_types.h`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_types.h) | 总线包 `RtuReq`/`RtuRsp`、槽位/通道状态结构 |
| [`VX_types.toml`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_types.toml) | 寄存器窗口槽位编号（`VX_RT_*`），软硬件共享 ABI 契约 |
| [`tests/raytracing/rt_smoke/kernel.cpp`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/raytracing/rt_smoke/kernel.cpp) | 最小可运行示例：一条 trace + 一条 wait |

辅助阅读：RTL 侧对应实现位于 [`hw/rtl/rtu/`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/rtu)（`VX_rtu_scheduler.sv`、`VX_rtu_box_pe.sv`、`VX_rtu_tri_pe.sv`、`VX_rtu_xform.sv` 等）。

## 4. 核心概念与源码讲解

### 4.1 RTU 整体架构：异步遍历引擎

#### 4.1.1 概念说明

RTU 的定位可以用一句话概括：**一个异步、SIMT 派发的光线追踪加速器**。这里的「异步」是关键词——它和 ALU/FPU 这种「当拍算完」的单元完全不同：

- 一个 warp 用一条 `TRACE2` 指令把射线「提交」给 RTU，立即拿到一个 **handle**（异步凭证）；
- warp 可以继续做无关工作（这就是异步带来的吞吐收益），也可以用 `WAIT2` 阻塞等待；
- RTU 在 warp 旁边离线遍历 BVH，完成后通过 **记分板化的寄存器窗口** 把命中属性（命中距离 \(t\)、重心坐标 \(u,v\)、图元 ID 等）送回；
- 若遍历过程中遇到需要可编程着色的情形（非不透明三角形、procedural AABB、closest-hit/miss shader），RTU 会 **yield**：让 warp 陷入一个异步回调陷阱（callback trap），运行对应的着色器，再用 `CB_RET` 释放上下文。

RTU 是「per-core SFU-class unit」，与图形单元共用核心与缓存，经共享的图形寄存器窗口和一个 RTCache 到达。遍历、实例变换、相交测试全部在 RTU 自己的处理单元（PE）上以定点/浮点完成——**绝不在 SIMT 核上做遍历**。

#### 4.1.2 核心流程

设计文档给出了一张高度浓缩的架构图，把整条数据通路讲清楚了：

[docs/designs/ray_tracing_unit.md:L37-L51](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/ray_tracing_unit.md#L37-L51) 描绘的流程可以用伪代码表示：

```
# warp 侧
stage 射线/配置 (SETW 写窗口)
handle ← vx_rt_wtrace(TRACE2)        # 提交射线，立即返回
... 可选：跑无关工作 ...
status ← vx_rt_wait(WAIT2, handle)   # 阻塞到 terminal
读命中属性 (GETW/GETWF)

# RTU 侧（异步）
scheduler: 上下文池 + 短栈 + 两相 SELECT/EXEC
  ├─ TLAS: instance 下降 + xform(世界→对象)
  ├─ box_pe:  slab 测试(量化子 AABB / raw / proc box)
  ├─ tri_pe:  Möller–Trumbore(fdivsqrt)
  └─ 叶节点 → commit hit | proc-AABB/非不透明 → CALLBACK yield
```

注意四个关键 PE：`VX_rtu_xform`（仿射变换）、`VX_rtu_box_pe`（slab 测试）、`VX_rtu_tri_pe`（三角形相交）、以及调度器 `VX_rtu_scheduler`。它们都消费 **CW-BVH**（宽度 4 或 6）。

#### 4.1.3 源码精读

架构总览见 [docs/designs/ray_tracing_unit.md:L27-L56](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/ray_tracing_unit.md#L27-L56)，这张图把 warp 侧的 trace/wait 与 RTU 内部的 scheduler/box_pe/tri_pe/xform 串成一条回路。

RTL 模块清单见 [docs/designs/ray_tracing_unit.md:L97-L131](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/ray_tracing_unit.md#L97-L131)，关键点：

- `VX_rtu_scheduler` 按 `VX_CFG_RTU_BVH_WIDTH` 选两种遍历器：**flat** 列表遍历器（WIDTH=0）与 **CW-BVH4/6** 遍历器（WIDTH=4/6）；持有 per-lane **上下文池**（`NUM_CTX = NUM_THREADS`）、**短栈**（`sp`）、两相 `SELECT`/`EXEC` 流水线，在 PE 之间时间复用各上下文。
- `VX_rtu_box_pe` 做 slab ray/AABB 测试，也处理 raw/procedural box。
- `VX_rtu_tri_pe` 做 Möller–Trumbore，依赖 `VX_fdivsqrt_unit`。
- `VX_rtu_xform` 做 TLAS 实例变换，**只用 FMA**（复用 `VX_fma_unit`，无新数据通路）。

关于「异步」的另一面——回调与 parked context——见 [docs/designs/ray_tracing_unit.md:L116-L131](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/ray_tracing_unit.md#L116-L131)：叶节点需要 any-hit/intersection/closest-hit/miss shader 时，调度器 yield，warp 陷入异步陷阱运行回调着色器，再用 `CB_RET` 释放。一个重要的工程结论：**回调陷阱里允许浮点运算**（靠陷阱前后的记分板快照/恢复），这是早期版本的限制，现已解除。

#### 4.1.4 代码实践

**实践目标**：建立「warp 提交 → RTU 异步遍历 → 结果经窗口送回」的全景图。

1. 打开 [docs/designs/ray_tracing_unit.md:L37-L51](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/ray_tracing_unit.md#L37-L51) 的架构图。
2. 用三种颜色分别标注：warp 侧动作（SETW/TRACE2/WAIT2/GETW）、RTU 内部 PE（scheduler/xform/box_pe/tri_pe）、以及两者之间的两条通道（记分板窗口、cb 总线）。
3. 回答：为什么「`rd` = scoreboard handle」这条注释意味着 `vx_rt_wait` 不会在 `vx_rt_wtrace` 真正完成前读到结果？（提示：记分板在 §4.3 详解。）

**预期结果**：你能用一张图说清「异步」体现在哪两个环节（trace 立即返回、wait 阻塞到 terminal），以及回调陷阱在哪条路径上触发。

#### 4.1.5 小练习与答案

**练习 1**：RTU 与图形固定功能（RASTER/TEX/OM）共享了哪两样东西？
**答案**：同一个 `custom1` opcode 扩展槽，以及同一个 per-core 图形寄存器窗口（`VX_gfx_window`）和 RTCache 访存路径。

**练习 2**：为什么 RTU 的遍历「绝不在 SIMT 核上做」？
**答案**：因为遍历、变换、相交都跑在 RTU 自己的 PE 上，SIMT 核只负责提交射线、等待结果、以及在回调陷阱里跑着色器；这正是固定功能加速器相对于「用核软遍历」的性能与能效来源（设计文档明确把「SIMT 软件遍历」列为被淘汰方向）。

---

### 4.2 设备侧 API：vx_raytrace.h 与寄存器窗口 ABI

#### 4.2.1 概念说明

`vx_raytrace.h` 是 kernel 侧用 RTU 的入口。它的设计哲学是 **把一堆 marshalling 折叠成最少的架构指令**：v1 时代要用十几条 `vx_gfx_set`/`vx_gfx_get` 逐字段搬运射线与命中属性；v2 用「寄存器窗口 ABI」折叠成 **一条 trace + 一条 wait**。

核心抽象有两个结构体：

- `vx_ray_t`：per-thread 射线几何，正好 8 个 float（origin 3 + dir 3 + tmin + tmax），按硬件约定钉死在浮点寄存器 `f0..f7` 这扇「射线窗口」上。
- `vx_hit_t`：wait 写回的命中属性，浮点（\(t,u,v\)）落 FP 寄存器堆，整数 ID（primitive/instance/geometry/custom）落 GP 寄存器堆——**类型分流，避免 `fmv` 转换**。

ISA v2 的 op 集都骑在 `custom1` 上，靠 `funct3`/`funct2` 区分：

| Op | funct3/funct2 | 用途 |
|---|---|---|
| `TRACE2` | 7 / 0 | 提交一条射线，`rd`=handle |
| `WAIT2` | 7 / 1 | 阻塞到 terminal，`rd`=status |
| `SETW` | 6 / 1 | 写一个窗口槽（暂存射线/配置） |
| `GETWF` | 6 / 2 | 读 `count` 个连续 **FP** 窗口槽 |
| `GETW` | 6 / 3 | 读 `count` 个连续 **GP** 窗口槽 |
| `CB_RET` | 6 / 0 | 释放本 lane 的 parked 回调上下文 |

（完整表格见 [docs/designs/ray_tracing_unit.md:L68-L76](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/ray_tracing_unit.md#L68-L76)。注意 v1（funct3=5）已退役，funct3=5 现归 TEX。）

#### 4.2.2 核心流程

窗口有 32 个槽（`VX_RT_SLOT_COUNT = 32`），RTU 占用的槽位在 `VX_types.toml` 里逐个编号：

[ray_tracing_unit.md:L78-L94](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/ray_tracing_unit.md#L78-L94) 指出 RTU 用 **对象射线槽 8..13 与命中属性槽 14..24**。对照 [VX_types.toml:L340-L407](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_types.toml#L340-L407)：

```
VX_RT_RAY_ORIGIN         = 0   # 世界射线 origin (0..2)
VX_RT_RAY_DIRECTION      = 3   # 世界射线 dir   (3..5)
VX_RT_T_MIN / T_MAX      = 6 / 7
VX_RT_OBJECT_RAY_ORIGIN  = 8   # 对象空间 origin (8..10)
VX_RT_OBJECT_RAY_DIRECTION = 11 # 对象空间 dir  (11..13)
VX_RT_HIT_T / BARY_U / BARY_V = 14 / 15 / 16
VX_RT_HIT_PRIMITIVE_ID   = 21
VX_RT_HIT_INSTANCE_ID    = 22
VX_RT_HIT_GEOMETRY_INDEX = 23
VX_RT_CB_HANDLE          = 30
VX_RT_SLOT_COUNT         = 32
```

这里有一条 **必须知道的「槽位重叠」不变量**：图形 fragment 载荷（槽 8..21）与 RTU 的对象射线+命中槽 **重叠**。正确性靠「按约定互斥」（一个 warp 不会同时持有活的 fragment 与 ray-query 状态），**不是硬件强制的**。这正是「在 fragment shader 里融合 ray query」目前被阻塞的原因（需重新规划槽位）。

#### 4.2.3 源码精读

**射线结构体与编译期布局守卫**。`vx_ray_t` 用一连串 `_Static_assert` 钉死字段必须精确映射到 `f0..f7`：

[vx_raytrace.h:L86-L104](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_raytrace.h#L86-L104)

```c
typedef struct {
  float origin[3];   // f0..f2
  float dir[3];      // f3..f5
  float tmin;        // f6
  float tmax;        // f7
} vx_ray_t;
_Static_assert(sizeof(vx_ray_t) == 8 * sizeof(float), ...);
```

这样一旦有人重排字段或加 padding，**编译期就失败**，而不是悄悄打乱 per-lane 射线窗口。

**trace 内联函数**。`vx_rt_wtrace` 把 per-trace 的 warp-uniform 配置（scene/payload/flags/cull）用 `vx_wgather` lane-pack 进一个寄存器，再把射线钉进 `f0..f7`，发出一条 `.insn r`：

[vx_raytrace.h:L144-L174](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_raytrace.h#L144-L174)

```c
register float r0 __asm__("f0") = ray->origin[0];
// ... f1..f7 ...
__asm__ volatile (".insn r %[op], 7, 0, %[hnd], %[cfg], x0"
    : [hnd]"=r"(handle)
    : [op]"i"(RISCV_CUSTOM1), [cfg]"r"(cfg),
      "f"(r0), "f"(r1), ... "f"(r7));
```

注意一个反直觉点：编码本身只命名 `rd`/`rs1`，`f0..f7` 是靠 **HW 约定**（解码器硬编码读 `f0..f7`）骑在操作数列表里，和 TCU 的 fragment 窗口同构。配置放在 gather 的 lane 1..3（非 self slot 0），保证即便 lane 0 被掩蔽（回调/递归收窄的 trace）scene 仍有效。

**wait 内联函数**。`vx_rt_wait` 故意拆成 **两个 op**：(1) `WAIT2`（funct2=1）单 op 阻塞，park/revive 与寄存器堆 get 路径一致，从而能在异步回调陷阱中存活；(2) `WAIT_WB`（funct2=3）非阻塞的命中窗口写回宏操作，记分板链在 status 字上，故只在 block 退休（terminal）后才发射：

[vx_raytrace.h:L189-L223](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_raytrace.h#L189-L223)

为什么不用一条 fused 指令？因为融合指令要在宏操作中途 park（arm 与写回之间），徒增 sequencer/记分板复杂度，还丢掉了 trace/wait 拆分本要换来的异步重叠——而省下的不过是一次取指。所以同步形式 `vx_rt_wtrace_sync`（[vx_raytrace.h:L275-L281](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_raytrace.h#L275-L281)）就是两条宏操作背靠背。

**最小用例**。看真实 kernel 怎么用：

[tests/raytracing/rt_smoke/kernel.cpp:L34-L49](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/raytracing/rt_smoke/kernel.cpp#L34-L49)

```c
vx_ray_t ray;
ray.origin[0] = arg->ray_origin[0]; // ... 填充射线
uint32_t h = vx_rt_wtrace(scene_lo, 0u, VX_RT_FLAG_OPAQUE, 0xffu, &ray);
vx_hit_t hit;
uint32_t sts = vx_rt_wait(h, &hit);
```

每个 lane 装配一条 per-thread 射线，一条 trace + 一条 wait，命中属性直接进 `hit`。这就是「~16-op marshalling 折叠成两条指令」的真实落点。

#### 4.2.4 代码实践

**实践目标**：在源码层面验证「窗口 ABI 把搬运折叠成了两条指令」。

1. 打开 [sw/kernel/include/vx_raytrace.h:L189-L223](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/kernel/include/vx_raytrace.h#L189-L223)。
2. 数一数 `vx_rt_wait` 内联函数里实际发出了几条 `.insn`（答案：3 条——1 条 WAIT2 block + 1 条 GETWF 读 \(t/u/v\) + 1 条 GETW 读 4 个 ID）。
3. 对照 [VX_types.toml:L348-L364](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_types.toml#L348-L364)，确认 `HIT_PRIMITIVE_ID`(21)/`HIT_INSTANCE_ID`(22)/`HIT_GEOMETRY_INDEX`(23) 这三个槽的读出顺序，与 `vx_hit_t` 里字段赋值顺序是否一致（注意头文件注释特意说明 struct 字段顺序 ≠ 窗口槽顺序）。

**预期结果**：你发现 GETW 一次读槽 21..24，但 `vx_rt_wait` 把它们 **逐个** 赋给 `primitive_id/instance_id/geometry_index/instance_custom`，两个顺序是解耦的——这正是注释「do NOT bulk-copy the register window into this struct」的含义。**待本地验证**：若环境装了 RISC-V clang，可编译 `rt_smoke` 并 `objdump` 看 `vx_rt_wtrace`/`vx_rt_wait` 真正展开成的指令序列。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `vx_hit_t` 的浮点（\(t,u,v\)）和整数 ID 要分别用 `GETWF` 与 `GETW` 读，而不是统一一种？
**答案**：浮点落 FP 寄存器堆、整数落 GP 寄存器堆，类型分流让两者各归其位，避免 `fmv` 整数↔浮点转换开销。

**练习 2**：`vx_rt_wtrace` 为什么把 scene 指针放在 `vx_wgather` 的 lane 1，而不是 lane 0（self slot）？
**答案**：self slot 是写抑制的，是 partial-warp wgather 唯一无法从活 lane 物化的字；把 scene 放 lane 1 保证即便 lane 0 被掩蔽（回调/递归收窄）scene 仍然有效。

**练习 3**：槽位 8..21 被图形 fragment 与 RTU 同时占用却不冲突，靠什么保证？
**答案**：靠「按约定互斥」——一个 warp 不同时持有活的 fragment 和 ray-query 状态。这是约定而非硬件强制，也是 FS-fusion ray query 被阻塞的根因。

---

### 4.3 RtuUnit：SFU 前端、宏指令展开与 park/revive

#### 4.3.1 概念说明

`RtuUnit` 是每核一个的 SFU 处理单元（PE），由 `SfuUnit` 持有（参见 u6-l4「SFU 是分派器」）。它干三件事：

1. **持有 RTU 寄存器文件**：per-(warp, lane) 的 32 个命名槽（即 §4.2 的窗口），借用与 TEX/OM 共享的 `GfxWindow`。
2. **把 v2 宏指令展开成微操作**：`TRACE2` 展成 4 个 uop（1 GP 配置 + 3 FP 射线），`WAIT2`/`GETWF` 按 count 展开。
3. **管理 WAIT 的 park/revive 与回调载荷**：因为遍历是异步的，`WAIT2` 到达时结果未必就绪，需要把 trace「挂起」、等 terminal 响应回来再「复活」。

它和集群级的 `RtuCore` 通过 `SimChannel<RtuReq>`/`<RtuRsp>` 通信：trace 时发 `TRACE_NEW` 请求，`RtuCore` 遍历完回 `TERMINAL`（命中/未中）或 `CB_YIELD`（需要回调）。

#### 4.3.2 核心流程

TRACE2 的四拍 uop（见 [rtu_unit.h:L126-L145](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_unit.h#L126-L145) 注释）：

```
uop 0: 读 lane-packed 配置(rs1) → 分配池槽 → 写 handle 到 rd → 暂存 flags/cull/payload/scene
uop 1: origin.xyz ← f0,f1,f2
uop 2: dir.xyz    ← f3,f4,f5
uop 3: tmin,tmax  ← f6,f7 → ARM(构造并发送 RtuReq)
```

WAIT 的 park/revive 是异步模型的核心（[rtu_unit.h:L76-L110](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_unit.h#L76-L110)）：

```
process_wait(trace):
  slot = trace.rs1(handle)
  if slot 已有 pending TERMINAL:        # 快速路径：结果先到
    apply_response(rsp); 写 status; free_slot; 返回 trace
  else:                                  # 慢路径：结果未到
    trace.suspended = true              # 挂起，rd 保持占用→形成排序屏障
    wait_parked_[wid][slot] = {trace}; 返回 nullptr

on_terminal_rsp(rsp):                    # RtuCore 遍历完回调
  if wait_parked_ 有对应项: 复活 trace; apply_response; free_slot; 返回 {trace, block}
  else:                  latch 进 pending_terminals_（WAIT 还没来）
```

关键不变量：**任意时刻，对一个 `(wid, slot)`，`wait_parked_` 与 `pending_terminals_` 恰有一个有表项**。这覆盖了「结果先到」和「wait 先到」两种竞态。

#### 4.3.3 源码精读

**SFU 把 RTU op 派发给 RtuUnit**。`SfuUnit` 在构造时 new 出 `RtuUnit` 并把集群共享的 `RtuCore` 接上：

[sfu_unit.cpp:L65-L78](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sfu_unit.cpp#L65-L78)

```cpp
, rtu_unit_(new RtuUnit(core, rtu_req_out, gfx_window_))
...
rtu_unit_->set_rtu_core(core);
```

派发分支把 `CB_RET`/`TRACE2`/`WAIT2` 分别路由：

[sfu_unit.cpp:L461-L506](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sfu_unit.cpp#L461-L506) 调用 `rtu_unit_->process_cb_ret` / `process_trace2_uop` / `process_wait`。响应侧（[sfu_unit.cpp:L105-L185](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/sfu_unit.cpp#L105-L185)）区分两种 rsp：`TERMINAL` 走 `apply_response` + 复活 parked wait；`CB_YIELD` 走 `apply_callback_payload` 再 `raise_async_trap`（陷入回调着色器）。

**宏指令展开器 RtuUopGen**。uop 计数与生成：

[rtu_unit.cpp:L242-L254](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_unit.cpp#L242-L254)

```cpp
if (*rtu_p == RtuType::TRACE2)  return 4;          // 1 GP config + 3 FP ray
if (*rtu_p == RtuType::GETWF || *rtu_p == RtuType::GETW)
    return args.count ? args.count : 1;            // 一个槽一个 uop
```

`get()`（[rtu_unit.cpp:L256-L321](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_unit.cpp#L256-L321)）为 TRACE2 的 4 个 uop 分别设置源/目的寄存器：uop0 读 `rs1` 写 handle；uop1..3 分别把 `f0..f2`/`f3..f5`/`f6..f7` 设为源。这正对应 §4.2 说的「HW 约定窗口」——编码只命名 `rd/rs1`，窗口靠这里物化。

**TRACE2 uop 处理与 ARM**。`process_trace2_uop` 的 uop 0 解包 lane-packed 配置、uop 3 在 ARM 阶段构造 `RtuReq` 并发送：

[rtu_unit.cpp:L386-L430](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_unit.cpp#L386-L430)

```cpp
// ARM: build + send the RtuReq
req.kind = RtuReqKind::TRACE_NEW;
req.slot_idx = uint32_t(slot);
for (uint32_t t = 0; t < VX_CFG_NUM_THREADS; ++t) {
  ...
  req.origin_x[t] = bits_to_float(lregs[VX_RT_RAY_ORIGIN + 0]);
  // ... 填满 per-lane 射线快照
}
req_out_.send(req);
trace2_slot_.at(wid) = -1;   // 槽已交给 RtuCore
```

注意 per-lane 射线被 **快照** 进 `RtuReq`（一整个 warp 的射线塞进一个包）。`RtuReq` 的结构见 [rtu_types.h:L58-L110](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_types.h#L58-L110)，每种 per-lane 字段都是 `std::array<..., NUM_THREADS>`。

**park/revive**。`process_wait` 的慢路径把 trace 挂起：

[rtu_unit.cpp:L72-L103](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_unit.cpp#L72-L103)，关键一句 `trace->suspended = true` —— 这让记分板保持 WAIT 的 `rd` 占用，从而形成排序屏障，挡住后续 `vx_rt_get_after`（这正是 §4.2.3 的「读 post-TERMINAL 属性」的物理基础）。复活逻辑在 `on_terminal_rsp`（[rtu_unit.cpp:L105-L134](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_unit.cpp#L105-L134)）：`suspended = false`、`apply_response`、`free_slot`。

**回调载荷暂存**。`apply_callback_payload`（[rtu_unit.cpp:L197-L229](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_unit.cpp#L197-L229)）把候选命中属性 + `cb_type` + `cb_handle` 暂存进 yielded lane 的 RTU 槽，供回调 dispatcher 的 `vx_rt_get` 读取、并供 `vx_rt_cb_ret` 把动作路由回原 slot。注意 `VX_RT_CB_HANDLE` 是 **per-lane** 而非 warp 级——因为同 warp 重整可能把来自 **多个 slot** 的 lane 打包进一个 `CB_YIELD` 陷阱。

#### 4.3.4 代码实践

**实践目标**：跟踪一条 trace 从 SFU 到 `RtuCore` 的数据流，理解 park/revive 的两态不变量。

1. 读 [rtu_unit.cpp:L323-L336](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_unit.cpp#L323-L336) `trace2_reserve_slot`：在 **issue 时**（不是 uop0 时）就从集群共享池 `allocate_slot()` 预订槽位。思考：为什么必须在 issue 时预订？（提示：见 rtu_unit.h L161-L166 注释——WAIT2 是释放槽位的唯一途径，若 head uop 进 SFU 时没槽，会卡在队头死锁。）
2. 读 [rtu_unit.cpp:L59-L61](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_unit.cpp#L59-L61) `wait_would_short_circuit` 与 [rtu_unit.cpp:L105-L134](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_unit.cpp#L105-L134) `on_terminal_rsp`，列出「结果先到」与「wait 先到」两条路径分别写哪张表。

**预期结果**：你能讲清为何 `wait_parked_` 与 `pending_terminals_` 对同一 `(wid, slot)` 恰有一个非空，以及 issue 时预订槽位如何避免队头死锁。

#### 4.3.5 小练习与答案

**练习 1**：`process_wait` 慢路径里 `trace->suspended = true` 为什么能挡住后续 `vx_rt_get_after` 读命中属性？
**答案**：suspended 让记分板保持 WAIT 的 `rd`（status 寄存器）占用，而 `vx_rt_get_after` 的读记分板链在 status 上，故必须等 WAIT 真正退休（terminal 已到、`apply_response` 已把命中属性写进窗口）后才能发射。

**练习 2**：为什么槽位必须在 **issue 时** 而不是 uop 0 执行时预订？
**答案**：槽位唯一的释放途径是 WAIT2 完成；若一个无槽的 TRACE2 卡在 SFU 输入队头，它后面的 WAIT2 永远进不来，槽永远释放不了，形成死锁。

---

### 4.4 BVH 遍历：walker、短栈与回调分类

#### 4.4.1 概念说明

`RtuCore` 拿到 `TRACE_NEW` 后，真正的几何遍历交给 **walker**。SimX 提供两个 walker，编译期二选一（不是运行期分支）：

- `FlatWalker`：平铺三角列表（可选一层 TLAS 实例展开），对应 `VX_CFG_RTU_BVH_WIDTH == 0`。
- `Bvh4Walker`：CW-BVH4/6 深度优先遍历，带 TLAS→BLAS 递归，对应 `WIDTH == 4/6`。

两者共享同一个一方法接口 `walk_lane(Slot&, LaneState&, lane, slot_idx)`，返回 true 表示该 lane 排了一个 `CB_YIELD`（需要回调）。walker 只管 **遍历机制**（FSM + 取节点/三角形），不做策略——透明度/剔除/标志位判定走 `rtu_classifier`，纯数学走 `rtu_isect`。

一个关键设计立场：**SimX 是功能 oracle**，遍历栈是无界的，保证绝不漏命中；而 HW 只有 `VX_CFG_RTU_STACK_DEPTH` 深的短栈，溢出时走 **trail restart**（沿路径重新下降找回被驱逐的子树）——访问同样的叶节点，只是多花代价。SimX 用计数器 `bvh_stack_restarts` 把这部分代价算进去，但不丢功能。

#### 4.4.2 核心流程

CW-BVH4 内部节点的遍历（`walk_bvh4_subtree`）伪代码：

```
stack = []
current = root_off
while current 有效 且 未 terminated:
    读节点 kind word
    if kind == LeafTri:   visit_leaf_tri()      # 对每个三角形做 ray_triangle + classify
    elif kind == LeafInst: visit_leaf_inst()    # TLAS: 仿射变换射线, 递归 walk_bvh4_subtree(BLAS)
    elif kind == LeafProc: visit_leaf_proc()    # procedural AABB: ray_aabb → IS 回调
    elif kind == Internal:
        解码节点(量化子 AABB)
        for 每个子节点: reconstruct_child_aabb → ray_aabb_intersect → 收集命中子节点(t_near)
        按 t_near 升序插入排序                 # 就近优先遍历
        把第 1..n-1 个命中子节点压栈; current = 最近子节点
    若无 current: 从 stack 弹出
```

每个 lane 维护一个 `WalkCtx` 累加器（[rtu_walker.cpp:L72-L96](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_walker.cpp#L72-L96)），同时跟踪「最佳不透明命中」（`best_*`）与「最近非不透明候选」（`yield_*`）。遍历结束后 `finalise_lane` 决定该 lane 终结（hit/miss）还是 yield（AHS/IS/CHS/MISS）。

整条遍历由 `RtuCore` 编排（[rtu_core.cpp:L334-L341](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_core.cpp#L334-L341) 的 `tick`）：`drain_requests → issue_memory → compute_intersections → reform.tick → emit_completions`。槽位状态机 `ISSUE → AWAIT(取 cache line) → COMPUTE → (IN_QUEUE 或 RESP) → EMITTED`。

#### 4.4.3 源码精读

**量化子 AABB 重建**。CW-BVH 把子节点 AABB 存成 `origin + qaabb × 2^exp` 的量化形式，遍历时还原：

[rtu_walker.cpp:L54-L62](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_walker.cpp#L54-L62)

```cpp
inline void reconstruct_child_aabb(const float origin[3], const int8_t exp[3],
                                   const uint8_t qmin[3], const uint8_t qmax[3],
                                   float out_mn[3], float out_mx[3]) {
  for (int i = 0; i < 3; ++i) {
    float scale = std::ldexp(1.0f, exp[i]);     // 2^exp
    out_mn[i] = origin[i] + float(qmin[i]) * scale;
    out_mx[i] = origin[i] + float(qmax[i]) * scale;
  }
}
```

**内部节点：box 测试 + 就近排序 + 压栈**。

[rtu_walker.cpp:L293-L351](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_walker.cpp#L293-L351) 解码节点后，对每个子节点做 `ray_aabb_intersect`，命中的收集进 `hits[]`，按 `t_near` 插入排序，最近子节点设为 `current`，其余压栈。压栈时若超出 HW 短栈深度，计数 `bvh_stack_restarts` 但仍保留（无界栈，不漏命中）：

```cpp
if (stack.size() >= VX_CFG_RTU_STACK_DEPTH)
    ++perf.bvh_stack_restarts;        // 算代价，但不丢子树
stack.push_back(hits[i].offset);
```

**叶节点：三角形 + 分类**。`visit_leaf_tri`（[rtu_walker.cpp:L113-L178](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_walker.cpp#L113-L178)）对每个三角形调 `ray_triangle`，再调 `classify_tri_hit` 决定 `Commit`/`Yield`/`Ignore`。不透明命中更新 `best_*`，非不透明候选更新 `yield_*`；`terminate_on_first_hit` 直接终止全树。

分类策略集中在 `rtu_classifier`（[rtu_classifier.h:L33-L77](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_classifier.h#L33-L77)）：`TriAction {Ignore, Commit, Yield}` 与 `LaneAction {TerminalHit, TerminalMiss, YieldAhs, YieldIs, YieldChs, YieldMiss}`。这是 ray-flag + 透明度策略的单一更新点。

**TLAS→BLAS 实例下降**。`visit_leaf_inst`（[rtu_walker.cpp:L223-L250](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_walker.cpp#L223-L250)）先做 `instanceCullMask` 门控，再用 `affine_inverse_transform_ray` 把世界射线转成对象空间射线，递归 `walk_bvh4_subtree` 遍历该实例的 BLAS。注意 `ctx` 跨整个调用树共享，所以 BLAS 命中能更新同一个 `best_t` 来剪枝后续 TLAS 侧的 AABB 测试。

**编排：compute_intersections 与周期代价**。`RtuCore::Impl::compute_intersections`（[rtu_core.cpp:L476-L543](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_core.cpp#L476-L543)）是 SELECT/EXEC 两相的 SimX 实现：walker 在一个 tick 内功能跑完（正确性立即得出），然后 **读 perf 计数器增量**（`bvh_box_tests`/`bvh_tri_tests`/`bvh_instance_descents`/`bvh_stack_restarts` 的 delta），折算成周期：

```cpp
uint32_t cycles = BoxPe::cycles_for(box_delta)
                + TriPe::cycles_for(tri_delta)
                + kRtuXformLatency * inst_delta
                + VX_CFG_RTU_STACK_DEPTH * restart_delta;
```

然后每 tick 只 drain 一个 cycle（[rtu_core.cpp:L550-L554](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_core.cpp#L550-L554)），归零才推进槽位。这正是 SimX 用功能结果反推时序、担当 RTL 预言机的典型手法。

**回调重整 ReformationEngine**。当 walker 为某些 lane 排了 `CB_YIELD`，`compute_intersections` 在周期 drain 完后把它们 push 进 `ReformationEngine` 的队列（[rtu_core.cpp:L564-L577](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_core.cpp#L564-L577)），`reform_.tick()` 再把同 warp 的多个候选打包成一个 `CB_YIELD` 响应发给 `RtuUnit`，触发异步陷阱。`drain_requests`（[rtu_core.cpp:L347-L423](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_core.cpp#L347-L423)）处理回调返回的 `CB_ACTION`：ACCEPT 提交候选（procedural 用 IS 算的 `cb_hit_t`），IGNORE 不改命中，全部 lane 处理完才推进到 RESP。

#### 4.4.4 代码实践

**实践目标**：跟踪一条射线在 CW-BVH4 里的遍历，理解就近优先与短栈代价。

1. 读 [rtu_walker.cpp:L268-L360](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_walker.cpp#L268-L360) 主循环。
2. 假设一个内部节点有 3 个子节点 A/B/C，射线和它们的 `t_near` 分别是 0.5/miss/0.2。手算：哪个子节点成为 `current`？哪些压栈？压栈顺序是什么？
3. 设置环境变量 `VX_RTU_STATS=1` 跑一个 BVH 测试（见 [rtu_core.cpp:L296-L324](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_core.cpp#L296-L324) 的 stats dump），观察 `bvh_stack_restarts` 是否非零。

**预期结果**：第 2 步答案是 C(0.2) 成为 current，A(0.5) 压栈，B 被 miss 剪枝；压栈顺序保证出栈时先访问 A。`bvh_stack_restarts > 0` 说明该射线深度超过了 HW 短栈。**待本地验证**：实际 stats 数值依赖场景深度与 `VX_CFG_RTU_STACK_DEPTH`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 SimX 用无界栈，却还要数 `bvh_stack_restarts`？
**答案**：SimX 作为功能 oracle 必须不漏命中，故无界栈；但 RTL 只有固定深度短栈，溢出要 trail restart 重下降，这部分额外周期必须计入代价模型才能保持 cycle parity，所以用计数器记录而不丢功能。

**练习 2**：`WalkCtx` 为何要同时维护 `best_*`（不透明）和 `yield_*`（非不透明）两套？
**答案**：因为一个更近的非不透明候选可能被 AHS 拒绝（IGNORE），此时较远的不透明命中仍应是结果；分开维护两套最近候选，遍历顺序无关，且能在结束时由 `finalise_lane` 正确决定 commit 还是 yield。

**练习 3**：procedural AABB 叶节点为什么「天然非不opaque」、总是 yield IS？
**答案**：procedural 原语的真实命中由 intersection shader 计算，RTU 只能用 AABB 入口 \(t\) 作下界；是否真命中、命中多远，都由 IS 决定，所以总是 stage 一个 IS yield，由 IS 通过 `VX_RT_HIT_T` 回传真实 \(t\)，ACCEPT 时提交该 \(t\)。

---

### 4.5 相交测试数学与 PE 时序模型

#### 4.5.1 概念说明

`rtu_isect.cpp` 是 RTU 的「数学内核」：三个纯函数 + 两个周期代价类。今天它们是标量内联函数，被 walker 逐三角形/AABB/实例调用；RTL 里它们对应 box-PE / tri-PE / XFORM 单元里的组合逻辑。设计文档（§8.7）把它们定位为未来流水化 `BoxPe`/`TriPe` 协处理器的雏形。

#### 4.5.2 核心流程

**ray-AABB（slab 方法）**。对射线的每个轴，算进/出该对平行面（slab）的参数，取所有轴进入值的最大值 `tn` 与退出值的最小值 `tf`；若 \(t_n \leq t_f\) 且区间与 \([t_{min}, t_{max}]\) 相交则命中，入口参数 \(t_{near} = t_n\)。

**ray-triangle（Möller–Trumbore）**。用三角形两条边 \(e_1=V_1-V_0\)、\(e_2=V_2-V_0\) 与射线方向构造行列式，一次解出 \(t\) 与重心坐标 \( (u,v) \)。

**仿射逆变换**。TLAS 实例下降时把世界射线转到对象空间：\(o' = R^{-1}(o - t)\)，\(d' = R^{-1}d\)。纯旋转+平移下 \(t\) 参数跨空间不变，故 BLAS 报告的 \(t\) 就是世界 \(t\)。

**PE 时序模型**。RTL 每个 `RtuCore` 只有 **一个** box PE 和 **一个** tri PE，跨所有 `NUM_CTX` 上下文每周期流式处理一个原语（不是旧模型假设的 W 宽并行阵列）。代价 = \(n\) 个 issue 周期（1/周期）+ 一次流水线排空，排空深度由 FMA/FDIV 延迟符号化表达以跟踪配置。

#### 4.5.3 源码精读

**ray-AABB slab 测试**：

[rtu_isect.cpp:L52-L67](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_isect.cpp#L52-L67)

```cpp
bool ray_aabb_intersect(const float ro[3], const float rd[3],
                        const float mn[3], const float mx[3],
                        float tmin, float tmax, float& t_near) {
  float tn = tmin, tf = tmax;
  for (int i = 0; i < 3; ++i) {
    float inv = 1.0f / rd[i];
    float t0 = (mn[i] - ro[i]) * inv;
    float t1 = (mx[i] - ro[i]) * inv;
    if (t0 > t1) { float tmp = t0; t0 = t1; t1 = tmp; }
    if (t0 > tn) tn = t0;
    if (t1 < tf) tf = t1;
    if (tn > tf) return false;
  }
  t_near = tn;
  return true;
}
```

每轴的进出参数：

\[ t_0^{(i)} = (mn_i - o_i)/d_i, \quad t_1^{(i)} = (mx_i - o_i)/d_i \]

取 \(t_n = \max_i \min(t_0^{(i)}, t_1^{(i)})\)、\(t_f = \min_i \max(t_0^{(i)}, t_1^{(i)})\)，命中当且仅当 \(t_n \leq t_f\)。代码里 `tn/tf` 初值用 `[tmin,tmax]` 而非 ±∞，等价地把射线有效区间一起裁进去。

**ray-triangle Möller–Trumbore**：

[rtu_isect.cpp:L19-L50](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_isect.cpp#L19-L50) 的数学：

\[ P = d \times e_2, \quad \det = e_1 \cdot P \]

若 \(|\det| < \varepsilon\) 则射线平行于三角形，返回未命中（[rtu_isect.cpp:L34-L35](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_isect.cpp#L34-L35)）。令 \(\mathrm{invDet} = 1/\det\)，\(T = o - V_0\)，则：

\[ u = (T \cdot P)\,\mathrm{invDet}, \quad Q = T \times e_1, \quad v = (d \cdot Q)\,\mathrm{invDet}, \quad t = (e_2 \cdot Q)\,\mathrm{invDet} \]

边界检查：\(u \in [0,1]\)、\(v \geq 0\)、\(u+v \leq 1\)、\(t \in [t_{min}, t_{max}]\)。代码还输出 `back_facing = (det < 0)`（[rtu_isect.cpp:L48](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_isect.cpp#L48)），供 ray-flag 面剔除使用（三角形正面 = 从 \((V_0,V_1,V_2)\) 看去逆时针的一侧）。

**仿射逆变换**：

[rtu_isect.cpp:L69-L108](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_isect.cpp#L69-L108) 按余子式展开算 \(\det(R)\)，奇异时退化为单位变换（[rtu_isect.cpp:L80-L84](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_isect.cpp#L80-L84)），否则 \(R^{-1} = \mathrm{adj}(R)/\det\)，得到：

\[ o_{\text{obj}} = R^{-1}(o_{\text{world}} - t), \quad d_{\text{obj}} = R^{-1} d_{\text{world}} \]

RTL 对应 `VX_rtu_xform`，**只用 FMA**（设计文档明确：复用 `VX_fma_unit`，无新数据通路），SimX 这里的标量实现是功能等价物。

**PE 周期代价模型**。

[rtu_isect.cpp:L116-L128](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_isect.cpp#L116-L128)

```cpp
uint32_t BoxPe::cycles_for(uint32_t n_tests) {
  if (n_tests == 0) return 0;
  constexpr uint32_t kDepth = 3 * kRtuLatencyFma + 1 + 2 + 1;  // 31
  return n_tests + kDepth - 1;
}
uint32_t TriPe::cycles_for(uint32_t n_tests) {
  if (n_tests == 0) return 0;
  constexpr uint32_t kDepth = 8 * kRtuLatencyFma + kRtuFdivLat + 2;  // 91
  return n_tests + kDepth - 1;
}
```

即「issues + LATENCY − 1」：最后一个 issue 进入流水后还要排空 LATENCY 拍。box-PE 深度 31（3 级 FMA slab min/max + 排空），tri-PE 深度 91（8 级 FMA + 一次 FDIV 倒数 + 排空）。注释强调 RTL 是「一个 PE、每周期一个原语」，而非 W 宽并行——这是相对旧模型的重要修正。`rtu_isect.h`（[rtu_isect.h:L83-L117](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_isect.h#L83-L117)）详述了这个成本模型。

#### 4.5.4 代码实践

**实践目标**：验证时序模型与 RTL 流水深度对齐。

1. 读 [rtu_isect.cpp:L116-L128](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_isect.cpp#L116-L128) 的两个 `cycles_for`。
2. 设 `kRtuLatencyFma=9`、`kRtuFdivLat=17`（具体值见 `constants.h`，**待确认**），算 box-PE 与 tri-PE 的 `kDepth`，确认分别 ≈ 31 / 91。
3. 跟踪一次遍历：若某 lane 做了 4 次 box 测试 + 2 次 tri 测试 + 1 次实例下降，用 [rtu_core.cpp:L521-L530](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_core.cpp#L521-L530) 的公式算出 `cycles`。

**预期结果**：cycles = `BoxPe(4) + TriPe(2) + kRtuXformLatency*1` = (4+30) + (2+90) + xform ≈ 126 + xform。你能说清这个数怎么进 `compute_cycles_remaining` 再被逐 tick drain（§4.4.3）。

#### 4.5.5 小练习与答案

**练习 1**：Möller–Trumbore 里 \(|\det| < \varepsilon\) 为什么返回未命中？
**答案**：\(\det = e_1 \cdot (d \times e_2)\) 与射线方向和三角形平面法向的点积成正比；\(|\det|\) 过小意味着射线几乎平行于三角形平面，无交点（或数值不稳定），故判未命中。

**练习 2**：纯旋转+平移的实例变换为何「BLAS 报告的 \(t\) 就是世界 \(t\)」？
**答案**：旋转保持向量长度，平移不改变方向参数；射线参数 \(t\) 沿方向度量，旋转不改方向长度，故 \(t\) 在世界/对象空间一致。非均匀缩放才需要重归一化 \(t\)（本实现不支持）。

**练习 3**：PE 代价为何是 `n_tests + depth - 1` 而不是 `n_tests * depth`？
**答案**：因为 PE 是 **流水线** 而非迭代式——每周期可接受一个新原语（issue），n 个原语 n 周期喂完，最后一个原语还需 depth-1 拍排空，故总周期为 n + depth − 1。

---

## 5. 综合实践

本讲的综合实践把四个最小模块串起来：从一条射线的发射，到命中一个三角形，画出 RTU 的完整处理流程。

**任务**：阅读 [docs/designs/ray_tracing_unit.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/ray_tracing_unit.md)，然后对照 SimX 源码，画出一条不透明射线从发射到命中的端到端流程图，要求：

1. **标注 warp 侧** 的每一步（`vx_rt_wtrace` 的 4 个 uop → handle、`vx_rt_wait` 的 park → terminal → 复活 → 读命中属性），引用 [rtu_unit.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_unit.cpp) 的具体函数。
2. **标注 RtuCore 编排** 的槽位状态变迁 `ISSUE→AWAIT→COMPUTE→RESP→EMITTED`，引用 [rtu_core.cpp:L476-L584](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_core.cpp#L476-L584)。
3. **标注 walker 内部** 的 BVH 下降（box-PE 测试 + 就近排序 + 短栈）与叶节点的 tri-PE 测试，引用 [rtu_walker.cpp:L268-L360](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_walker.cpp#L268-L360) 与 [rtu_isect.cpp:L19-L67](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_isect.cpp#L19-L67)。
4. **在图上标出三个 PE**（box_pe / tri_pe / xform）与两段通道（`RtuReq` 请求、`RtuRsp` TERMINAL 响应）。

**可选运行验证**：在 SimX 上跑最小测试 `rt_smoke`（不透明三角形，命中路径）：

```
./ci/blackbox.sh --driver=simx --app=rt_smoke   # 待本地验证具体 app 名与旋钮
```

设置 `VX_RTU_STATS=1` 观察 stats（[rtu_core.cpp:L296-L324](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sim/simx/rtu/rtu_core.cpp#L296-L324)），把 `rays_issued/rays_hit/bvh_box_tests/bvh_tri_tests/walker_cycles_total` 填进你的图里，验证它们与你画的流程一致。若想观察回调路径，改跑 `rt_smoke_ahs`（非不透明三角形 → AHS yield）或 `rt_smoke_proc`（procedural AABB → IS yield），对比 stats 里 `cb_ahs`/`cb_is` 计数的变化。

> 注意：设计文档 §7 列出 `rt_raycast`/`bvh_multinode` 等用例在重负载下会 wedge 记分板（sustained multi-warp servicing 是 RTL-deferred），综合实践请用 `rt_smoke*` 这类已验证的用例。

**自检问题**（答得出说明你吃透了本讲）：

- 一条不透明命中射线经过了哪几个 PE？答：box_pe（剪枝）+ tri_pe（叶节点命中），无 xform（非实例化）、无回调。
- 若三角形非不透明，流程在哪一步分叉？答：tri 命中后 `classify_tri_hit` 返回 `Yield` 而非 `Commit`，walker stage 一个 `yield_*`，`finalise_lane` 返回 `YieldAhs`，`ReformationEngine` 发 `CB_YIELD`，warp 陷入 AHS 陷阱，着色器用 `vx_rt_cb_ret(ACCEPT/IGNORE)` 释放。

## 6. 本讲小结

- **RTU 是异步、SIMT 派发的 BVH 遍历加速器**：warp 用 `TRACE2` 提交射线立即拿 handle，RTU 离线遍历，结果经记分板化寄存器窗口送回；与图形 FF 共享 `custom1` opcode 与图形寄存器窗口，由 `VX_CFG_EXT_RTU_ENABLE` 门控。
- **v2 窗口 ABI 把 marshalling 折叠成两条指令**：`vx_rt_wtrace`（射线钉在 `f0..f7`、配置 lane-pack 进 rs1）+ `vx_rt_wait`（拆成 block + 写回两个 op，靠记分板链保证 post-terminal 才读命中属性）；窗口 32 槽的编号是软硬件共享的 ABI 契约（`VX_types.toml`）。
- **RtuUnit 是每核 SFU 前端**：展开 `TRACE2`→4 uop、`WAIT2`/`GETWF`→按 count；用 `wait_parked_`/`pending_terminals_` 两态不变量管理异步 WAIT 的 park/revive；槽位在 issue 时预订以避免队头死锁。
- **walker 是功能 oracle**：`FlatWalker`/`Bvh4Walker` 编译期二选一，DFS 就近优先遍历，无界栈不漏命中、同时数 `bvh_stack_restarts` 给短栈算代价；TLAS→BLAS 经仿射逆变换递归；透明度/剔除策略集中在 `rtu_classifier`。
- **相交数学是经典三件套**：slab（ray-AABB）、Möller–Trumbore（ray-triangle，输出 \(t,u,v\) 与 back_facing）、仿射逆变换（世界→对象，纯旋转+平移保 \(t\)）；`BoxPe`/`TriPe` 用「issues + LATENCY − 1」把测试数折算成 RTL 单 PE 流水周期。
- **SimX↔RTL 逐模块对应**：`rtu_unit↔SFU+窗口`、`rtu_core↔VX_rtu_scheduler`、`walker↔box/tri PE`、`rtu_isect↔PE 数据通路`，共同维持 model parity（u7-l4）这条主线。

## 7. 下一步学习建议

- **RTL 对照**：打开 [`hw/rtl/rtu/VX_rtu_scheduler.sv`](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/rtu/VX_rtu_scheduler.sv)、`VX_rtu_box_pe.sv`、`VX_rtu_tri_pe.sv`、`VX_rtu_xform.sv`，把本讲的 SimX 模块逐一对应到 RTL，体会「语义与时序同居一处」如何成为 RTL 的预言机（承接 u7-l4 model parity）。
- **图形软件栈衔接**：RTU 的 Vulkan ray-query 路径（NIR `rayQueryEXT` → RTU op lowering、AS transcode、residency）在 `vortexpipe_architecture.md` §6.3，可结合 u10-l2（图形软件栈）阅读；注意当前 AS 每次 dispatch 都重建，residency 是 tracked gap。
- **回调与重整的边界**：设计文档 §7 列出的 RTL-deferred 项（in-trap 递归、multi-warp/SBT-divergent reformation、sustained multi-warp servicing）是理解「为什么 `rt_raycast` 在重负载下会 wedge」的钥匙，建议作为进阶阅读。
- **下一讲**：u11 系列进入虚拟内存、原子操作与命令处理器；RTU 的 AXI master 绕过 MMU、使用 BVH 的物理地址（§6），正好作为虚拟内存子系统（u11-l1）的一个具体用例承接。
