# 算法模板 Template

## 1. 本讲目标

上一讲（u3-l4）我们看清了**算法执行器 Executor**：它接住 Selector 产出的 `algName`，负责「算资源 + 编排执行」。但 executor 自己并不真正搬数据——它把数据搬移的细节委托给了一个更底层的组件：**算法模板 Template**。

本讲就钻进 template 子系统。读完本讲，你应当能够：

1. 说出 template 在「Selector → Executor → **Template**」三级链路中的职责——**按「算法 × 引擎」的具体组合，真正下发数据搬移指令**。
2. 掌握模板抽象基类 `InsAlgTemplateBase`（及其父类 `CommonAlgTemplateBase`）的生命周期接口：`Describe / CalcRes / KernelRun / GetRes / CalcScratchMultiple / GetThreadNum`，并分清「host 资源计算阶段」与「device 执行阶段」分别调谁。
3. 理解模板注册表 `InsAlgTemplateRegistry` 与 `REGISTER_TEMPLATE_V2` 宏的运行期字符串查表机制，**同时**认清 HCCL 里 template 还有第二条「编译期模板参数绑定」路径，并能区分两者。
4. 顺着真实源码读完一个具体模板 `InsTempAllReduceMesh1DOneShot`（AICPU 引擎下的 1D Mesh one-shot AllReduce），讲清 `CalcRes`（算 channel/thread/mem）与 `KernelRun`（下发数据搬移）各做了什么。

## 2. 前置知识

本讲承接 u3-l4，假定你已经掌握：

- **三级链路与 algName 字符串契约**：Selector 产出 `algName`（如 `AicpuAllReduceSoleMeshOneShot`），Executor 拿它查注册表得到「已经绑好 template 的 executor 实例」（见 u3-l1、u3-l4）。
- **引擎（CommEngine）与算法正交**：引擎有 AICPU_TS / AIV / CCU 三类（见 u1-l2、u2-l4）；算法有 Ring / Mesh / NHR 等。template 恰好是「算法 × 引擎」二维表的交叉点——同一个 Mesh 算法，在 AICPU 引擎下有一个模板，在 AIV、CCU 引擎下各有另一个模板。
- **thread / channel / notify 三类执行资源**（见 u3-l4）：thread 是执行上下文（一条流），channel 是通信通道（两端设备 + 协议 + 若干 notify），notify 是线程间同步信号。模板既要「申请」它们（`CalcRes`），又要「消费」它们（`KernelRun`）。

两个本讲会反复用到、但尚未细讲的结构：

- `AlgResourceRequest`：模板在 host 阶段产出的「资源需求单」，列出要几个 slave thread、每个 thread 要几个 notify、要哪些 channel、要哪些 CCU kernel。
- `TemplateDataParams` / `TemplateResource`：executor 在 device 阶段塞给模板的「数据参数」与「已分配好的资源句柄」。

一句话定位：**template 是离硬件最近的软件抽象**——executor 决定「用哪套算法、怎么编排」，template 决定「在这一步里，具体把哪段数据、从哪个地址、经哪条 channel、搬到哪个对端地址」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/ops/op_common/template/common_alg_template_base.h` | 模板最顶层抽象 `CommonAlgTemplateBase`，声明全部纯虚生命周期接口。 |
| `src/ops/op_common/template/alg_v2_template_base.h` / `.cc` | 本仓 template 的核心抽象 `InsAlgTemplateBase`，持有 rank/切片/notify 等公共成员，给出默认（报错）实现。 |
| `src/ops/op_common/template/registry/alg_v2_template_register.h` / `.cc` | 模板注册表 `InsAlgTemplateRegistry`（单例 + 字符串键 + 工厂）与 `REGISTER_TEMPLATE_V2` 宏。 |
| `src/ops/op_common/template/dpu/kernel_launch.cc` | DPU 路径下**运行期**按 `templateName` 字符串查表得到模板、再调 `DPUKernelRun` 的入口。 |
| `src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_nhr.h` | 另一个 AICPU AllReduce 模板（NHR 算法）的头文件，用于对比模板形态。 |
| `src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.h` / `.cc` | 本讲主角：AICPU 引擎下 1D Mesh one-shot AllReduce 模板的完整实现。 |
| `src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.h` / `.cc` | 消费上述模板的 executor，演示「编译期把 template 绑成 executor 的模板参数」这条路径。 |
| `src/ops/op_common/inc/alg_param.h` / `src/ops/op_common/template/template_utils.h` | `AlgResourceRequest`、`ChannelInfo`、`BuffInfo`、`TemplateDataParams`、`TemplateResource` 等结构定义。 |

## 4. 核心概念与源码讲解

### 4.1 模板抽象：从 CommonAlgTemplateBase 到 InsAlgTemplateBase

#### 4.1.1 概念说明

template 子系统采用了**两层抽象**：

- `CommonAlgTemplateBase`：最顶层抽象，只声明「一个通信模板必须能做什么」，不持有任何具体成员。它定义了模板的**生命周期接口契约**。
- `InsAlgTemplateBase`：继承自上面，是本仓（"v2" 架构）所有具体模板的直接父类。它在契约之上补齐了具体模板都需要的公共数据成员（我是哪个 rank、子通信域长什么样、数据类型、归约算子、主从线程的 notify 索引等），并为部分接口提供了「默认实现」。

模板的生命周期被切成**两个阶段**，理解这一点是理解整个 template 子系统的钥匙：

1. **host 资源计算阶段**：在 host 侧、真正下发任务之前调用。executor 调 `CalcRes(...)`，让模板把「我这套算法要跑起来需要多少 thread / notify / channel / 多大 scratch 显存」算清楚，填进一张资源需求单 `AlgResourceRequest`。这个阶段**不搬任何数据**，纯算术 + 拓扑查询，属控制面。
2. **device 执行阶段**：资源分配好之后，executor 调 `KernelRun(...)`，模板才真正组织「本地拷贝 → 远端收发 → 本地归约」等数据搬移动作，经 channel 把数据搬过网。这属数据面。

此外还有两个辅助接口：`CalcScratchMultiple` 告诉 executor「每个用户数据单位需要几份 scratch 显存」（executor 用它来决定一次循环搬多少、要循环几轮）；`Describe` 返回一段自描述字符串，仅用于日志。

#### 4.1.2 核心流程

模板在三级链路里的位置与两阶段调用：

```
Selector 产出 algName
        │
        ▼
