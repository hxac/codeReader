# 主流程编排 vpr_api

## 1. 本讲目标

前几讲我们认识了 VPR 的各类数据结构：原子网表 `AtomNetlist`（u3-l2）、聚簇网表 `ClusteredNetlist`（u3-l3），以及把它们挂在一起的全局状态容器 `VprContext` / `g_vpr_ctx`（u3-l4）。但这些零件是「怎么被一条主线串起来、按什么顺序跑完整个 CAD 流程」的？本讲就来回答这个问题。

本讲的中心是 `vpr/src/base/vpr_api.h` 与 `vpr_api.cpp` 这一对文件。它们是 VPR 对外暴露的「总开关面板」，把初始化、打包、布局、布线、分析、释放编排成一条完整生命周期，是连接所有阶段的**主动脉**。`main.cpp` 本身只是一个极薄的壳，真正的流程逻辑全部落在 `vpr_api` 里。

学完本讲你应该能够：

- 说清 `main → vpr_init → vpr_flow → vpr_free_all` 这条顶层调用链各自的职责。
- 读懂 `vpr_flow` 如何把打包 / 布局 / 布线 / 分析四个阶段按顺序分派出去，以及每个阶段的「DO / LOAD / SKIP」语义从何而来。
- 解释阶段之间是如何借助 `g_vpr_ctx` 和 `t_vpr_setup` 这「一状态、一配置」传递数据的，并能在源码里指出每个阶段读写了哪个子上下文。

## 2. 前置知识

本讲假设你已经读过：

- **u1-l5 VPR 命令行与参数体系**：知道 `t_options` 是所有命令行选项的聚合结构体，知道布尔开关 `--pack/--place/--route` 会被翻译成四态枚举 `e_stage_action`（DO / LOAD / SKIP / SKIP_IF_PRIOR_FAIL）。本讲会大量用到这个枚举。
- **u3-l2 / u3-l3 网表家族**：知道 `AtomNetlist` 是技术映射后的原子级电路，`ClusteredNetlist` 是打包后的逻辑块级网表。
- **u3-l4 VprContext 与全局状态管理**：知道 `g_vpr_ctx` 是全局状态的「单一真相来源」，分若干子上下文（Atom / Device / Clustering / Placement / Routing / Timing 等），访问遵循 `mutable_xxx()` 写、`xxx()` 读的双 getter 模式。

两个本讲会用到的关键词：

- **阶段（stage）**：VPR 把一次运行切成打包、布局、布线、分析等阶段，每个阶段对应一个 `*_flow` 函数。
- **配置对象 `t_vpr_setup`**：一个聚合结构体，把架构、电路、各阶段选项打包在一起，贯穿整个流程。它和 `g_vpr_ctx` 的分工是：`t_vpr_setup` 装「配置 / 输入」，`g_vpr_ctx` 装「运行时状态 / 结果」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`vpr/src/main.cpp`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/main.cpp) | 程序入口，瘦壳：声明三个栈对象，调用 `vpr_init → vpr_flow → vpr_free_all`，并用三层 `catch` 兜底异常。 |
| [`vpr/src/base/vpr_api.h`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.h) | VPR 对外 API 的声明。文件头注释给出了「外部工具应当只调用这里的函数」的约定与推荐调用顺序。 |
| [`vpr/src/base/vpr_api.cpp`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp) | 编排逻辑的实现，本讲主角：`vpr_init`、`vpr_flow`、各 `*_flow`、`vpr_free_all` 全在这里。 |
| [`vpr/src/base/vpr_types.h`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h) | 定义配置对象 `t_vpr_setup`、阶段动作枚举 `e_stage_action`、以及 `RouteStatus`。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：①初始化与选项加载；②`vpr_flow` 主流程分派；③阶段间数据传递。

### 4.1 初始化与选项加载

#### 4.1.1 概念说明

VPR 的一次运行可以粗略看成三个大动作：**初始化 → 跑流程 → 释放**。初始化阶段要完成两件事：

1. 把命令行参数和架构 / 电路文件读进来，整理成一份「配置」——也就是 `t_vpr_setup`。
2. 顺便把那些「不依赖后续阶段、一开始就能确定」的全局状态建好，主要是 **AtomContext**（原子网表）和 **TimingContext**（时序图 + 约束）。

理解初始化的关键是分清两个对象的生命周期：

