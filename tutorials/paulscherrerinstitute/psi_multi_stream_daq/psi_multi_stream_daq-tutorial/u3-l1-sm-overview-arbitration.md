# 控制状态机 psi_ms_daq_daq_sm：总览与优先级仲裁

## 1. 本讲目标

学完本讲后，读者应该能够：

- 说清楚控制状态机 `psi_ms_daq_daq_sm` 在整个 IP 核里扮演的"大脑"角色，以及它和输入逻辑、DMA 引擎、上下文存储、中断之间的关系。
- 读懂状态机实体的 `generic`（尤其是 `StreamPrio_g`、`MinBurstSize_g`、`MaxBurstSize_g`）和端口分组。
- 解释一条流被判定为"数据可用"需要满足哪些条件，以及 `InpDataAvail`、`DataAvailArbIn`、`DataPending` 三个向量各自的作用和差异。
- 描述三级优先级（prio 1/2/3）仲裁的执行顺序，以及同一优先级内部的轮询机制。
- 理解 `GetBitsOfStreamPrio` 与 `GetStreamNrFromGrant` 这对辅助函数如何把"按流号编址"和"按优先级编址"两套坐标系互相转换。

本讲**只讲总览和仲裁**，刻意不展开上下文读取、DMA 命令计算、窗口切换、上下文回写等内部状态——它们分别是 u3-l2、u3-l3 的主题。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自前置讲义）：

- **数据通路全貌**（u1-l3、u2-l1）：数据走"流输入 → 输入逻辑 → DMA 引擎 → AXI Master → DDR 内存"；本讲的状态机正是这条路之上的调度者。
- **公共类型包**（u2-l1）：你需要知道 `DaqSm2DaqDma_Cmd_t`（状态机发给 DMA 的命令，含 `Address`、`MaxSize`、`Stream`）、`DaqDma2DaqSm_Resp_t`（DMA 回送给状态机的响应，含 `Size`、`Trigger`、`Stream`），以及上下文访问记录 `ToCtxStr_t` / `ToCtxWin_t` / `FromCtx_t`。
- **输入逻辑输出**（u2-l2、u2-l4）：输入逻辑向状态机汇报两个关键信号——`Inp_Level`（FIFO 里攒了多少内部数据字）和 `Inp_HasLast`（这一路是否已经凑齐至少一个完整帧末尾 TLAST）。

补充两个本讲要用到的 VHDL 概念：

- **两进程法（two-process method）**：把时序逻辑写成 `p_comb`（纯组合，算出下一拍状态 `r_next`）和 `p_seq`（在时钟上升沿把 `r_next` 打入寄存器 `r`）。本讲义的状态机就是这种写法，所以"某信号在某一拍如何变化"要看 `p_comb` 里对 `v.xxx`（即 `r_next.xxx`）的赋值。
- **数组型 generic（`t_ainteger`）**：来自 `psi_common_array_pkg`，是一个"编译期整数数组"，可以用 `(1, 2, 3, 1)` 这样的写法给每一路流单独配一个值。`count(arr, val)` 是配套函数，统计数组里等于 `val` 的元素个数。

## 3. 本讲源码地图

本讲只涉及一个源文件，但它非常大（700 多行），所以先给一张"地图"：

| 文件 | 作用 |
| --- | --- |
| [hdl/psi_ms_daq_daq_sm.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd) | 控制状态机实体，整个 IP 的调度中枢。本讲聚焦其中的实体声明、仲裁相关信号与 `Idle_s`/`CheckPrio*_s`/`TlastCheck_s`/`CheckResp_s` 状态。 |

测试平台方面，有一个专门验证优先级的用例，本讲的代码实践会用到它：

| 文件 | 作用 |
| --- | --- |
| [tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_priorities.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_priorities.vhd) | `priorities` 测试用例，构造多路不同优先级的并发数据，校验状态机的服务顺序。 |

## 4. 核心概念与源码讲解

### 4.1 控制状态机的角色：整个 IP 的"大脑"

#### 4.1.1 概念说明

在已经学过的数据通路里，每一级都只管自己的一段：输入逻辑负责把样本拼成内部字、DMA 引擎负责把内部字搬到内存。那么**谁来决定"现在该服务哪一路流、把哪一段数据搬到内存的哪个地址"**？答案就是控制状态机 `psi_ms_daq_daq_sm`。

它不搬运数据本身，而是统筹四件事：

1. **看输入**：每一路流的 FIFO 里攒了多少数据（`Inp_Level`）、有没有完整帧（`Inp_HasLast`）、有没有时间戳（`Ts_*`）。
2. **算命令**：根据上下文（窗口起止地址、当前写指针等）算出下一次 DMA 的地址和最大长度，发给 DMA 引擎（`Dma_Cmd`）。
3. **收响应**：接收 DMA 引擎回送的传输完成响应（`Dma_Resp`），更新写指针、切换/回绕窗口、把新状态写回上下文存储。
4. **发中断**：每完成一个窗口，把"流号 + 窗口号"写入 IRQ FIFO，配合内存接口的 `TfDone` 脉冲生成 `StrIrq`。