Executor（HcclExecOp）按 algName 查注册表 → 得到「已绑好 template 的 executor」
        │
        ├── host 阶段：executor.CalcRes(...)
        │       └── 内部 make_shared<Template>() → template.CalcRes(comm, param, topoInfo, resourceRequest)
        │                                              └── 填 AlgResourceRequest（thread/notify/channel/ccuKernel）
        │
        └── device 阶段：executor.Orchestrate(...) → OrchestrateLoop(...)
                ├── template.CalcScratchMultiple(...)  → 决定单轮数据量、循环轮数
                └── for 每个数据块 loop:
                        template.KernelRun(param, tempAlgParams, templateResource)
                              └── 组织 LocalCopy / SendRecvBatchWrite / LocalReduce ...
```

模板接口契约一览（哪些是必须重写的）：

| 接口 | 阶段 | 在 `CommonAlgTemplateBase` | 在 `InsAlgTemplateBase` | 含义 |
| --- | --- | --- | --- | --- |
| `Describe()` | 调试 | 纯虚 | 纯虚（=0） | 返回自描述字符串 |
| `CalcRes(...)` | host | 纯虚 | 默认报错 | 算资源，填 `AlgResourceRequest` |
| `GetRes(...)` | host | 纯虚 | 默认报错 | 取/回填资源（部分模板用） |
| `CalcScratchMultiple(...)` | host | 纯虚 | 默认返回 0 | scratch 显存倍数 |
| `GetThreadNum()` | host | 纯虚 | 默认返回 0 | 模板期望线程数 |
| `KernelRun(...)` | device | 纯虚 | 默认报错 | 下发数据搬移 |
| `FastLaunch(...)` | device | 纯虚 | 默认报错 | CCU 快速回放路径 |
| `GetNotifyIdxMainToSub/SubToMain` | device | —（InsAlgTemplateBase 新增） | 纯虚 | 主从线程 notify 索引 |

注意 `InsAlgTemplateBase` 把 `CalcRes/KernelRun/GetRes` 的默认实现写成**返回 `HCCL_E_INTERNAL` 并打 `HCCL_ERROR("Unsupported interface")`**——这是一种「你不重写我就直接报错」的防御式设计，强制具体模板必须显式实现自己用到的接口。

#### 4.1.3 源码精读

先看顶层抽象 `CommonAlgTemplateBase`，它只声明契约、不持有成员：

[common_alg_template_base.h:19-38](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/common_alg_template_base.h#L19-L38) —— 全部为纯虚函数：`Describe/CalcRes/GetRes/GetThreadNum/CalcScratchMultiple/KernelRun/FastLaunch`，外加两个带默认实现的辅助方法 `CalcDataSplitByPortGroup`、`SetchannelsPerRank`，以及一个 protected 成员 `channelsPerRank_`。这是「模板必须能做什么」的契约定义。

再看本仓核心抽象 `InsAlgTemplateBase` 的类声明：

[alg_v2_template_base.h:18-24](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/alg_v2_template_base.h#L18-L24) —— `class InsAlgTemplateBase : public CommonAlgTemplateBase`，构造函数接收 `param`、`rankId`（即 userRank）、`subCommRanks`（子通信域的 rank 列表）。

[alg_v2_template_base.h:52-75](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/alg_v2_template_base.h#L52-L75) —— protected 成员：`opMode_`（单算子/图模式）、`myRank_`、`templateRankSize_`（本模板参与的 rank 数）、`subCommRanks_`、`buffInfo_`、`threadNum_`、`reduceOp_`、`dataType_`，以及主从线程同步用的 `notifyIdxMainToSub_` / `notifyIdxSubToMain_`。这些都是任何具体模板都要用的公共状态，所以抽到父类。

构造函数里有一个值得注意的小算术——如何由 `subCommRanks` 推出 `templateRankSize_`：

[alg_v2_template_base.cc:15-30](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/alg_v2_template_base.cc#L15-L30) —— 若 `subCommRanks` 有两级（节点内 + 节点间），则 `templateRankSize_ = level0.size() * level1.size()`；只有一级时取 `level0.size()`。这正是分级通信在模板层的体现：一个模板实例覆盖的 rank 数，等于它所负责的各层子通信域规模之乘积。

默认（报错）实现：

[alg_v2_template_base.cc:42-62](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/alg_v2_template_base.cc#L42-L62) —— `KernelRun / DPUKernelRun / CalcRes / GetRes` 的默认实现统统 `(void)参数;` 后打 `HCCL_ERROR("Unsupported interface ...")` 并返回 `HCCL_E_INTERNAL`。[alg_v2_template_base.cc:83-85](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/alg_v2_template_base.cc#L83-L85) —— `CalcScratchMultiple` 默认返回 0、`GetThreadNum` 默认返回 0。**含义**：具体模板必须重写自己真正用到的接口，否则运行期会以内部错误失败——这是一种「用默认报错代替纯虚」的折中，允许不同模板只实现自己需要的子集。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：搞清「一个具体模板最少必须重写哪些接口」「哪些可以不重写」。
2. **操作步骤**：
   - 打开 [alg_v2_template_base.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/alg_v2_template_base.h)，把所有 `virtual` 方法分成三类：①`= 0` 的纯虚（必须重写）；②带 `{ }` 或在 `.cc` 里有默认实现的（可重写）；③非虚的普通方法（直接继承）。
   - 再打开本讲主角 [ins_temp_all_reduce_mesh_1D_one_shot.h](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.h)，对照它的 `override` 列表。
3. **需要观察的现象**：`Describe`、`GetNotifyIdxMainToSub`、`GetNotifyIdxSubToMain` 是纯虚，必须重写；`KernelRun/CalcRes/CalcScratchMultiple` 它重写了；而 `GetRes`、`GetThreadNum`、`FastLaunch`、`DPUKernelRun` 它**没有**重写——因此继承父类的默认实现（`GetRes` 会返回 `HCCL_E_INTERNAL`，`GetThreadNum` 返回 0）。
4. **预期结果**：你能列出「mesh_1d_one_shot 实际重写的方法清单」，并解释为什么它不重写 `GetRes` 也不会出问题（因为它的 executor 根本不调 `GetRes`，只调 `CalcRes` 与 `KernelRun`）。
5. 运行行为属「待本地验证」（本实践为源码阅读，无需运行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `InsAlgTemplateBase` 不把 `KernelRun/CalcRes` 直接写成纯虚 `= 0`，而是给一个返回 `HCCL_E_INTERNAL` 的默认实现？

**参考答案**：因为不同引擎的模板用到的接口子集不同（例如 DPU 模板走 `DPUKernelRun`，CCU 模板走 `FastLaunch`，AICPU 模板走 `KernelRun`）。写成纯虚会强迫每个模板都实现全部接口；写成「默认报错」则允许每个模板只实现自己需要的，调到没实现的接口时以明确的内部错误失败，既灵活又不至于静默出错。

**练习 2**：`templateRankSize_` 与全局通信域的 `rankSize` 有何区别？

**参考答案**：`rankSize` 是整条通信域的总 rank 数；`templateRankSize_` 是**本模板实例**所负责的那一层子通信域的 rank 数（可能只是节点内的一组卡）。分级通信把一个大通信域切成多级子组，每一级由各自的模板实例处理，所以 `templateRankSize_` 通常远小于全局 `rankSize`。

---

### 4.2 模板注册：InsAlgTemplateRegistry 与 REGISTER_TEMPLATE_V2（及编译期绑定）

#### 4.2.1 概念说明

template 与 executor 之间有**两条不同的「绑定点」**，必须分清，否则会误以为「每个模板都注册在一个字符串表里」。这两条路径并存于本仓：

1. **运行期字符串注册表 `InsAlgTemplateRegistry`**：靠 `REGISTER_TEMPLATE_V2(name, ClassName)` 宏，在程序启动时把「字符串名 → 工厂函数」登记进一个单例 map；运行时用 `GetAlgTemplate(name)` 按名字查表、`new` 出实例。典型消费者是 **DPU 路径**：device 侧从共享内存反序列化出一个 `templateName` 字符串，再查表得到模板。
2. **编译期模板参数绑定**：把 template 类作为**类型参数**直接烘进 executor 类模板（`InsV2AllReduceSoleExecutor<TopoMatch, Template>`），再用 `REGISTER_EXEC_V2(...)` 把 `(opType, algName)` 绑到这个已经实例化好的 executor 类上。这条路径**根本没有运行期字符串查 template 的步骤**——executor 直接 `std::make_shared<Template>()`，类型在编译期就定死了。

本讲的主角 `InsTempAllReduceMesh1DOneShot` 走的就是**第二条（编译期绑定）**，所以你在全仓 grep `REGISTER_TEMPLATE_V2` 时**找不到它**——这一点很重要，下面会用源码逐一证实。

> 与 u3-l1/u3-l4 的衔接：u3-l1 讲的「executor 与 template 在注册宏中编译期绑定（template 作 executor 模板参数）」指的就是第二条路径；而 `InsAlgTemplateRegistry` 这张字符串表是给 DPU 等特殊路径用的运行期查表能力。

#### 4.2.2 核心流程

两条路径的数据流对比：

```
路径① 运行期字符串注册表（DPU 等）
   启动期：REGISTER_TEMPLATE_V2("XxxTemplate", XxxClass)
              └→ InsAlgTemplateRegistry::Register("XxxTemplate", 工厂)
   运行期：GetAlgTemplate("XxxTemplate") → new XxxClass → DPUKernelRun(...)