- `t_vpr_setup`：在 `main` 里**栈上**声明，装着用户选项与输入文件名，整条流程都带着它走。
- `t_arch`：同样栈上声明，是 libarchfpga 解析架构 XML 得到的顶层架构对象（回顾 u2-l1 / u2-l2），作为 `const` 引用传给几乎所有阶段。
- `g_vpr_ctx`：全局的、按主题切分的状态容器（u3-l4），初始化阶段往里写 Atom / Timing，后续阶段继续往里填 Device / Clustering / Placement / Routing。

#### 4.1.2 核心流程

`main` 的顶层结构非常干净——三个栈对象 + 三步调用 + 三层异常兜底：

```
main():
    声明 Options(t_options), Arch(t_arch), vpr_setup(t_vpr_setup)
    try:
        vpr_install_signal_handler()
        vpr_init(argc, argv, &Options, &vpr_setup, &Arch)   # 读选项 + 读架构 + 读电路
        若仅查询版本/资源 → 释放后退出
        flow_succeeded = vpr_flow(vpr_setup, Arch)            # 跑完整流程
        打印时序统计
        vpr_free_all(Arch, vpr_setup)                         # 释放
    catch tatum::Error   / VprError / vtr::VtrError → 打印 + 释放 + 返回错误码
```

`vpr_init` 自身又分两步：先用 `read_options` 解析命令行，再调用 `vpr_init_with_options` 做真正的「读架构 + 读电路 + 建时序图」。把命令行解析和后续初始化拆开，是为了允许「先用别的方式拿到 `t_options`、再直接调 `vpr_init_with_options`」这种二次集成方式。

#### 4.1.3 源码精读

**main 的瘦壳结构**——栈上声明三大对象，三步调用，三层 catch：