DMA 引擎、AXI 主接口、内存本身都**不认识"窗口"和"优先级"**——这些概念只活在状态机里。状态机把高层语义翻译成一串"地址 + 大小"的 DMA 命令，下层只管执行。

#### 4.1.2 核心流程

状态机一共有 14 个状态（见 [psi_ms_daq_daq_sm.vhd:144](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L144)）。本讲只关心其中和"调度决策"相关的 6 个，其余属于上下文读写和响应处理，留给 u3-l2、u3-l3：

```text
              ┌──────────────────────────┐
              │  Idle_s                  │  ← 等待：IRQ FIFO 有空间、仲裁延时到
              │  （只有这里放行才往下走） │
              └────────────┬─────────────┘
                           ▼
              ┌──────────────────────────┐
              │  CheckPrio1_s            │  ← 有 prio1 数据？ → ReadCtxStr（服务）
              │  （无 grant 但有 pending │     否则 → CheckResp 或 CheckPrio2
              │     则去 CheckResp 等响应）│
              └────────────┬─────────────┘
                           ▼ (prio1 无数据)
              ┌──────────────────────────┐
              │  CheckPrio2_s            │  ← 同上，prio2
              └────────────┬─────────────┘
                           ▼ (prio2 无数据)
              ┌──────────────────────────┐
              │  CheckPrio3_s            │  ← prio3
              └────────────┬─────────────┘
                           ▼ (prio3 也无 grant)
              ┌──────────────────────────┐
              │  TlastCheck_s            │  ← 找一个"帧末尾待冲刷"的流来服务
              └────────────┬─────────────┘
                           ▼
              ┌──────────────────────────┐
              │  CheckResp_s             │  ← 有 DMA 响应就处理，否则回 Idle
              └──────────────────────────┘
```

一句话总结调度策略：**prio1 永远优先；prio1 没活儿才看 prio2；prio2 没活儿才看 prio3；都没有就去检查有没有"帧已结束但残余数据还没搬"的流；最后再看看有没有 DMA 响应要处理。**

#### 4.1.3 源码精读

状态机进程的敏感信号列表，能让你一眼看出它"听谁的指挥"：

[psi_ms_daq_daq_sm.vhd:212](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L212) —— `p_comb` 的敏感列表里同时挂着输入侧（`Inp_HasLast`、`Inp_Level`、`Ts_*`）、DMA 侧（`Dma_Resp`、`Dma_Resp_Vld`）、上下文侧（`CtxStr_Resp`、`CtxWin_Resp`）、控制位（`GlbEna`、`StrEna`）、内存完成脉冲（`TfDone`）以及仲裁器输出（`GrantVld`、`GrantPrio*`）和 IRQ FIFO 状态。这正是"大脑"的四路输入。

`Idle_s` 是唯一的总闸门：

[psi_ms_daq_daq_sm.vhd:263-L277](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L263-L277) —— 只有当 `IrqFifoAlmFull = '0'`（IRQ FIFO 没接近满，能确保之后的响应有地方放）时，才放行进入 `CheckPrio1_s`。后面那段 `ArbDelCnt` 计数是**仿真专用**的仲裁延时（0 数到 4），目的是让 testbench 有时间在两次仲裁之间改变激励；注释明确写了"Delay arbitration in simulation to allow TB to react"。

#### 4.1.4 代码实践

- **实践目标**：在不读后续章节的前提下，仅凭本讲的"调度策略"一句话，预测状态机在不同输入下的走向。
- **操作步骤**：
  1. 打开 [hdl/psi_ms_daq_daq_sm.vhd:260-L335](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L260-L335)，对照上面的流程图，逐个状态读 `case r.State is` 分支。
  2. 用纸笔跟踪一种情况：`IrqFifoAlmFull = 1`（IRQ FIFO 接近满）。问自己：状态机停在哪里？为什么？