路径② 编译期模板参数绑定（AICPU/AIV/CCU 主路径）
   编译期：REGISTER_EXEC_V2(cmd, algName, ExecutorClass, TopoMatcher, TemplateClass)
              └→ executor 类被实例化为 ExecutorClass<TopoMatcher, TemplateClass>
   运行期：HcclExecOp 按 (cmd, algName) 查 executor 注册表 → new ExecutorClass<...>
              └→ executor 内部 make_shared<TemplateClass>() → CalcRes / KernelRun
```

注册表的内部结构是经典的「单例 + 二级 map + 工厂」，和 selector/executor 注册表同构（见 u3-l1 的三大注册表）。`REGISTER_TEMPLATE_V2` 宏用 `__COUNTER__` 给每个注册点生成唯一的静态变量名，靠静态初始化在 `main` 之前完成登记。

#### 4.2.3 源码精读

先看注册表的类型与工厂：

[alg_v2_template_register.h:22-30](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/registry/alg_v2_template_register.h#L22-L30) —— `InsAlgTemplateCreator = std::function<InsAlgTemplateBase*()>` 是工厂函数类型；`DefaultTemplateCreatorV2<P>()` 是个函数模板，带 `static_assert(is_base_of<InsAlgTemplateBase, P>)` 编译期校验，返回 `new (std::nothrow) P()`。

注册表类与宏：

[alg_v2_template_register.h:32-48](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/registry/alg_v2_template_register.h#L32-L48) —— `InsAlgTemplateRegistry`：`Instance()` 取单例、`Register(name, creator)` 登记、`GetAlgTemplate(name)` 按名取实例；内部 `std::map<std::string, InsAlgTemplateCreator> tempCreators_` 加一把 `mutex`。`REGISTER_TEMPLATE_V2(name, insAlgTempBase)` 宏展开为一个静态变量，初始化时调用 `Instance().Register(name, DefaultTemplateCreatorV2<insAlgTempBase>())`。

注册表的实现细节：

[alg_v2_template_register.cc:15-45](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/registry/alg_v2_template_register.cc#L15-L45) —— `Instance()` 是 Meyers 单例（函数内 `static`）；`Register` 在加锁后检查重名（已存在且非空则报 `HCCL_E_INTERNAL`）；`GetAlgTemplate` 找不到名字或工厂为空时返回 `nullptr`，否则用工厂构造并包成 `unique_ptr` 返回。

**谁在运行期用这张表？** 典型消费者是 DPU kernel 下发：

[kernel_launch.cc:29-42](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/op_common/template/dpu/kernel_launch.cc#L29-L42) —— `HcclLaunchDPUKernel` 从共享内存反序列化出 `DPURunInfo`，取其中的 `dpuRunInfo.templateName` 字符串，调 `InsAlgTemplateRegistry::Instance().GetAlgTemplate(templateName)` 得到模板实例，再调 `templateIns->DPUKernelRun(...)`。这才是 `REGISTER_TEMPLATE_V2` 注册名的真正用途——它必须能被序列化/反序列化、跨进程按名字还原。

那么哪些模板真的用了 `REGISTER_TEMPLATE_V2`？以一个 DPU 跨节点模板为例：

[ins_temp_all_gather_nhr_dpu_inter.cc:259](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/template/aicpu/ins_temp_all_gather_nhr_dpu_inter.cc#L259) —— `REGISTER_TEMPLATE_V2("InsTempAllGatherNhrDpuInter", InsTempAllGatherNhrDpuInter);`，注册名与类名一致，供 DPU 路径按字符串查表。

**对比：本讲主角走的是编译期绑定，不是字符串注册表。** 看它实际出现在哪里：

[ins_v2_all_reduce_sole_executor.cc:271-273](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc#L271-L273) —— `REGISTER_EXEC_V2(HCCL_CMD_ALLREDUCE, AicpuAllReduceSoleMeshOneShot, InsV2AllReduceSoleExecutor, TopoMatch1D, InsTempAllReduceMesh1DOneShot);`。这里 `InsTempAllReduceMesh1DOneShot` 是作为**类型实参**传给 executor 类模板的，绑定的字符串 `AicpuAllReduceSoleMeshOneShot` 是 **algName**（executor 注册表的键），**不是** template 注册表的键。换句话说，全仓搜不到 `REGISTER_TEMPLATE_V2("...", InsTempAllReduceMesh1DOneShot)` 这一行——因为它根本没进字符串注册表。

executor 类模板本身长这样：

[ins_v2_all_reduce_sole_executor.h:19-23](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.h#L19-L23) —— `template <typename AlgTopoMatch, typename InsAlgTemplate> class InsV2AllReduceSoleExecutor : public InsCollAlgBase`。两个模板参数：拓扑匹配器与模板类。编译期绑定后，executor 内部直接构造该模板类型：

[ins_v2_all_reduce_sole_executor.cc:54-59](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc#L54-L59) —— host 阶段 `CalcRes`：`std::make_shared<InsAlgTemplate>(param, topoInfo->userRank, algHierarchyInfo.infos[0])` 直接 `new` 出模板（类型已定死），再调 `algTemplate->CalcRes(comm, param, topoInfo, resourceRequest)`。

[ins_v2_all_reduce_sole_executor.cc:126-134](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc#L126-L134) —— device 阶段 `OrchestrateLoop`：同样 `make_shared<InsAlgTemplate>(...)`，然后调 `algTemplate->CalcScratchMultiple(...)` 拿到 scratch 倍数，用来算单轮最大数据量与循环轮数。

[ins_v2_all_reduce_sole_executor.cc:165-188](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc#L165-L188) —— 数据分块循环：按 `loopTimes` 把 `dataCount_` 切成若干块，每块设好 `tempAlgParams`（count、sliceSize、各 baseOff），然后 [第 186 行](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc#L186) `algTemplate->KernelRun(param, tempAlgParams, templateAlgRes)`。**这就是 template 与 executor 的最终交汇点**：executor 负责切块与循环，template 负责每一块内部的具体搬移。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：亲手验证「本讲主角 `InsTempAllReduceMesh1DOneShot` 没有进 `REGISTER_TEMPLATE_V2` 字符串注册表，而是编译期绑进 executor」。
2. **操作步骤**：
   - 在仓库根目录执行（只读检索）：
     - 搜注册宏：`grep -rn "REGISTER_TEMPLATE_V2(" src/` —— 查看哪些模板类被字符串注册。
     - 搜主角类名：`grep -rn "InsTempAllReduceMesh1DOneShot" src/ | grep -i register` —— 期望**无输出**（即没有字符串注册）。
     - 搜它在 executor 里的绑定：`grep -rn "InsTempAllReduceMesh1DOneShot" src/ops/all_reduce/executor/` —— 期望命中 `ins_v2_all_reduce_sole_executor.cc` 的 `REGISTER_EXEC_V2`。
3. **需要观察的现象**：主角类只出现在 executor 的 `REGISTER_EXEC_V2` 里，作为类型实参；而 `REGISTER_TEMPLATE_V2` 的命中都是 `...DpuInter` 之类的 DPU 模板。
4. **预期结果**：得出结论——AICPU/AIV/CCU 主路径模板走编译期绑定，algName `AicpuAllReduceSoleMeshOneShot` 是 executor 注册表的键而非 template 注册表的键；只有 DPU 等需要跨进程按名字还原的路径才用 `REGISTER_TEMPLATE_V2`。
5. 运行行为属「待本地验证」（本实践为只读检索，无需编译/上板）。

#### 4.2.5 小练习与答案

**练习 1**：`REGISTER_TEMPLATE_V2` 宏里为什么用 `__COUNTER__` 拼静态变量名？

**参考答案**：因为同一个翻译单元里可能注册多个模板，甚至同一行宏被多处调用。用 `__COUNTER__`（每次展开递增）拼出 `g_func_<类名>_<计数>` 这样的唯一变量名，保证每个注册点的静态变量互不冲突，从而各自的静态初始化都能正确执行一次注册。

**练习 2**：既然主路径用编译期绑定，为什么还要保留 `InsAlgTemplateRegistry` 这张运行期字符串表？

**参考答案**：因为存在「device 侧从共享内存反序列化得到模板名、再按名查表构造」的场景（DPU 路径 `HcclLaunchDPUKernel`）。这种场景下构造点的代码看不到具体 C++ 类型（跨进程、跨编译单元），只能靠字符串中转，于是需要运行期注册表。两条路径服务于不同的部署形态。

---

### 4.3 具体模板精读：InsTempAllReduceMesh1DOneShot

#### 4.3.1 概念说明

现在把抽象落到一个具体模板上：`InsTempAllReduceMesh1DOneShot`——AICPU 引擎下、1D Mesh 拓扑、one-shot（一轮）搬运的 AllReduce 模板。先建立直觉：

- **1D Mesh**：所有 rank 排成一条一维的网格，任意两两之间都有一条直连链路（全连接），不需要像 Ring 那样逐跳转发。
- **one-shot**：每个 rank **一次性**把自己的数据并发地推给所有其它 rank（每个对端各占一个 slave thread），而不是分多轮流水。这适合**数据量较小、追求低延迟**的场景；数据量大时它会退化（要占用 N−1 个 thread），此时 selector 会改选 two-shot 或 NHR（见 u3-l2）。
- **AllReduce 的三步**：在本模板里被组织成 ① 本地拷贝（把自己那份数据先放进输出缓冲）→ ② 并发收发（每个 rank 把自己的数据写到所有对端的 cclBuff 槽位）→ ③ 本地归约（把收到的各对端数据累加归约进输出缓冲）。

注意第 ② 步是**写远端 cclBuff**（跨 rank 的中间缓冲），不是直接写远端用户输出；第 ③ 步才在本地把所有槽位归约进自己的 `outputPtr`。这是 Mesh one-shot 的典型数据流。

#### 4.3.2 核心流程

一次 `KernelRun` 内部的执行序列（多线程，主线程 + N−1 个 slave thread）：

```
KernelRun(param, tempAlgParams, templateResource)
  │
  ├── 读取 threadNum_ / processSize_ / count_ / dataType_
  ├── 计算 needAicpuReduce_（INT64/UINT64/FP64/PROD 需要 AICPU 归约）
  ├── 断言 threadNum_ == templateRankSize_
  ├── CalcSlice(processSize_) → 每个 rank 一个等大切片（one-shot 不均分，offset = rankIdx * size）
  │
  ├── [多线程] PreSyncInterThreads  主→从 同步（保证 slave 就绪）
  │
  ├── RunAllReduce(...)                         ← 第①②步
  │     ├── 主线程 LocalCopy：userIn → userOut（先把自己那份放进输出）
  │     ├── 若 subCommRanks[0].size()==1 → 直接返回（单卡无需通信）
  │     └── for queIdx in 1..threadNum-1:       （每个 slave thread 对接一个对端）
  │           nextRank = (myRank + queIdx) % templateRankSize_
  │           取 linkSend / linkRecv 两条 channel
  │           构造 tx/rx 切片指向对端 remoteCclMem 的对应槽位
  │           SendRecvBatchWrite(...)  在 thread[queIdx] 上并发收发
  │
  ├── [多线程] PostSyncInterThreads  从→主 同步（保证所有收发完成）
  │
  └── PostLocalReduce(...)                      ← 第③步
        ├── [若 needAicpuReduce_] BatchModeEnd + Join 全部 thread + BatchModeStart
        │     （插一道同步屏障，切换到 AICPU 归约）
        └── for 每个其它 rank：LocalReduce(remote 槽位 → userOut, dataType_, reduceOp_)