[`vpr/src/main.cpp:44-72`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/main.cpp#L44-L72) 中，`main` 先声明 `t_options Options`、`t_arch Arch`、`t_vpr_setup vpr_setup`，随后 `vpr_init(...)`（L55）→ `vpr_flow(...)`（L67）→ `vpr_free_all(...)`（L78）。注意 `vpr_flow` 返回 `bool`，失败时 `main` 走 `UNIMPLEMENTABLE_EXIT_CODE`（L71）——这是「电路无法实现」的退出码，区别于程序出错。三层 `catch`（L82 / L88 / L97）分别处理 Tatum 时序库错误、VPR 自定义错误、libvtrutil 通用错误，**每条分支都保证调用 `vpr_free_all`**，避免异常路径下内存泄漏。

**vpr_init 的两段式**：

[`vpr/src/base/vpr_api.cpp:185-200`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L185-L200) 里，`vpr_init` 先 `vpr_initialize_logging()`、`vpr_print_title()`，再用 `*options = read_options(argc, argv)`（L192）把命令行解析进 `t_options`，打印参数后转交给 `vpr_init_with_options`。

**vpr_init_with_options 是初始化的真正主体**，内部按顺序做几件事：

[`vpr/src/base/vpr_api.cpp:209-378`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L209-L378)

- **确定并行 worker 数**（L215-229）：优先级是「命令行显式指定 > 环境变量 `VPR_NUM_WORKERS` > 默认值」，用 `options->num_workers.provenance()` 判断用户是否显式给出（回顾 u1-l5 的 Provenance 机制）。
- **把顶层选项搬进 `vpr_setup`**（L247-254）：`TimingEnabled`、`device_layout`、`device_width`、`num_workers` 等字段在这里从 `t_options` 拷进 `t_vpr_setup`。
- **`SetupVPR`（即 `vpr_setup_vpr`）读取架构与电路并填充所有 `t_xxx_opts` 子结构**（L294-318）：这是初始化里最重的一步，它把架构 XML 解析进 `t_arch`，并把文件名、打包 / 布局 / 布线 / 分析等选项分别填进 `vpr_setup` 的各子结构。
- **合法性校验**（L321-324）：`CheckArch` 检查架构合理性，`check_setup` 检查选项之间是否冲突。
- **读电路、建原子网表**（L330-331）：

  ```cpp
  auto& atom_ctx = g_vpr_ctx.mutable_atom();
  atom_ctx.mutable_netlist() = read_and_process_circuit(...);
  ```

  这里用了 `mutable_atom()`（u3-l4 的「生产者取 mutable」约定），把 BLIF 读成的 `AtomNetlist` 写进 `AtomContext`。这正是 u3-l2 讲过的「从 BLIF 到 AtomNetlist」入口。
- **建时序图与约束**（L341-360）：若 `TimingEnabled`，用 `TimingGraphBuilder` 构造时序图写进 `timing_ctx.graph`，再用 `read_sdc` 读约束写进 `timing_ctx.constraints`。注意时序图在**初始化阶段**就建好了，因为它的依据是原子网表，而原子网表此刻已经就绪。

**配置对象 t_vpr_setup 的全貌**：

[`vpr/src/base/vpr_types.h:1647-1682`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h#L1647-L1682) 定义了 `t_vpr_setup`，它就是把各阶段选项「打包带走」的容器：

```cpp
struct t_vpr_setup {
    bool TimingEnabled;
    t_file_name_opts  FileNameOpts;   // 各阶段输入/输出文件名
    t_packer_opts     PackerOpts;     // 打包选项（含 doPacking）
    t_placer_opts     PlacerOpts;     // 布局选项（含 do_placement）
    t_ap_opts         APOpts;         // 分析式布局选项（含 doAP）
    t_router_opts     RouterOpts;     // 布线选项（含 doRouting、flat_routing）
    t_analysis_opts   AnalysisOpts;   // 分析选项（含 doAnalysis）
    t_det_routing_arch RoutingArch;   // 布线架构参数
    std::vector<t_segment_inf> Segments;
    t_timing_inf      Timing;
    unsigned int      num_workers;
    // ... 其余字段省略
};
```

可以看到，`t_vpr_setup` 几乎为每个阶段都留了一个 `t_xxx_opts` 子结构，而每个子结构里都有一个 `doXxx`（类型为 `e_stage_action`）字段——这正是下一节分派的依据。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「初始化阶段建好了哪些全局状态」。

**操作步骤**：

1. 打开 `vpr/src/base/vpr_api.cpp`，定位 `vpr_init_with_options`（L209）。
2. 在其中搜索所有 `g_vpr_ctx.mutable_` 调用，记录它们分别写进了哪个子上下文。
3. 对比 `vpr_flow`（下一节）里出现的 `g_vpr_ctx.mutable_` 调用，看哪些上下文是初始化写的、哪些是后续阶段写的。

**需要观察的现象**：初始化阶段只出现 `mutable_atom()`、`mutable_timing()`、`mutable_power()`、`mutable_device()`（仅写了一个 `pad_loc_type`）等少数写入；而 `mutable_clustering()`、`mutable_placement()`、`mutable_routing()` 在初始化里**不出现**，它们要等打包 / 布局 / 布线阶段才被填充。

**预期结果**：你会得到一张「子上下文 → 谁负责填充」的对照表，这正是理解阶段分工的钥匙。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `main` 里要把 `t_options`、`t_arch`、`t_vpr_setup` 都声明在栈上，而不是 `new` 在堆上？

**参考答案**：栈对象在 `main` 返回时自动析构，配合每个 `catch` 分支里 `vpr_free_all`，能保证无论正常退出还是异常退出，资源都被释放；且这三个对象的生命周期恰好等于整个进程，没有需要提前销毁的场景，栈分配最简单安全。

**练习 2**：`vpr_init_with_options` 里读电路用的是 `g_vpr_ctx.mutable_atom()`，而读时序约束用的是 `g_vpr_ctx.mutable_timing()`。为什么这里必须用 `mutable_` 版本？

**参考答案**：因为初始化阶段是这些上下文的**生产者**，要往里写数据（u3-l4 的约定：「生产者取 mutable，消费者取 const」）。`xxx()` 返回的是 `const T&` 只读引用，无法赋值。

---

### 4.2 vpr_flow 主流程分派

#### 4.2.1 概念说明

`vpr_flow` 是整条主动脉。它把四个阶段按固定顺序串起来：**打包 → （可选的分析式布局）→ 建器件 → 布局 → 布线 → 分析**。每个阶段都封装成一个 `*_flow` 函数（`vpr_pack_flow` / `vpr_place_flow` / `vpr_route_flow` / `vpr_analysis_flow`），`vpr_flow` 只负责「按顺序调用 + 检查成功与否」。

每个 `*_flow` 内部都遵循同一种**阶段动作分派**模式：读取对应 `doXxx` 字段（类型 `e_stage_action`），按下表决定行为。这个枚举你在 u1-l5 已经见过，本讲看它如何驱动真实流程：

| `doXxx` 取值 | `*_flow` 的行为 | 典型调用 |
| --- | --- | --- |
| `SKIP` | 直接跳过该阶段（路由 / 分析假定成功或不运行） | 无 |
| `DO` | 真正运行该阶段 | `vpr_pack` / `vpr_place` / `vpr_route_min_W` |
| `LOAD` | 从已有结果文件加载该阶段产物 | `vpr_load_packing` / `vpr_load_placement` / `vpr_load_routing` |
| `SKIP_IF_PRIOR_FAIL` | 仅分析阶段使用：前置阶段成功才运行 | （见 `vpr_analysis_flow`） |

`e_stage_action` 的定义见 [`vpr/src/base/vpr_types.h:712-718`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h#L712-L718)：

```cpp
enum class e_stage_action {
    SKIP = 0,
    LOAD,
    DO,
    SKIP_IF_PRIOR_FAIL,
    NUM_STAGE_ACTIONS
};
```

#### 4.2.2 核心流程

`vpr_flow` 的主流程可以抽象成下面的伪代码（保留了关键调用与成功检查）：

```
vpr_flow(vpr_setup, arch):
    if vpr_setup.exit_before_pack: 警告后 return true     # 仅收集统计就退出

    设置 TBB 并行度 (num_workers)

    # 1) 打包
    if not vpr_pack_flow(vpr_setup, arch): return false    # 不可实现 → 中止

    # 2) （可选）分析式布局 AP —— 一条替代路径，自带建器件
    if APOpts.doAP == DO:
        run_analytical_placement_flow(vpr_setup)
        vpr_setup_clock_networks(...)

    # 3) 建器件（非 AP 路径才在此建：网格 + 时钟网 + NoC + RR 图）
    if APOpts.doAP != DO:
        vpr_create_device(vpr_setup, arch, is_pack_only(...))

    打印资源用量 / 器件利用率
    vpr_init_graphics(...); vpr_init_server(...)

    # 4) 布局：输入是聚簇网表 clb_nlist
    placement_net_list = g_vpr_ctx.clustering().clb_nlist
    if not vpr_place_flow(placement_net_list, vpr_setup, arch): return false

    # 5) 布线：根据 is_flat 选输入网表
    router_net_list = is_flat ? atom_netlist : clb_nlist
    route_status = vpr_route_flow(router_net_list, vpr_setup, arch, is_flat)

    # 6) 分析
    vpr_analysis_flow(router_net_list, vpr_setup, arch, route_status, is_flat)

    vpr_close_graphics(...)
    return route_status.success()
```

几个要点：

- **成功传播**：打包、布局返回 `bool`，失败（`false` 表示「电路不可实现」）会立刻 `return false` 中断流程；`main` 据此返回 `UNIMPLEMENTABLE_EXIT_CODE`。布线返回 `RouteStatus`（带是否成功 + 通道宽度），分析总是跑（哪怕布线失败也会对「非法实现」做分析并打印警告）。
- **AP 是一条分叉路**：当 `APOpts.doAP == DO` 时走分析式布局（u8-l1），它**自己内部建器件**；因此 `vpr_create_device` 只在非 AP 路径调用。源码里多处 `TODO` 注释也承认这种「流程分叉」需要清理。
- **`is_pack_only` 短路**：若用户只要求打包、跳过后续所有阶段，建器件时就不再构建布线 RR 图，省下大量开销。

#### 4.2.3 源码精读

**vpr_flow 的打包 + AP + 建器件段**：

[`vpr/src/base/vpr_api.cpp:437-520`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L437-L520) 里：先 `exit_before_pack` 短路（L438-441）；设置 TBB 并行度（L446）；调用 `vpr_pack_flow` 并检查返回（L450-454）；AP 分支（L457-496）与「非 AP 才建器件」分支（L500-503）互斥；最后打印资源用量、初始化图形与 server。

**vpr_flow 的布局 + 布线 + 分析段**：

[`vpr/src/base/vpr_api.cpp:535-561`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L535-L561) 里，布局的输入被显式取成聚簇网表：

```cpp
const auto& placement_net_list = (const Netlist<>&)g_vpr_ctx.clustering().clb_nlist;
bool place_success = vpr_place_flow(placement_net_list, vpr_setup, arch);
```

随后根据 `is_flat` 选择布线输入网表（L544），调用 `vpr_route_flow`（L551）得到 `route_status`，再交给 `vpr_analysis_flow`（L554），最后 `return route_status.success()`（L560）。

**阶段动作分派的三个范例**：

打包的分派 [`vpr/src/base/vpr_api.cpp:679-718`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L679-L718)：`SKIP` 直接 pass；`DO` 先 `vpr_pack`（真正打包）再 `vpr_load_packing`（从生成的 `.net` 文件重新加载聚簇网表）；`LOAD` 则跳过打包、直接 `vpr_load_packing` 读取已有 `.net`。

布线的分派 [`vpr/src/base/vpr_api.cpp:1038-1078`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1038-L1078)：`SKIP` 时假定成功 `route_status = RouteStatus(true, -1)`；`DO` 时按通道宽度是否固定二选一——`NO_FIXED_CHANNEL_WIDTH`（即 `--route_chan_width` 未指定，回顾 u1-l5）调 `vpr_route_min_W` 做最小通道宽度搜索，否则调 `vpr_route_fixed_W`；`LOAD` 调 `vpr_load_routing`。

分析的分派 [`vpr/src/base/vpr_api.cpp:1512-1518`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1512-L1518)：这是 `SKIP_IF_PRIOR_FAIL` 唯一被用到的地方——`!route_status.success()` 时跳过分析并返回 `false`，成功才继续。

**is_pack_only 的判定**：

[`vpr/src/base/vpr_api.cpp:1403-1409`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1403-L1409)：当「打包不跳过，但布局 / AP / 布线 / 分析全跳过」时返回真，用于让 `vpr_create_device` 知道不必构建布线 RR 图。

#### 4.2.4 代码实践

**实践目标**：体会 `e_stage_action` 如何让同一份 `vpr_flow` 代码同时支持「从头跑」「从中间阶段加载」「只跑到某阶段」三种用法。

**操作步骤**：

1. 阅读 [`vpr/src/base/vpr_api.cpp:878-929`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L878-L929) 的 `vpr_place_flow`，注意 L886-893 那段 `min_w_search_with_re_placement` 的逻辑。
2. 思考：当用户同时指定「布线要做最小通道宽度搜索」且「布局频率为 ALWAYS」时，为什么这里要**跳过**显式布局（L895）？

**需要观察的现象**：`min_w_search_with_re_placement` 为真时，`vpr_place_flow` 在 `do_placement == DO` 的情况下依然不调用 `vpr_place`，而是把布局职责让给后续的最小通道宽度二分搜索（它会在每次宽度尝试时自己重新布局）。

**预期结果**：理解「阶段并非绝对串行」——布线阶段在特定配置下会反过来接管布局。这是阶段之间少见的耦合点，源码注释明确指出了这种「由二分搜索自己管理布局」的设计。

**待本地验证**：可选地，用一个小电路分别跑 `--route_chan_width 30`（固定宽度，走正常布局）和不带该参数（最小宽度搜索），对比日志里布局是否被打包/布线阶段「借用」。

#### 4.2.5 小练习与答案

**练习 1**：`vpr_pack_flow` 在 `DO` 模式下，为什么 `vpr_pack` 之后还要再调一次 `vpr_load_packing`？

**参考答案**：因为 `vpr_pack`（内部走 `try_pack`）生成的是 `.net` 文件形式的聚簇结果，而后续布局 / 布线需要的 `ClusteredNetlist` 内存结构是由 `vpr_load_packing` 通过 `read_netlist` 重新从 `.net` 文件加载并校验得到的。源码 L694-697 的 `TODO` 也坦言这与布局 / 布线「自己加载数据」的约定不完全一致，是一个历史遗留。

**练习 2**：`vpr_route_flow` 在 `SKIP` 模式下返回的 `RouteStatus(true, -1)` 含义是什么？为什么通道宽度是 -1？

**参考答案**：表示「假定布线成功，但不知道通道宽度」。`-1` 是 `RouteStatus` 里通道宽度的「未定义」占位值（见 `RouteStatus` 默认值 [`vpr_types.h:1699-1700`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_types.h#L1699-L1700)）。跳过布线通常意味着用户只想做布局，此时没有真实的通道宽度可言。

---

### 4.3 阶段间数据传递

#### 4.3.1 概念说明

前两节讲的是「调用顺序」，本节讲「数据怎么在阶段之间流动」。VPR 的答案极其统一：

- **状态走全局总线 `g_vpr_ctx`**：每个阶段把自己产出的结果写进对应的子上下文，下一个阶段从同一个子上下文里读。阶段之间**不直接互相传参**，而是通过这个「共享黑板」交接。
- **配置走 `t_vpr_setup` + `t_arch`**：这两个对象作为引用参数一路传递，提供「这次运行要怎么做」的说明。

于是阶段间数据传递的本质就是一张「谁写哪个子上下文、谁读哪个子上下文」的表。本节就把它建出来。

#### 4.3.2 核心流程

| 阶段（函数） | 主要**写入**的子上下文 | 主要**读取**的子上下文 | 产物 |
| --- | --- | --- | --- |
| 初始化 `vpr_init_with_options` | Atom（netlist）、Timing（graph、constraints）、Power（activity） | — | 原子网表、时序图、约束 |
| 打包 `vpr_pack_flow` → `vpr_load_packing` | Clustering（`clb_nlist`、`atoms_lookup`） | Atom、Device（逻辑块类型） | 聚簇网表 `.net` |
| 建器件 `vpr_create_device` | Device（`grid`、`rr_graph`、时钟网、NoC） | Clustering（资源需求）、Arch | 器件网格、RR 图 |
| 布局 `vpr_place_flow` → `vpr_place` | Placement（`block_locs`、`place_macros`、`placement_id`） | Clustering、Device | 布局 `.place` |
| 布线 `vpr_route_flow` | Routing（`route_trees` 等） | Placement、Device（`rr_graph`）、Timing | 布线 `.route` |
| 分析 `vpr_analysis_flow` → `vpr_analysis` | Timing（统计）、可选 Power | Routing、Placement、Atom、Clustering | 时序报告、功耗 |

一个关键细节是**布线输入网表的选择**（即上一节 L544 那行）：

- 默认（`is_flat == false`）：布线用的是 `g_vpr_ctx.clustering().clb_nlist`（聚簇网表），即「先打包成逻辑块，再对块间连线布线」。
- 扁平布线（`is_flat == true`，`RouterOpts.flat_routing` 打开）：布线直接用 `g_vpr_ctx.atom().netlist()`（原子网表），跳过聚簇抽象，对原子级连线直接布线。此时 `vpr_flow` 还会调用 `unset_port_equivalences` 关闭端口等价性（因为扁平布线不支持）。

这正好把本讲和 u3-l2（AtomNetlist）、u3-l3（ClusteredNetlist）串起来：两种网表在 `vpr_flow` 里按 `is_flat` 二选一地喂给布线器。

#### 4.3.3 源码精读

**聚簇网表在打包阶段诞生**：

[`vpr/src/base/vpr_api.cpp:781-838`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L781-L838) 的 `vpr_load_packing` 是 `ClusteredNetlist`（u3-l3）真正被构造出来的地方：

```cpp
auto& cluster_ctx = g_vpr_ctx.mutable_clustering();
...
cluster_ctx.clb_nlist = read_netlist(vpr_setup.FileNameOpts.NetFile.c_str(),
                                     &arch, ...);
init_clb_atoms_lookup(cluster_ctx.atoms_lookup, atom_ctx, cluster_ctx.clb_nlist);
...
unsigned num_errors = verify_clustering(g_vpr_ctx);  // 校验聚簇一致性
```

注意它同时建立了 `atoms_lookup`——这是聚簇层与原子层之间的映射（u3-l3 提到的 `ClusterAtomsLookup`），让后续时序分析能把聚簇结果下放回原子层。

**建器件读聚簇网表来估算资源**：

[`vpr/src/base/vpr_api.cpp:595-623`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L595-L623) 的 `vpr_create_device_grid` 先遍历 `cluster_ctx.clb_nlist.blocks()` 统计每种逻辑块需要多少个（L606-609），再交给 `create_device_grid`（u2-l3）据此决定网格尺寸。这是一个典型的「上一阶段产物驱动下一阶段决策」的交接点。

**布线后把多层网表同步一致**：

[`vpr/src/base/vpr_api.cpp:1535-1546`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1535-L1546) 的分析阶段里，若布线成功，会调用 `sync_netlists_to_routing(...)`（或扁平版的 `sync_netlists_to_routing_flat()`），把布线结果回填，保持 atom ↔ cluster ↔ routing 三层网表的一致性。这是「数据传递」不仅向前、还会向回同步的体现。

**释放阶段按依赖顺序拆解**：

[`vpr/src/base/vpr_api.cpp:1411-1431`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L1411-L1431) 的 `vpr_free_all` 先 `free_rr_graph()`、再 `free_route_structs()`，最后调 `vpr_free_vpr_data_structures`（依次释放 lb_type_rr_graph、circuit、arch、device、placement、routing、atoms、noc）。释放顺序与构建顺序**相反**，反映数据之间的依赖关系——先建的最后释放。

#### 4.3.4 代码实践

**实践目标**：亲手验证「聚簇网表是打包阶段写、布局阶段读」这条数据流。

**操作步骤**：

1. 在 `vpr_load_packing`（[`vpr_api.cpp:787`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L787)）找到 `g_vpr_ctx.mutable_clustering()` 的写入点，确认 `clb_nlist` 在此处被赋值。
2. 在 `vpr_flow`（[`vpr_api.cpp:536`](https://github.com/verilog-to-routing/vtr-verilog-to-routing/blob/c3ad1ec64818c9db312e24967de9d6283410ab19/vpr/src/base/vpr_api.cpp#L536)）找到布局前读取 `g_vpr_ctx.clustering().clb_nlist` 的点，确认它用的是**只读**的 `clustering()` 而非 `mutable_clustering()`。
3. 跟踪这个 `placement_net_list` 如何作为参数传入 `vpr_place_flow` → `vpr_place` → `try_place`。

**需要观察的现象**：打包用 `mutable_clustering()` 写，布局用 `clustering()` 只读读——这正是 u3-l4 「生产者取 mutable、消费者取 const」约定在主流程里的体现。

**预期结果**：你会清楚地看到 `ClusteredNetlist` 这一个对象「在打包阶段出生、在布局 / 布线阶段被消费」的完整生命线，而它唯一的栖身之所就是 `g_vpr_ctx.clustering().clb_nlist`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `vpr_flow` 在布局处用 `g_vpr_ctx.clustering().clb_nlist`（const），而 `vpr_load_packing` 用 `g_vpr_ctx.mutable_clustering()`？

**参考答案**：`vpr_load_packing` 是聚簇网表的**生产者**（要写入 `clb_nlist`），必须用 `mutable_clustering()` 拿到可写引用；`vpr_flow` 把它传给布局时只是**消费者**（只读），用只读的 `clustering()` 即可，这也是 C++ const-correctness 强制「读为默认、写需显式」的体现（u3-l4）。

**练习 2**：扁平布线（`is_flat == true`）时，`vpr_flow` 为什么要把布线输入网表从 `clb_nlist` 换成原子网表，并调用 `unset_port_equivalences`？

**参考答案**：扁平布线绕过聚簇，直接对原子级原语的连线布线，因此要用 `AtomNetlist`；而端口等价性（多个物理引脚可互换）是聚簇 / 块级布线才支持的优化，扁平布线下没有意义甚至会引发错误，所以要关闭。

**练习 3**：`vpr_free_all` 的释放顺序（RR 图 → 路由结构 → 其余）与构建顺序相反，这样设计的原因是什么？

**参考答案**：因为存在依赖关系——RR 图被路由结构引用、路由结构又被 placement/atoms 等引用。先释放被依赖的对象会造成悬垂引用或重复释放；逆序释放保证「先释放依赖者、再释放被依赖者」，与构建顺序正好相反，是资源管理的常见模式。

---

## 5. 综合实践

把本讲三个模块串起来，完成 spec 要求的主任务：**画出从 init 到 analysis 的阶段调用顺序图，并标注每个阶段读写哪个 Context**。

**实践目标**：用一张图把「顶层调用链 + 阶段分派 + 数据交接」三件事一次性表达出来，作为本讲的验收。

**操作步骤**：

1. 以 `main` 为起点，画出 `vpr_init → vpr_init_with_options → SetupVPR / read_and_process_circuit / 建时序图` 这条初始化支线，并在每个箭头旁标注写入的子上下文（Atom、Timing、Power）。
2. 画出 `vpr_flow` 主线：`vpr_pack_flow → vpr_create_device → vpr_place_flow → vpr_route_flow → vpr_analysis_flow`，用 `e_stage_action` 的 DO/LOAD/SKIP 标注每个 `*_flow` 内部的三种分支。
3. 在主线下方画一条「数据流」泳道：`AtomNetlist →（打包）→ ClusteredNetlist →（布局）→ PlacementContext.block_locs →（布线）→ RoutingContext.route_trees →（分析）→ TimingContext.stats`，每一步标出它读写 `g_vpr_ctx` 的哪个子上下文。
4. 在图上标出两个特殊点：AP 分叉（`APOpts.doAP == DO` 时自带建器件）、扁平布线分叉（`is_flat` 时布线改用原子网表）。
5. 最后画一条 `vpr_free_all` 的释放箭头，标注逆序释放顺序。

**需要观察的现象**：你会发现整张图里，阶段之间的箭头**几乎不携带数据载荷**——载荷全在 `g_vpr_ctx` 这条共享总线上，箭头只表示「控制流 / 调用顺序」。唯一显式传递的核心数据对象是 `net_list`（聚簇网表或原子网表），而且它本身就是从 `g_vpr_ctx` 里取出来的。

**预期结果**：得到一张「控制流（上）+ 数据流（下）」的双泳道图。这张图就是 VPR 主流程的全貌，后续学习打包（u4）、布局（u5）、布线（u6）、时序（u7）各单元时，都可以把对应章节回填到这张图的相应阶段里。

**待本地验证**：可选地，开启 VPR 的 echo 文件选项（`--echo_file on`）或观察 `VTR_LOG` 计时输出，把日志里出现的阶段计时顺序与你画的图对照，验证阶段执行顺序与预期一致。

## 6. 本讲小结

- `main.cpp` 是瘦壳：栈上声明 `t_options / t_arch / t_vpr_setup`，按 `vpr_init → vpr_flow → vpr_free_all` 三步调用，三层 `catch` 兜底且每条分支都释放资源。
- 初始化（`vpr_init_with_options`）负责把命令行 + 架构 + 电路整理成配置对象 `t_vpr_setup`，并提前建好不依赖后续阶段的 `AtomContext`（原子网表）与 `TimingContext`（时序图 + 约束）。
- `vpr_flow` 是主动脉，按「打包 →（可选 AP）→ 建器件 → 布局 → 布线 → 分析」顺序分派，每个 `*_flow` 内部用 `e_stage_action`（DO/LOAD/SKIP/SKIP_IF_PRIOR_FAIL）决定是真正运行、加载已有结果还是跳过。
- 阶段间数据靠全局总线 `g_vpr_ctx` 交接：每个阶段写自己负责的子上下文、读上一阶段的产物；`t_vpr_setup` 与 `t_arch` 作为配置一路传递。
- 成功传播用 `bool` / `RouteStatus`：打包、布局失败立刻中止并返回「不可实现」；布线返回带通道宽度的 `RouteStatus`；分析即使布线失败也会对非法实现出报告。
- 释放（`vpr_free_all`）按与构建相反的依赖顺序拆解所有数据结构。

## 7. 下一步学习建议

本讲把「主流程骨架」讲完了，接下来的单元会沿着这条骨架逐阶段深入：

- **u4 打包 Packing**：进入 `vpr_pack` 内部的 `try_pack`，看原子如何被聚成逻辑块、最终产出本讲提到的 `ClusteredNetlist`。
- **u5 布局 Placement**：进入 `vpr_place` 内部的 `try_place`，看模拟退火如何把聚簇块摆到 `PlacementContext.block_locs` 里。
- **u6 布线 Routing**：进入 `vpr_route_flow` 背后的 `vpr_route_min_W / vpr_route_fixed_W`，看 RR 图与迷宫布线如何产出 `RoutingContext.route_trees`。
- **u7 时序分析与布线后评估**：进入 `vpr_analysis`，看它如何用本讲初始化阶段建好的时序图 + 约束，结合布线延迟产出关键路径与时序报告。

建议在进入下一单元前，先把本讲「综合实践」那张双泳道图完成——它会成为你阅读后续阶段代码时的「地图」，让你随时知道当前函数处在 `vpr_flow` 的哪一段、读写的是哪个子上下文。