- **预期现象**：状态会一直卡在 `Idle_s`，`CheckPrio*_s` 不会被执行，因此不会发出新的 DMA 命令。
- **预期结果**：这印证了 IRQ FIFO 的反压是从**源头**控制状态机——避免它发出来不及收尾的命令。完整的中断反压机制见 u4-l1。
- **运行结果**：待本地验证（需 PsiSim 仿真环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Idle_s` 要先检查 `IrqFifoAlmFull` 才往下走，而不是等到真正需要写 IRQ FIFO 时再检查？

**参考答案**：因为一次 DMA 命令从发起到完成、到产生窗口结束事件，中间隔着上下文读取、数据搬运、响应处理等多个状态和多个时钟。如果只在最后写 IRQ FIFO 时才检查，一旦那时发现 FIFO 满，命令已经发出、数据已经搬了，就来不及反压。所以状态机选择在**发起命令之前**就确认 IRQ FIFO 有余量（`IrqFifoAlmFull = '0'`），从源头保证"这条命令的响应一定有处可放"。

**练习 2**：`ArbDelCnt` 这段延时在真实硬件里是必要的吗？

**参考答案**：不是。注释写明它是"Delay arbitration in simulation to allow TB to react"。真实硬件里它只是白白多等几拍；它的存在是为了让 testbench 在状态机两次仲裁之间有时间更新 `Inp_Level` 等激励，方便验证。这是仿真/验证友好性设计，不是功能必需。

---

### 4.2 实体接口与关键 generic

#### 4.2.1 概念说明

要看懂仲裁，先得看清状态机的"配置项"和"对外接头"。配置项里有三个和调度强相关：

- **`StreamPrio_g`**：数组型 generic，给每一路流分配一个优先级（1、2 或 3）。**数字越小优先级越高**。默认值 `(1, 2, 3, 1)` 就是本讲代码实践要分析的场景。
- **`MinBurstSize_g`**：一次 DMA 突发的"最低门槛"。只有当某路流的 FIFO 数据量达到这个值，它才会被判为"数据可用"。
- **`MaxBurstSize_g`**：一次 DMA 突发的最大长度上限（和 `MinBurstSize_g` 一起夹出突发长度范围）。

#### 4.2.2 核心流程

端口按功能可以分成五组，对应状态机的五类对话对象：

| 端口组 | 代表信号 | 对话对象 |
| --- | --- | --- |
| 控制 | `GlbEna`、`StrEna`、`StrIrq`、`StrLastWin` | 全局/单流使能，回送中断 |
| 输入逻辑 | `Inp_HasLast`、`Inp_Level`、`Ts_*` | 每路流的 FIFO 水位、帧末尾、时间戳 |
| DMA | `Dma_Cmd*`、`Dma_Resp*` | 向 DMA 引擎发命令、收响应 |
| 内存 | `TfDone` | AXI 主接口回送的"一次传输真正完成"脉冲 |
| 上下文 RAM | `CtxStr_*`、`CtxWin_*` | 读/写流上下文与窗口上下文存储 |

#### 4.2.3 源码精读

实体声明与 generic：

[psi_ms_daq_daq_sm.vhd:31-L39](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L31-L39) —— 注意 `StreamPrio_g : t_ainteger := (1, 2, 3, 1)`，这就是默认的"4 流、优先级 1/2/3/1"配置；`MinBurstSize_g` 和 `MaxBurstSize_g` 默认都是 512。

实体顶部的元注解（给 PsiSim 用的测试描述）：

[psi_ms_daq_daq_sm.vhd:28-L30](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L28-L30) —— `$$ testcases=...priorities... $$` 列出了所有测试用例，其中 `priorities` 就是本讲代码实践要读的那个；`$$ processes=control,dma_cmd,dma_resp,ctx $$` 说明状态机内部按功能分了四个进程域（虽然实际只用一个时钟）。

端口列表：

[psi_ms_daq_daq_sm.vhd:40-L70](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L40-L70) —— 对照上面的五组表格读，可以看到每路流相关的端口（`Inp_HasLast`、`Inp_Level`、`Ts_*`）都是长度为 `Streams_g` 的数组，状态机在内部循环处理。

#### 4.2.4 代码实践

- **实践目标**：确认你对 `MinBurstSize_g` 单位的理解。
- **操作步骤**：
  1. 读 [psi_ms_daq_daq_sm.vhd:246-L253](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L246-L253)，看 `Inp_Level(str)` 和 `MinBurstSize_g` 是直接比较的，说明单位一致。
  2. 再读 [psi_ms_daq_daq_sm.vhd:428-L432](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L428-L432)，看 `MaxBurstSize_g` 是如何换算成发给 DMA 的字节数的（注释 `8 bytes per 64-bit QWORD`）。
- **预期现象**：`MaxSize` 被赋值为 `MaxBurstSize_g * 8`，即 `MaxBurstSize_g` 的单位是"64 位字（QWORD）"，乘以 8 才得到字节。
- **预期结果**：据此推断 `Inp_Level` 和 `MinBurstSize_g` 的单位也都是**内部数据字**（`IntDataWidth` 位，典型 64 位即 QWORD），而不是字节。默认 `MinBurstSize_g = 512` 意味着一路流要攒够 512 个 QWORD（4096 字节）才会被判为"数据可用"。
- **运行结果**：单位推断有源码注释支撑；若要在波形上确认，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `MinBurstSize_g` 调大到 1024（`MaxBurstSize_g` 仍是 512），会发生什么？

**参考答案**：`Inp_Level(str) >= MinBurstSize_g` 这个条件更难满足——一路流要攒够 1024 个 QWORD 才"数据可用"。但单次 DMA 突发仍受 `MaxBurstSize_g = 512` 限制，所以一次只能搬 512，搬完后该路 FIFO 还剩 ≥512，下一轮仲裁它依然"数据可用"，会继续被服务。效果是：**突发更密集地连续服务同一路**，低优先级流更难插队。注意：`MinBurstSize_g` 不应大于 `MaxBurstSize_g` 之外的不合理设置需结合实际，本练习只讨论趋势。

**练习 2**：`StreamPrio_g` 的取值为什么限定在 1/2/3？

**参考答案**：因为状态机里**硬编码了三个优先级档**——对应 `CheckPrio1_s`/`CheckPrio2_s`/`CheckPrio3_s` 三个状态和三个仲裁器实例（见 4.4）。优先级不是任意整数，而是一个三档枚举；状态机用 `count(StreamPrio_g, Prio)` 统计每一档里有几路流。如果你给某路填了 4，没有任何状态或仲裁器会处理它，这路流实际上永远得不到服务。

---

### 4.3 数据可用性判定：InpDataAvail / DataAvailArbIn / DataPending

#### 4.3.1 概念说明

仲裁器要决定服务谁，首先得知道"谁有数据可服务"。状态机用三个长度为 `Streams_g` 的向量来描述这件事，它们一层比一层严格：

- **`InpDataAvail`**：最朴素的"有没有数据"。一路流满足"使能 + 数据量够"即为 1。
- **`DataAvailArbIn`**：真正喂给仲裁器的请求。在 `InpDataAvail` 基础上，再屏蔽掉两类流——已经有命令在飞（`OpenCommand`）的、当前窗口被保护（`WinProtected`）的。
- **`DataPending`**：一个辅助判定，只屏蔽 `WinProtected`，**不屏蔽** `OpenCommand`。用来回答"虽然现在不能立刻发命令，但要不要为了它等一等响应"。

理解这三者的差异是理解整个调度公平性的钥匙。

#### 4.3.2 核心流程

三者的逻辑关系（`:=` 表示在 `p_comb` 里对 `v.xxx` 即下一拍值的赋值）：

```text
InpDataAvail(str)   = (Inp_Level(str) >= MinBurstSize_g) AND StrEnaReg(str) AND GlbEnaReg