```

而 `CalcRes` 则在 host 阶段把这套编排所需的资源算出来：

```
CalcRes(comm, param, topoInfo, resourceRequest)
  ├── threadNum = max(templateRankSize_, 1)
  ├── resourceRequest.slaveThreadNum      = threadNum - 1   （主线程复用用户 stream）
  ├── resourceRequest.notifyNumPerThread  = [1] * slaveThreadNum
  ├── resourceRequest.notifyNumOnMainThread = threadNum - 1
  ├── CalcChannelRequestMesh1D(...) → level0Channels       （按 1D Mesh 算每对 rank 的 channel）
  └── resourceRequest.channels.push_back(level0Channels)
```

资源量与算法形态直接挂钩：one-shot 要「每对端一线程、每线程一 notify」，所以 thread/notify 数都是 N−1；这正解释了为什么数据量一大、N 一多，one-shot 就不划算。

#### 4.3.3 源码精读

类声明与自描述：

[ins_temp_all_reduce_mesh_1D_one_shot.h:20-33](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.h#L20-L33) —— `class InsTempAllReduceMesh1DOneShot : public InsAlgTemplateBase`。`Describe()` 返回 `"Template of all resduce (one-shot) 1D Mesh with tempRankSize N"`（注：源码原文如此拼写）。私有成员 `needAicpuReduce_`、`processSize_`、`count_`，私有辅助方法 `CalcSlice / RunAllReduce / PostLocalReduce`。

[ins_temp_all_reduce_mesh_1D_one_shot.h:46-57](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.h#L46-L57) —— 私有方法与状态声明。

**`CalcRes`（host 阶段，算 channel/thread/notify）：**

[ins_temp_all_reduce_mesh_1D_one_shot.cc:23-38](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.cc#L23-L38) —— 注释「mesh 算法只做 level 0 层级的」；`threadNum = templateRankSize_ > 1 ? templateRankSize_ : 1`；`slaveThreadNum = threadNum - 1`（主线程通过传入的 stream 转换，所以少算一个）；每个 slave thread 配 1 个 notify，主线程配 `threadNum - 1` 个 notify；`CalcChannelRequestMesh1D(...)` 算出 Layer0 的 channel 描述列表塞进 `resourceRequest.channels`。这一段就是「算 channel/thread」的全部。

**`CalcScratchMultiple`（host 阶段，算 mem 倍数）：**

[ins_temp_all_reduce_mesh_1D_one_shot.cc:40-46](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.cc#L40-L46) —— 直接返回 `templateRankSize_`。含义：每个 rank 在 cclBuff 里要占 `templateRankSize_` 个槽位（每个对端一份），所以每份用户数据需要 N 倍 scratch。executor 据此算 `maxDataSizePerLoop = hcclBuff.size / templateScratchMultiplier`，从而决定一次搬多少、循环几轮（见 4.2.3 的 [ins_v2_all_reduce_sole_executor.cc:141-147](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc#L141-L147)）。

**`CalcSlice`（切片，one-shot 不均分）：**

[ins_temp_all_reduce_mesh_1D_one_shot.cc:48-65](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.cc#L48-L65) —— 给每个 rank 切一个等大的切片，`offset` 随 `rankIdx` 线性累加 `dataSize`：`sliceInfoVec[rankIdx][0] = {accumOff, dataSize}`。one-shot 的关键：**每个 rank 拿到的是完整大小的一块**（不是把数据均分成 N 份），因为每个 rank 都要把自己的整份数据发给所有对端。末尾 `CHK_PRT_RET` 校验切片总长恰为 `dataSize * templateRankSize_`。

**`KernelRun`（device 阶段，下发搬移）：**

[ins_temp_all_reduce_mesh_1D_one_shot.cc:67-101](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.cc#L67-L101) —— 依次：读取 `threadNum_ / processSize_ / count_ / dataType_`；[L74-76](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.cc#L74-L76) 算 `needAicpuReduce_`（INT64/UINT64/FP64 或 PROD 归约时为真——这些类型/算子需要走 AICPU 归约核）；断言 `threadNum_ == templateRankSize_`；`CalcSlice`；多线程时先 `PreSyncInterThreads`（主→从）；`RunAllReduce`；多线程时 `PostSyncInterThreads`（从→主）；最后 `PostLocalReduce`。

**`RunAllReduce`（第①②步：本地拷贝 + 并发收发）：**

[ins_temp_all_reduce_mesh_1D_one_shot.cc:103-160](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.cc#L103-L160) —— [L116](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.cc#L116) 主线程 `LocalCopy(threads[0], usrInSlices, usrOutSlices)`（第①步：把自己数据放进输出）；[L119-121](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.cc#L119-L121) 单 rank 早退；[L124-157](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.cc#L124-L157) 循环 `queIdx=1..threadNum-1`：`nextRank = (myRank_ + queIdx) % templateRankSize_`，取该对端的 `linkSend`/`linkRecv` 两条 `ChannelInfo`，构造指向**对端 `remoteCclMem` 对应槽位**的 `txDstSlice`/`rxDstSlice`（注意目标地址 `linkSend.remoteCclMem.addr` + 切片 offset），最后 `SendRecvBatchWrite(sendRecvInfo, threads[queIdx])` 在该 slave thread 上并发收发（第②步）。每个 slave thread 专责一个对端，这正是「one-shot 并发」的体现。

**`PostLocalReduce`（第③步：本地归约）：**

[ins_temp_all_reduce_mesh_1D_one_shot.cc:162-197](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.cc#L162-L197) —— [L168-175](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.cc#L168-L175) 若 `needAicpuReduce_`，先 `HcommBatchModeEnd` + 逐个 `HcommThreadJoin` + `HcommBatchModeStart`——插一道同步屏障，确保前面所有收发任务执行完，再切换到 AICPU 归约模式（因为 64 位/PROD 类型要走 AICPU 归约核）；[L180-195](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.cc#L180-L195) 遍历除自身外的每个 rank，从本地 cclBuff 里对应槽位取数据，`LocalReduce(threads[0], curSrcSlice, curDstSlice, dataType_, reduceOp_)` 归约进 `userOut`。归约完毕，每个 rank 的输出里就是全体的归约结果，AllReduce 完成。

> 跨仓边界提醒（承接 u6）：`HcommBatchModeEnd/Start`、`HcommThreadJoin`、channel 里的 `remoteCclMem`、`SendRecvBatchWrite`/`LocalReduce`/`LocalCopy` 等最终都经 `src/common/hcomm_dlsym/` 落到 HCOMM 基础通信层。template 是这些数据面原语的直接消费方，但它只看得到封装好的接口，不耦合 HCOMM 控制面内部。

#### 4.3.4 代码实践（源码阅读型 —— 本讲主实践）

> 本实践直接回应本讲规格里的实践任务。注意：规格原文写的是「说明它的 `REGISTER_TEMPLATE_V2` 注册名」，但据上面 4.2 的查证，**这个模板并没有 `REGISTER_TEMPLATE_V2` 注册名**——它走的是编译期绑定。下面按真实机制作答。

1. **实践目标**：为 `InsTempAllReduceMesh1DOneShot` 说清「它怎么被绑定的」+「`CalcRes` 算了什么」+「`KernelRun` 做了什么」。
2. **操作步骤与作答**：
   - **绑定方式 / 名字**：它**没有** `REGISTER_TEMPLATE_V2` 注册名。它作为类型实参在 [ins_v2_all_reduce_sole_executor.cc:271-273](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc#L271-L273) 被 `REGISTER_EXEC_V2` 编译期绑进 `InsV2AllReduceSoleExecutor<TopoMatch1D, InsTempAllReduceMesh1DOneShot>`，对应的 algName 是 `AicpuAllReduceSoleMeshOneShot`（这是 **executor 注册表**的键，不是 template 字符串注册表的键）。
   - **`CalcRes`（算 channel/thread/mem）做了什么**：见 [ins_temp_all_reduce_mesh_1D_one_shot.cc:23-38](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.cc#L23-L38) 与 [L40-46](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.cc#L40-L46)。它产出三类资源：① **thread**：`slaveThreadNum = templateRankSize_ - 1`（主线程复用用户 stream）；② **notify**：每个 slave thread 1 个，主线程 `templateRankSize_ - 1` 个；③ **channel**：`CalcChannelRequestMesh1D` 算出 Layer0 上每对 rank 的 1D Mesh 通道；④ **mem**：`CalcScratchMultiple` 返回 `templateRankSize_`，告诉 executor 每 rank 需 N 份 cclBuff 槽位。
   - **`KernelRun`（下发数据搬移）做了什么**：见 [L67-101](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.cc#L67-L101)。它三步走：① 主线程 `LocalCopy` 把自己数据放进输出；② N−1 个 slave thread 各对接一个对端，经 `SendRecvBatchWrite` 把自己数据写到对端 cclBuff 槽位（one-shot 并发）；③ `PostLocalReduce` 把收到的各对端数据 `LocalReduce` 归约进输出。若数据类型是 INT64/UINT64/FP64 或归约算子是 PROD，第③步前会插 `BatchModeEnd/Join/Start` 屏障切到 AICPU 归约核。
3. **需要观察的现象**：把上述三段代码连读，应能看到「资源需求（host）」与「数据搬移（device）」严格分离——`CalcRes` 里没有任何数据搬移调用，`KernelRun` 里没有任何资源申请。
4. **预期结果**：你能向别人讲清「为什么 one-shot Mesh 的 thread 数是 N−1」「为什么 scratch 倍数是 N」「三步搬移分别落在哪几个函数」。
5. 运行行为属「待本地验证」（本实践为源码阅读；若要真跑，需 NPU 环境 + 关闭对自编 AICPU 包的验签，见 u1-l4）。

#### 4.3.5 小练习与答案

**练习 1**：one-shot 模板的 `CalcSlice` 为什么给每个 rank 切「等大完整块」，而不是把数据均分成 N 份？

**参考答案**：因为 one-shot 的语义是「每个 rank 把自己的**整份**数据并发推给所有对端」，每个对端要在自己的 cclBuff 里为每个 rank 留一个完整槽位。所以切片描述的是「第 rankIdx 个 rank 的数据放在 cclBuff 的哪个 offset」，offset = rankIdx × size，每块都是完整大小。均分是 ReduceScatter 的做法，不是 one-shot AllReduce 的。

**练习 2**：`needAicpuReduce_` 在什么条件下为真？为什么为真时要在归约前插 `BatchModeEnd/Join/Start`？

**参考答案**：当 `dataType_` 是 INT64/UINT64/FP64，或 `reduceType` 是 PROD 时为真。这些类型/算子的归约要走 AICPU 归约核（普通 vector 归约不支持）。插 `BatchModeEnd` + `HcommThreadJoin(所有 thread)` + `BatchModeStart` 是一道**同步屏障**：先确保前面所有 slave thread 的收发任务都执行完（数据都已落到 cclBuff），再切换 batch 模式启动 AICPU 归约，避免归约读到未就绪的数据。

**练习 3**：数据量很大时，为什么 selector 会避开 one-shot 而选 two-shot 或 NHR？

**参考答案**：one-shot 要占用 N−1 个 slave thread、N 份 cclBuff 槽位，且所有收发并发抢占链路带宽。N 大、数据大时，线程数与 scratch 显存压力、链路拥塞都会使其劣化。two-shot 用两轮流水降低并发度，NHR（Non-stationary Hierarchical Ring）则结合 ReduceScatter + AllGather 降低单步数据量与中间显存占用（见 u3-l2、u1-l2 的 α-β 模型与分级通信）。

## 5. 综合实践

把本讲三块知识串起来，完成一个「端到端跟踪一个 algName 到模板搬数据」的任务：

1. 选定 algName = `AicpuAllReduceSoleMeshOneShot`。
2. **跟踪绑定**（用 4.2 的方法）：在 [ins_v2_all_reduce_sole_executor.cc:271-273](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc#L271-L273) 确认它绑到 `InsV2AllReduceSoleExecutor<TopoMatch1D, InsTempAllReduceMesh1DOneShot>`，并解释为什么这里查不到 `REGISTER_TEMPLATE_V2`。
3. **跟踪 host 阶段**：从 executor 的 `CalcRes`（[L49-60](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc#L49-L60)）进入模板的 `CalcRes`（[mesh_1D_one_shot.cc:23-38](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.cc#L23-L38)），列出它产出的 thread/notify/channel/mem 需求，并说明这些需求会被资源管理器如何满足（承接 u3-l4）。
4. **跟踪 device 阶段**：从 executor 的 `OrchestrateLoop`（[L160-188](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc#L160-L188)）进入模板的 `KernelRun`，画出「LocalCopy → SendRecvBatchWrite(×N−1) → PostLocalReduce」的时序，并标注每一步用的是哪个 thread、哪条 channel、落在 `userOut` 还是 `cclBuff`。
5. **产出**：一张端到端时序图 + 一份「资源需求清单」。要求图上能回答：algName 如何定位到模板？host 阶段算了哪些资源？device 阶段数据在三块缓冲（userIn / userOut / cclBuff）之间如何流动？

> 这是源码阅读型综合实践，无需运行；若要在真机验证日志，可在 `KernelRun` 入口（[L77](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/template/aicpu/ins_temp_all_reduce_mesh_1D_one_shot.cc#L77)）已有的 `HCCL_INFO` 基础上观察 `templateRankSize_`、`threadNum_` 与 loop 轮数的关系，属「待本地验证」。

## 6. 本讲小结

- **template 是三级链路的最后一环**，按「算法 × 引擎」的具体组合下发数据搬移；它是离硬件最近的软件抽象。
- **两层抽象**：`CommonAlgTemplateBase` 定契约（全纯虚），`InsAlgTemplateBase` 补公共成员并给部分接口「默认报错」实现，强制具体模板显式重写自己用到的接口。
- **两阶段生命周期**：host 阶段 `CalcRes`/`CalcScratchMultiple` 算资源（thread/notify/channel/mem），属控制面、不搬数据；device 阶段 `KernelRun` 真正搬数据，属数据面。
- **两条绑定路径并存**：`InsAlgTemplateRegistry` + `REGISTER_TEMPLATE_V2` 是**运行期字符串**查表（DPU 路径用）；`REGISTER_EXEC_V2` 把 template 作**编译期类型参数**烘进 executor（AICPU/AIV/CCU 主路径用）。本讲主角 `InsTempAllReduceMesh1DOneShot` 走的是后者，algName 为 `AicpuAllReduceSoleMeshOneShot`，**没有** `REGISTER_TEMPLATE_V2` 注册名。
- **one-shot Mesh AllReduce 的三步搬移**：`LocalCopy`（自己数据进输出）→ `SendRecvBatchWrite`（N−1 个 slave thread 并发把数据写到各对端 cclBuff）→ `PostLocalReduce`（本地把各对端数据归约进输出）。
- **资源量与算法形态挂钩**：one-shot 的 thread/notify 数都是 N−1、scratch 倍数是 N；这正是它只适合小数据低延迟、大数据要退到 two-shot/NHR 的根因。

## 7. 下一步学习建议

- **向「下」深入引擎内核**：本讲的 `KernelRun` 调到的 `SendRecvBatchWrite / LocalReduce / LocalCopy` 以及 `load_kernel / kernel_launch` 是 AICPU 引擎把任务真正下发到硬件的细节，这是 Unit 5（u5-l1 AICPU 模板与 Kernel 下发）的主题。建议接着读 `src/ops/op_common/template/aicpu/load_kernel.*` 与 `kernel_launch.*`。
- **横向对比另两个引擎的模板**：同样一个 Mesh one-shot AllReduce，AIV 引擎下是 `AivTempAllReduceMesh1DOneShot`、CCU 引擎下是 `CcuTempAllReduceMesh1DOneShot`（均见 [ins_v2_all_reduce_sole_executor.cc:291-322](https://github.com/gitcode.com/cann/hccl/blob/757867153ef03d005ec8752e6cb8f802cecd1e0a/src/ops/all_reduce/executor/ins_v2_all_reduce_sole_executor.cc#L291-L322) 的注册）。对比三者 `KernelRun` 的差异，能直观体会「算法 × 引擎」二维表。这承接 Unit 5 的 u5-l3（AIV）与 u5-l4（CCU）。
- **向「外」接 HCOMM**：本讲多次出现的 `HcommBatchMode*`、`remoteCclMem`、channel 等都经 dlsym 落到 HCOMM。若想看清 template 如何消费控制面/数据面原语，进入 Unit 6（u6-l1 dlsym 机制、u6-l2 dlsym 封装、u6-l3 控制面/数据面分离）。
- **动手扩展**：若要新增一个算法变体，按 u3-l1 的「algName 字符串契约」须在两端同步——selector 产出端（u3-l2）写新 algName，executor 注册端（本讲 4.2）用 `REGISTER_EXEC_V2` 绑定新 template 类，并实现 `CalcRes` 与 `KernelRun`。这正是 u7-l3（experimental 实验性贡献）给出的合规扩展路径。