DataAvailArbIn      = InpDataAvail AND (NOT OpenCommand) AND (NOT WinProtected)
                       └→ 喂给三个仲裁器作为 req_i

DataPending         = InpDataAvail AND (NOT WinProtected)
                       └→ 用来在 CheckPrio*_s 里决定"等响应还是降级到下一优先级"
```

设计要点：

- **`OpenCommand` 的两副面孔**：它在 `DataAvailArbIn` 里被屏蔽（防止对同一路流同时发两条命令），但在 `DataPending` 里**不**被屏蔽。这意味着：当一路高优先级流有数据但命令还在飞，状态机**不会**立即把通道让给低优先级，而是先去 `CheckResp_s` 等响应——响应一到、`OpenCommand` 清零，高优先级流立刻又能被服务。
- **`WinProtected` 一视同仁**：它在两个向量里都被屏蔽。因为"窗口被保护"意味着在等软件释放窗口，等响应也解决不了，所以这种情况**会**把通道让给低优先级流。

#### 4.3.3 源码精读

`InpDataAvail` 的逐流计算：

[psi_ms_daq_daq_sm.vhd:247-L253](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L247-L253) —— 对每一路流，只有 `unsigned(Inp_Level(str)) >= MinBurstSize_g` **且** 该流使能（`StrEnaReg`）**且** 全局使能（`GlbEnaReg`）时，`InpDataAvail(str)` 才为 1。注意这里用的是寄存过的 `StrEnaReg`/`GlbEnaReg`（已经同步过一拍），不是原始输入。

两个屏蔽向量的计算：

[psi_ms_daq_daq_sm.vhd:254-L255](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L254-L255) —— `DataAvailArbIn` 屏蔽 `OpenCommand` 和 `WinProtected`；`DataPending` 只屏蔽 `WinProtected`。`DataPending` 行末的注释 `Do not prevent lower priority channels from access if the window of a higher priority stream is protected` 直白点出了设计意图。

这两个向量如何影响状态跳转，看 `CheckPrio1_s`：

[psi_ms_daq_daq_sm.vhd:280-L291](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L280-L291) —— 三分支：① 有 prio1 仲裁结果（`GrantVldReg(1)=1`）→ 立即服务；② 没有 grant 但 prio1 的 `DataPending` 非零 → 去 `CheckResp_s` 等响应；③ 否则降级到 `CheckPrio2_s`。`CheckPrio2_s`、`CheckPrio3_s`（[L293-L314](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L293-L314)）结构完全一样。

`TlastCheck_s` 是兜底，专门服务"数据量没到 `MinBurstSize_g`、但帧已经结束（`HasLast`）必须冲刷"的流：

[psi_ms_daq_daq_sm.vhd:316-L324](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L316-L324) —— 它遍历所有流，找第一个满足 `HasLast AND NOT OpenCommand AND NOT WinProtected` 的流来服务。这一步和优先级无关——帧末尾的残余数据必须搬走，否则会卡住后续触发。

#### 4.3.4 代码实践

- **实践目标**：体会 `OpenCommand` 在 `DataAvailArbIn` 和 `DataPending` 中的不同待遇对调度公平性的影响。
- **操作步骤**：
  1. 假设场景：stream 0（prio1）和 stream 1（prio2）都"数据可用"，且 stream 0 刚发出一条 DMA 命令（`OpenCommand(0)=1`）。
  2. 跟踪 [psi_ms_daq_daq_sm.vhd:254-L255](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L254-L255)：此时 `DataAvailArbIn(0)=0`、`DataPending(0)=1`。
  3. 再跟踪 [psi_ms_daq_daq_sm.vhd:280-L291](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L280-L291)：`GrantVldReg(1)=0`（因为请求被屏蔽了），但 `DataPending` 的 prio1 位非零 → 走第二个分支 → `CheckResp_s`。
- **预期现象**：状态机**没有**掉到 `CheckPrio2_s` 去 service stream 1，而是去 `CheckResp_s` 等 stream 0 的 DMA 响应。
- **预期结果**：一旦响应回来、`OpenCommand(0)` 清零，下一轮仲裁 stream 0 又能立刻被服务。结论：**高优先级流的"命令在飞"状态不会让它丧失优先级**，从而保证高优先级流能连续占据通道。若改成"命令在飞就降级"，高优先级流会被低优先级流频繁打断。
- **运行结果**：待本地验证（可在 `priorities` 用例的波形上观察 `OpenCommand` 与状态跳转的关系）。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `DataPending` 的定义改成和 `DataAvailArbIn` 完全一样（也屏蔽 `OpenCommand`），调度行为会有什么变化？

**参考答案**：那么当高优先级流命令在飞时，`CheckPrio*_s` 的第二个分支不再成立，状态机会直接降级到下一优先级，把通道让给低优先级流。结果是高优先级流无法连续独占通道——它的命令在飞期间，低优先级流会插队服务。这会损害高优先级流的实时性，所以原设计刻意让 `DataPending` 不屏蔽 `OpenCommand`。

**练习 2**：为什么 `WinProtected` 在 `DataPending` 里也要屏蔽？

**参考答案**：`WinProtected=1` 表示该流的当前窗口还没被软件释放（`winOverwrite=false` 场景下的保护，详见 u4-l5），等 DMA 响应并不能让窗口变成可用——必须等软件 `MarkAsFree`。所以"等响应"是白等，不如直接降级让低优先级流工作。这正是行末注释"不要因为高优先级流的窗口被保护，就阻塞低优先级通道"的含义。

---

### 4.4 三级优先级仲裁器与 GrantVld

#### 4.4.1 概念说明

知道了"谁有数据"之后，还要在"同优先级的多路流"里选一路。状态机用了**三个独立的仲裁器实例**，分别对应 prio1、prio2、prio3：

- 每个仲裁器只接收属于自己优先级档的那几路流的请求。
- 仲裁器输出一个 one-hot 的 `GrantPrio*` 向量，表示"这一档里轮到谁了"。
- `GrantVld(p)` 是把 one-hot 向量压成一个有效位：只要这一档有任何 grant，`GrantVld(p)=1`。

> **关于仲裁算法的诚实说明**：源码在三个实例上方都写了注释 `Round Robin Arbiter`（[L639](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L639)、[L653](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L653)、[L667](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L667)），说明同一优先级内部是**轮询（round-robin）**。但 `psi_common_arb_priority` 的具体实现位于 `psi_common` 库，本仓库不含（由 `dependencies.py` 在构建时拉取，见 u1-l2），因此其内部实现细节**待确认**。本讲只依赖源码内联注释来描述行为。

#### 4.4.2 核心流程

仲裁器的连接关系：

```text
                       DataAvailArbIn (按流号编址，长度 Streams_g)
                              │
                  GetBitsOfStreamPrio(., 1)   GetBitsOfStreamPrio(., 2)   GetBitsOfStreamPrio(., 3)
                              │                              │                              │
                       AvailPrio1 (按档内序号)        AvailPrio2                       AvailPrio3
                              │                              │                              │
                       ┌──────▼──────┐               ┌──────▼──────┐               ┌──────▼──────┐
                       │ arb (prio1) │               │ arb (prio2) │               │ arb (prio3) │
                       └──────┬──────┘               └──────┬──────┘               └──────┬──────┘
                              │                              │                              │
                       GrantPrio1                     GrantPrio2                     GrantPrio3
                              │                              │                              │
                       GrantVld(1)                    GrantVld(2)                    GrantVld(3)
```

`CheckPrio*_s` 按 1→2→3 的顺序扫描这三个 `GrantVld`，谁先亮就服务谁，从而实现**档间严格优先级、档内轮询**。

#### 4.4.3 源码精读

三个仲裁器实例（结构完全相同，只是档位不同）：

[psi_ms_daq_daq_sm.vhd:639-L679](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L639-L679) —— 每个实例的 `width_g` 都是 `count(StreamPrio_g, Prio)`（该档里有几路流），`req_i` 是用 `GetBitsOfStreamPrio(r.DataAvailArbIn, Prio)` 从"按流号编址"抽出"按档内序号编址"的请求。

`GrantVld` 的压缩逻辑：

[psi_ms_daq_daq_sm.vhd:651](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L651)、[L665](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L665)、[L679](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L679) —— `GrantVld(p) <= '1' when (unsigned(GrantPrio*) /= 0) and (GrantPrio*'length > 0) else '0'`。后半段 `GrantPrio*'length > 0` 是给 Vivado 的空范围保护：如果某一档没有任何流（`count=0`），`GrantPrio*` 是 null-range，`unsigned(null)` 在 Vivado 里不保证是 0，所以显式判长度。

#### 4.4.4 代码实践

- **实践目标**：在默认配置 `(1, 2, 3, 1)` 下，算出每个仲裁器的位宽。
- **操作步骤**：
  1. 读 [psi_ms_daq_daq_sm.vhd:639-L679](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L639-L679)，每个 `width_g => count(StreamPrio_g, Prio)`。
  2. 对默认 `StreamPrio_g = (1, 2, 3, 1)` 手算：`count(., 1) = 2`（stream 0 和 3）、`count(., 2) = 1`（stream 1）、`count(., 3) = 1`（stream 2）。
- **预期现象**：prio1 仲裁器位宽 2，prio2、prio3 仲裁器位宽各 1。
- **预期结果**：对应 [psi_ms_daq_daq_sm.vhd:128-L133](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L128-L133) 的信号声明 `AvailPrio1/GrantPrio1` 宽度由 `count(StreamPrio_g, 1)-1` 决定。
- **运行结果**：可直接由源码 `count(...)` 推断，无需仿真。

#### 4.4.5 小练习与答案

**练习 1**：如果某档（比如 prio2）在 `StreamPrio_g` 里没有流（`count=0`），状态机会出问题吗？

**参考答案**：不会。`GrantVld(2)` 的表达式里有 `GrantPrio2'length > 0` 保护，长度为 0 时 `GrantVld(2)` 恒为 0；`CheckPrio2_s` 里也有 `count(StreamPrio_g, 2) /= 0` 的与条件（注释说是为了规避 Vivado 的 `unsigned(null-range)` 问题）。所以缺某一档时，状态机会安全地跳过它。

**练习 2**：三个仲裁器是同时工作的，为什么不会冲突？

**参考答案**：因为它们各自只看自己档的请求位，输出的 `GrantPrio*` 也是各自独立的 one-hot 向量；最终由 `CheckPrio1_s → CheckPrio2_s → CheckPrio3_s` 的**顺序状态扫描**来决定到底用哪一档的 grant。三个仲裁器并行算结果，状态机串行做选择，两者解耦，不会冲突。

---

### 4.5 辅助函数 GetBitsOfStreamPrio / GetStreamNrFromGrant

#### 4.5.1 概念说明

仲裁器只认识"连续的请求位（0, 1, 2, ...）"，但流的编号可能不连续。比如默认配置里 prio1 档的流是 stream 0 和 stream 3——仲裁器看到的请求是 2 位，bit 0 对应 stream 0，bit 1 对应 stream 3。于是需要一对函数来做两套坐标系之间的转换：

- **`GetBitsOfStreamPrio(InputVector, Prio)`**：正向。输入一个"按流号编址"的向量（如 `DataAvailArbIn`），抽出属于 `Prio` 档的那几位，重新打包成"按档内序号编址"的向量，喂给仲裁器。
- **`GetStreamNrFromGrant(GrantVector, Prio)`**：反向。输入仲裁器输出的 one-hot `GrantVector`，返回被选中的那一档对应的真实流号（整数），供 `HndlStream` 使用。

#### 4.5.2 核心流程

以默认 `(1, 2, 3, 1)` 的 prio1 档（含 stream 0、stream 3）为例：

```text
流号编址:  DataAvailArbIn = [ s0, s1, s2, s3 ]   (s1/s2 不属于 prio1)
                                ↓ GetBitsOfStreamPrio(., 1)
档内编址:  AvailPrio1     = [ s0, s3 ]            (bit0←s0, bit1←s3)
                                ↓ 仲裁器
档内编址:  GrantPrio1     = [ 0,   1  ]           (假设轮到 s3)
                                ↓ GetStreamNrFromGrant(., 1)
流号结果:  HndlStream     = 3
```

两函数的对应关系是**严格互逆**的：`GetBitsOfStreamPrio` 按流号从低到高扫描，命中目标档就写到下一个档内位；`GetStreamNrFromGrant` 用同样的扫描顺序计数，命中 grant 位就返回当前流号。

#### 4.5.3 源码精读

`GetBitsOfStreamPrio`：

[psi_ms_daq_daq_sm.vhd:83-L96](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L83-L96) —— 返回向量长度由 `count(StreamPrio_g, Prio)` 决定；循环遍历 `InputVector` 的每一位，只要 `StreamPrio_g(idx) = Prio`，就把 `InputVector(idx)` 拷到结果向量的 `OutIdx_v` 位，然后 `OutIdx_v` 自增。结果：档内低位对应流号较小的那一路。

`GetStreamNrFromGrant`：

[psi_ms_daq_daq_sm.vhd:98-L112](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L98-L112) —— 同样按流号扫描，用 `IdxCnt_v` 计数命中目标档的次数；一旦 `GrantVector(IdxCnt_v)='1'`，立即返回当前流号 `idx`。注意：若 `GrantVector` 全 0（理论不该发生，因为 `CheckPrio*_s` 已经先用 `GrantVldReg` 判断过），函数返回 0，相当于一个安全兜底。

函数的实际调用点：

[psi_ms_daq_daq_sm.vhd:640](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L640)、[L654](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L654)、[L668](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L668) —— 三个 `AvailPrio*` 都由 `GetBitsOfStreamPrio(r.DataAvailArbIn, Prio)` 生成。

[psi_ms_daq_daq_sm.vhd:284](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L284)、[L297](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L297)、[L310](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L310) —— `CheckPrio*_s` 里用 `GetStreamNrFromGrant(r.GrantPrio*Reg, Prio)` 把 grant 还原成 `HndlStream`。

#### 4.5.4 代码实践

- **实践目标**：手动模拟一次完整的"请求→仲裁→还原"往返。
- **操作步骤**：
  1. 设 `StreamPrio_g = (1, 2, 3, 1)`，假设 `DataAvailArbIn = "1011"`（stream 0、1、3 可用，stream 2 不可用）。
  2. 手算 `GetBitsOfStreamPrio("1011", 1)`：prio1 档含 stream 0、stream 3，取 `DataAvailArbIn(0)=1`、`DataAvailArbIn(3)=1` → `AvailPrio1 = "11"`。
  3. 假设仲裁器输出 `GrantPrio1 = "10"`（轮到 bit1，即 stream 3）。
  4. 手算 `GetStreamNrFromGrant("10", 1)`：扫描时 IdxCnt 在 stream 0 处 =0（grant 位是 0，跳过），在 stream 3 处 IdxCnt=1（grant 位是 1）→ 返回 3。
- **预期现象**：往返结果 `HndlStream = 3`，正是被服务的流。
- **预期结果**：两函数互逆，坐标系转换无损。可以换 `DataAvailArbIn = "0011"` 再算一遍，验证当 stream 3 不可用时 `AvailPrio1="01"`、若 grant 落在 bit0 则返回 stream 0。
- **运行结果**：纯逻辑推导，无需仿真即可确认。

#### 4.5.5 小练习与答案

**练习 1**：`GetBitsOfStreamPrio` 结果向量的位序为什么是"档内低位对应较小流号"？

**参考答案**：因为函数体里 `OutIdx_v` 从 0 开始自增，而外层循环 `for idx in InputVector'low to InputVector'high` 是按流号从小到大扫描。所以最先命中目标档的（流号最小的）被写到 bit 0。这种确定性顺序保证了 `GetStreamNrFromGrant` 能用同样的扫描逻辑精确还原。

**练习 2**：如果 `StreamPrio_g` 改成 `(2, 1, 1, 3)`，prio1 档包含哪些流？`AvailPrio1` 的 bit0 对应谁？

**参考答案**：prio1 档包含 stream 1 和 stream 2（`StreamPrio_g(1)=1`、`StreamPrio_g(2)=1`）。按从小到大扫描，stream 1 先命中 → `AvailPrio1` 的 bit0 对应 stream 1，bit1 对应 stream 2。

---

## 5. 综合实践

把本讲的四个模块串起来，做一个完整的"调度顺序预测"练习。这正是本讲的代码实践任务。

**场景**：4 路流，使用默认 `StreamPrio_g = (1, 2, 3, 1)`，即：

| 流号 | 优先级 | 所属档 |
| --- | --- | --- |
| stream 0 | 1 | prio1 |
| stream 1 | 2 | prio2 |
| stream 2 | 3 | prio3 |
| stream 3 | 1 | prio1 |

设 `MinBurstSize_g = MaxBurstSize_g = 512`，全局使能、所有流使能，IRQ FIFO 未满。

**任务**：按下述步骤推导状态机的服务顺序，并验证 `MinBurstSize_g` 如何决定"数据可用"。

1. **情形 A——四路同时满突发**：四路的 `Inp_Level` 都 ≥ 512。
   - 第一轮：`CheckPrio1_s` 命中，prio1 档（stream 0、stream 3）轮询，假设轮到 stream 0 → 服务 stream 0，`OpenCommand(0)=1`。
   - 第二轮：`DataAvailArbIn(0)=0`（被 `OpenCommand` 屏蔽），但 `DataPending(0)=1` → `CheckPrio1_s` 走第二分支 → `CheckResp_s` 等响应；响应到、`OpenCommand(0)` 清零后，prio1 继续轮询到 stream 3 → 服务 stream 3。
   - 结论：**只要 prio1 两路持续有 ≥512 数据，stream 1（prio2）和 stream 2（prio3）会被饿死**，状态机一直在 prio1 两路之间轮询 + 等响应。

2. **情形 B——prio1 数据不足**：stream 0、stream 3 的 `Inp_Level` 都 < 512（比如各只有 100），stream 1、stream 2 都 ≥ 512。
   - `InpDataAvail(0)=InpDataAvail(3)=0`（卡在 `MinBurstSize_g` 门槛）→ `DataAvailArbIn` 的 prio1 部分全 0 → `CheckPrio1_s` 既无 grant 也无 pending → 降到 `CheckPrio2_s` → 服务 stream 1。
   - 结论：**`MinBurstSize_g` 是低优先级流获得服务的关键**。高优先级流数据量不达标时，机会就让给低优先级流，避免低优先级流被永久饿死。

3. **情形 C——帧末尾冲刷**：stream 2（prio3）的 `Inp_Level` 只有 50（< 512），但 `Inp_HasLast(2)=1`（帧已结束）。
   - 即使 stream 2 不满足 `MinBurstSize_g`，`TlastCheck_s`（[L316-L324](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L316-L324)）会发现它有 `HasLast` 且无在途命令、窗口未保护，从而绕过 `MinBurstSize_g` 门槛把它服务掉——残余数据必须冲刷。

**验证方式（源码阅读型 + 可选仿真）**：
- 打开 [tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_priorities.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_priorities.vhd)，对照它的 `StreamPrio_g` 配置和施加的流数据，看它校验的服务顺序是否与你的预测一致。
- 若有 PsiSim 环境（见 u1-l2、u5-l1），可运行该用例并在波形上观察 `r.State`、`r.HndlStream`、`OpenCommand`、`WinProtected`、`InpDataAvail`，验证情形 A/B/C 的跳转。
- **运行结果**：待本地验证。

> 作业提交点：把情形 A 中"stream 0 和 stream 3 交替服务、期间不服务 stream 1/2"的现象，用一张时序草图表示出来；并解释如果要让 stream 1（prio2）尽快得到服务，软件侧或配置侧可以做哪两件事（提示：① 让 stream 0/3 的数据源头慢下来，使其 `Inp_Level` 跌破 `MinBurstSize_g`；② 直接改 `StreamPrio_g` 把 stream 1 提到 prio1）。

## 6. 本讲小结

- `psi_ms_daq_daq_sm` 是整个 IP 的"大脑"：统筹输入水位、DMA 命令/响应、上下文读写和中断生成，把"窗口/优先级"等高层语义翻译成下层执行的一串 DMA 命令。
- 调度策略是**档间严格优先级、档内轮询**：`CheckPrio1_s → CheckPrio2_s → CheckPrio3_s`，由三个独立的 `psi_common_arb_priority` 实例各管一档。
- "数据可用"分三层：`InpDataAvail`（使能 + 数据量达标）→ `DataAvailArbIn`（再屏蔽 `OpenCommand` 和 `WinProtected`，喂给仲裁器）→ `DataPending`（只屏蔽 `WinProtected`，用来决定"等响应还是降级"）。
- `MinBurstSize_g` 是低优先级流获得服务的钥匙：高优先级流数据量不达标时，机会让给低优先级；`TlastCheck_s` 则绕过门槛专门冲刷帧末尾残余。
- `Idle_s` 用 `IrqFifoAlmFull` 做源头反压，保证发出的每条命令其响应都有处可放。
- `GetBitsOfStreamPrio` / `GetStreamNrFromGrant` 这对函数在"流号编址"和"档内序号编址"两套坐标系间无损互逆转换，使不连续的流号能接入连续位宽的仲裁器。

## 7. 下一步学习建议

本讲只覆盖了"调度决策"那一半状态。状态机算出"服务哪一路"之后，还要去读上下文、算 DMA 地址、收响应、切窗口、回写上下文。建议按以下顺序继续：

- **u3-l2 控制状态机：上下文读取与 DMA 命令计算**：讲 `ReadCtxStr_s`/`ReadCtxWin_s`/`First_s`/`CalcAccess0_s`/`CalcAccess1_s`，回答"怎么读出窗口信息、怎么算出下一次 DMA 的地址和 `MaxSize`"。
- **u3-l3 控制状态机：窗口切换、环形缓冲与上下文回写**：讲 `ProcResp0_s`/`NextWin_s`/`WriteCtx_s`，回答"DMA 完成后怎么推进指针、怎么切窗口或回绕、怎么把新状态写回上下文"。
- 若想看仲裁的多路并发效果，可先读 [psi_ms_daq_daq_sm_tb_case_priorities.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_sm/psi_ms_daq_daq_sm_tb_case_priorities.vhd)（仿真验证实践详见 u5-l1、u5-l2）。
- `OpenCommand` / `WinProtected` / `NewBuffer` 的完整协议（窗口保护与覆盖语义）留到 **u4-l5** 深入。
