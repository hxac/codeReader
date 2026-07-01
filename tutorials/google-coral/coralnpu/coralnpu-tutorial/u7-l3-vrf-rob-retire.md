# 向量寄存器堆、ROB 与退休

## 1. 本讲目标

本讲是「RVV 向量/矩阵后端」系列的第三篇，承接 u7-l2（RVV 译码与派发）。在 u7-l2 里，派发单元（dispatch）把向量微操作（uop）送进了执行单元。但派发之后、结果回到寄存器堆之前，还有三个关键模块在「兜底」：

学完本讲，你应当能够：

1. 说出 **向量寄存器堆（VRF）** 有多少个寄存器、多宽、几个读口/写口，以及它为何用「触发器（FF）」而不是 SRAM 实现。
2. 说清楚 **重排序缓冲（ROB）** 如何做到「**乱序执行、按序退休**」——程序顺序由谁保证、乱序完成如何被容纳、队首如何决定何时提交。
3. 解释 **退休（retire）** 阶段如何按序把结果写回 VRF/标量寄存器堆，如何处理同周期内多条指令写同一向量寄存器的 **WAW（写后写）** 冲突，以及它如何与 LSU、vcsr/vxsat 协作。
4. 看懂这三段真实 SystemVerilog 源码，并能跟踪一条「依赖前一条结果」的向量指令从等待到乱序执行、再到按序提交的完整旅程。

---

## 2. 前置知识

本讲假设你已经读过 u7-l1（RVV 后端总览）和 u7-l2（RVV 译码与派发）。下面补充几个本讲会用到的术语：

- **uop（micro-op，微操作）**：一条向量指令（如 `vadd`）在 RVV 后端会被拆成若干个 uop。uop 是派发、执行、退休的基本单位。
- **VRF（Vector Register File，向量寄存器堆）**：存放向量操作数的寄存器组，是 RVV 后端的数据「仓库」。
- **ROB（Re-Order Buffer，重排序缓冲）**：一个环形队列，按程序顺序记录所有已派发但尚未退休的 uop，是「乱序执行、按序提交」的核心。
- **in-order / out-of-order（按序 / 乱序）**：派发按程序顺序进入 ROB；执行单元（PU）谁先算完谁就先把结果写回 ROB（乱序）；但最终写回寄存器堆必须按程序顺序（按序退休）。
- **WAW（Write-After-Write，写后写）**：两条指令写同一个寄存器。在按序退休里，如果它们在同一周期退休，就要保证「新值覆盖旧值」。
- **ready-valid 握手**：上游给 `valid` 表示数据有效，下游给 `ready` 表示能接收，二者同周期都为真时数据被「fire」（成交）传递。
- **`edff` / `cdffr`**：本项目自定义的寄存器原语。`edff` 是「带使能的 D 触发器」（`.e` 使能、`.d` 输入、`.q` 输出），`cdffr` 是「带使能与同步清零的 D 触发器」。

> 一个关于「文档与实现差异」的提醒：`doc/overview.md` 里写「Vector (64) v0..v63，256 bits」，但本讲涉及的 RTL 实现与之**有两处不同**，我们会以源码为准——
> （a）向量寄存器数量实际是 **32 个**（`NUM_VRF = 32`，即标准 RISC-V V 扩展的 v0–v31）；
> （b）`VLEN`（每寄存器位宽）由编译宏选择，**实际构建（FPGA/UVM/VCS/cocotb/Chisel）全部选用 `VLEN_128`，即每寄存器 128 位**；设计宏本身支持 128/256/512/1024。读源码时请以这两点为准。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [hdl/verilog/rvv/design/rvv_backend_vrf.sv](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_vrf.sv) | VRF 顶层：把派发端读口、置换端读口、退休端写口汇集起来，做字节使能处理，调用底层寄存器阵列。 |
| [hdl/verilog/rvv/design/rvv_backend_vrf_reg.sv](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_vrf_reg.sv) | VRF 底层存储：32 个向量寄存器，每个寄存器逐字节用 `edff` 实现（纯触发器阵列）。 |
| [hdl/verilog/rvv/design/rvv_backend_rob.sv](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_rob.sv) | 重排序缓冲：按序入队、乱序接收 PU 结果、按序出队给退休，并处理 trap（异常）冲刷。 |
| [hdl/verilog/rvv/design/rvv_backend_retire.sv](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_retire.sv) | 退休单元：把 ROB 按序吐出的 uop 写回 VRF/XRF/FRF/vcsr/vxsat/fcsr，处理 WAW 与反压。 |
| [hdl/verilog/rvv/design/rvv_backend_retire_waw.sv](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_retire_waw.sv) | 退休单元的 WAW 合并子模块：在同周期多条 uop 写同一向量寄存器时合并字节、报告冲突。 |
| [hdl/verilog/rvv/inc/rvv_backend_define.svh](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh) | 全局宏定义：`NUM_VRF`、`VLEN`、`ROB_DEPTH`、各类端口数等，是本讲所有「容量/带宽」数字的来源。 |
| [hdl/verilog/rvv/inc/rvv_backend.svh](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend.svh) | 数据结构定义：`DP2ROB_t`、`PU2ROB_t`、`ROB2RT_t`、`RT2VRF_t` 等，决定了模块间传递什么。 |
| [hdl/verilog/rvv/common/multi_fifo.sv](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/common/multi_fifo.sv) | 通用「多入多出同步 FIFO」，ROB 用它来维护按序队列。 |

整体数据流（结合 u7-l2）：

```
Dispatch ──push(2/cycle, 按序)──> ROB ──retire(4/cycle, 按序)──> Retire ──写(4口)──> VRF
                                    ↑                              
            PU(ALU/MUL/MAC/LSU…) ──乱序写回(rob_entry 索引)──┘
            VRF ──读(4口+置换1口+v0)──> Dispatch（取操作数）/ Permutation
```

---

## 4. 核心概念与源码讲解

### 4.1 向量寄存器堆 VRF

#### 4.1.1 概念说明

向量寄存器堆（VRF）是 RVV 后端的数据仓库。每个向量寄存器宽 `VLEN` 位（实际构建为 128 位），可装下若干个元素——例如 `VLEN=128`、`SEW=32`（32 位元素）时，一个向量寄存器正好放 4 个 int32。

VRF 要同时服务三类访客：

1. **派发端（dispatch）读操作数**：一条向量指令通常要读 `vs1`、`vs2` 两个源寄存器，4 发射就要多个读口。
2. **置换端（permutation，PMT）读**：诸如 `vmerge`、`vrgather` 这类需要重新排列元素的指令，单独有一个读口。
3. **退休端（retire）写结果**：PU 算出的结果最终要写回 VRF。

此外，`v0` 寄存器在 RVV 中有特殊地位——它是默认的 **掩码寄存器（mask register）**，被掩码操作的指令会专门读 `v0`，因此 VRF 给它留了一条专用输出。

#### 4.1.2 核心流程

VRF 的工作可以拆成三件事：

- **读**：派发端/PMT 端给出寄存器号（index），VRF 当周期组合逻辑地返回该寄存器全量数据。读是「免费」的，因为底层是触发器阵列而非 SRAM（见 4.1.3 的关键洞察）。
- **写**：退休端给出最多 4 路写请求，每路带 **字节使能（byte-enable）**——即可以只改写寄存器里的某几个字节（RVV 的 tail/mask 语义要求「未触及的字节保持原值」）。VRF 把 4 路写请求按目标寄存器号「或」合并，再驱动底层阵列。
- **v0 直通**：`vreg[0]` 额外用一根专用线输出给派发端做掩码。

写合并的关键约束：**同一周期若多条 uop 写同一向量寄存器**，要由退休段的 WAW 逻辑先合并好（见 4.3），到达 VRF 时已是「按字节谁新谁覆盖」后的最终结果；VRF 内部用按位或做兜底合并。

#### 4.1.3 源码精读

**端口总览**——VRF 顶层模块的端口定义了它的三类访客：

[rvv_backend_vrf.sv:L6-L45](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_vrf.sv#L6-L45) 定义了模块端口。要点：

- `dp2vrf_rd_index [NUM_DP_VRF-1:0]` → 派发端的 **4 个读口**（`NUM_DP_VRF` 默认 4，见 [rvv_backend_define.svh:L40](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh#L40)）。
- `vrf2dp_v0_data` → v0 掩码寄存器的专用输出。
- `pmt2vrf_rd_index` / `vrf2pmt_rd_data` → 置换端的 1 个读口。
- `rt2vrf_wr_valid [NUM_RT_UOP-1:0]` / `rt2vrf_wr_data` → 退休端的 **4 个写口**（`NUM_RT_UOP = 4`，见 [rvv_backend_define.svh:L94](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh#L94)）。

**关键容量数字**——全部来自宏定义：

- 向量寄存器个数：[rvv_backend_define.svh:L57-L58](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh#L57-L58) `NUM_VRF = 32`，对应 5 位寄存器号（`REGFILE_INDEX_WIDTH = 5`，[L162](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh#L162)）。
- 每寄存器位宽：[rvv_backend_define.svh:L129-L140](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh#L129-L140) 由 `VLEN_128/256/512/1024` 宏选一个，实际构建选 128。
- 字节数：`VLENB = VLEN/8`（[L142](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh#L142)）。

**写请求的拆包与字节使能生成**——退休送来的 `RT2VRF_t` 包含 `rt_index`（目标寄存器号）、`rt_data`（数据）、`rt_strobe`（字节使能）。VRF 先把它拆开：

[rvv_backend_vrf.sv:L64-L100](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_vrf.sv#L64-L100)。逻辑要点：

1. 把 `rt_strobe`（每位代表一字节）扩展成 `wr_web`（每字节 8 位的位掩码），用 `wr_data & wr_web` 屏蔽掉未使能字节的位。
2. 对每路写请求，把使能与数据「散列」到对应寄存器号的位置：`vrf_wr_wen[j][wr_addr[j]] = wr_we[j]`——即第 `j` 路只点亮目标寄存器 `wr_addr[j]` 的某些字节。
3. 最后把 4 路写请求按寄存器号「或」合并成 `vrf_wr_wen_full` / `vrf_wr_data_full`（[L89-L100](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_vrf.sv#L89-L100)），驱动底层阵列。

**底层存储：纯触发器阵列**——

[rvv_backend_vrf_reg.sv:L22-L43](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_vrf_reg.sv#L22-L43)。这段用两层 `generate for` 把 32 个寄存器、每个寄存器 `VLENB` 个字节，逐字节实例化成 `edff`：

```systemverilog
for (i=0; i<`NUM_VRF; i=i+1)        // 32 个向量寄存器
  for (j=0; j<`VLENB; j=j+1) begin  // 每寄存器 VLENB 个字节
    edff #(.T(logic [`BYTE_WIDTH-1:0])) vrf_unit1_reg (
      .q(vreg[i][j*8 +: 8]),   // 该字节当前值
      .e(wen[i][j]),           // 该字节写使能
      .d(wdata[i][j*8 +: 8]),  // 待写入值
      .clk(clk), .rst_n(rst_n));
```

> **关键洞察（为什么 VRF 读口这么多还「不贵」）**：这里每个字节就是一个独立触发器（FF），整个 VRF 是 `32 × VLENB × 8` 个 FF 的阵列，**不是 SRAM**。SRAM 的读口数量会成倍增加面积与功耗，而 FF 阵列的「读」只是组合逻辑多路选择——所以派发端可以一口气开 4 个读口、置换端再开 1 个、v0 再专用一根线，都不构成端口压力。代价是 FF 阵列比 SRAM 面积大、功耗高，但换来「任意字节粒度写、任意多读口、单拍访问」的灵活性，这正是 RVV 尾/掩码语义和乱序后端所需要的。

**读侧打包**——读非常直接，纯组合选择：

[rvv_backend_vrf.sv:L118-L128](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_vrf.sv#L118-L128)：`vrf2dp_rd_data[j] = vrf_rd_data_full[dp2vrf_rd_index[j]]`（按读口给的寄存器号选），`vrf2dp_v0_data = vrf_rd_data_full[0]`（v0 直通），置换端同理。

#### 4.1.4 代码实践

**实践目标**：动手核对 VRF 的容量、端口数与存储组织，验证「32 个寄存器 × 每寄存器 VLENB 字节、逐字节 FF」的结论。

**操作步骤（源码阅读型）**：

1. 打开 [rvv_backend_define.svh:L57-L58](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh#L57-L58) 确认 `NUM_VRF = 32`；打开 [L129-L144](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh#L129-L144) 确认 `VLENB = VLEN/8`。
2. 打开 [rvv_backend_vrf_reg.sv:L24-L43](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_vrf_reg.sv#L24-L43)，数出两层 `for` 的循环边界，验证 FF 总数 = `NUM_VRF × VLENB × 8` 个触发器位。
3. 打开 [rvv_backend_vrf.sv:L26-L40](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_vrf.sv#L26-L40)，统计读口（`NUM_DP_VRF` 个派发口 + 1 个置换口 + v0 专用线）与写口（`NUM_RT_UOP` 个退休写口）。

**需要观察的现象 / 预期结果**：

- 读口总数远多于写口，且读是组合逻辑（无寄存器），写才进 `edff`。
- `wr_we` 是字节级使能，配合 `&wr_web` 实现部分写——这正是 RVV「tail-undisturbed / mask-undisturbed」在硬件上的落点。
- 在 `VLEN_128` 构建下，每个向量寄存器 = 16 字节 = 4 个 int32。

> 待本地验证：若你打开仿真波形（见 u7-l2 / u11 系列），可在一个写 v3 的退休周期观察到 `wen[3]` 的对应字节位被点亮、`vreg[3]` 的那些字节在下一拍变化，而 `vreg[3]` 未点亮的字节保持不变。

#### 4.1.5 小练习与答案

**练习 1**：为什么 CoralNPU 的 VRF 不像标量核的 DTCM 那样用 SRAM，而用 FF 阵列？
**参考答案**：RVV 后端需要 (a) 同周期多读口（派发 4 口 + 置换 + v0）以支撑多发射、(b) 任意字节粒度的部分写以满足 tail/mask 语义、(c) 单拍确定性访问配合乱序后端。SRAM 的端口数与位宽会成倍增加面积功耗且难以做字节级写；FF 阵列读口几乎免费、写天然字节粒度。代价是面积/功耗更大，但对于一个寄存器堆规模（32×128 位）可接受。

**练习 2**：`vrf2dp_v0_data` 为什么单独引一根线，而不是让派发端用某个读口去读 v0？
**参考答案**：v0 是 RVV 默认掩码寄存器，几乎所有掩码指令都要同时读它，若占用普通读口会与读 `vs1`/`vs2` 争抢端口、减少可发射的指令组合。专用线让掩码读取「不占读口」，等价于免费多一个口。

---

### 4.2 重排序缓冲 ROB

#### 4.2.1 概念说明

ROB（Re-Order Buffer）是「**乱序执行、按序退休**」的总指挥。它的存在解决一个矛盾：

- **执行要乱序**：不同执行单元延迟不同（ALU 快、DIV/MAC 慢、LSU 还要等访存）。若强求按序完成，慢指令会卡住后面所有快指令，吞吐崩塌。
- **语义要按序**：但程序可见的最终状态（寄存器堆、内存、CSR）必须像「一条一条按程序顺序执行」那样更新——否则异常处理、精确中断、WAW 全乱套。

ROB 的做法：派发时**按程序顺序**把每个 uop 入队（拿到一个 `rob_entry` 序号）；执行单元**谁先算完**就按 `rob_entry` 把结果**乱序写回** ROB 内部；退休时**只从队首按序出队**——只有当队首 uop「完成」时才允许它（以及它后面连续若干个已完成的 uop）退休写回寄存器堆。

#### 4.2.2 核心流程

ROB 是一个深度为 `ROB_DEPTH = 8` 的环形队列，由通用 `multi_fifo` 实现。每周期：

```
入队(push): Dispatch ──最多2个uop──> 队尾(wptr)            [按程序顺序]
写回(wb):  PU ──最多NUM_SMPORT个结果──> 任意entry(按rob_entry) [乱序]
出队(pop): Retire <──最多4个uop── 队首(rptr)              [按程序顺序]
```

ROB 内部为每个 entry 维护四张「表」：

| 表 | 类型 | 何时置位 | 何时清除 |
| --- | --- | --- | --- |
| `uop_info` | `DP2ROB_t`（指令信息） | 入队时写入 | 出队时弹出 |
| `entry_valid` | 1 位 | 入队时置 1 | 出队时清 0 |
| `res_mem` | `RES_ROB_t`（结果数据） | PU 写回时按 entry 写 | 随队列出队丢弃 |
| `uop_done` | 1 位 | PU 写回时置 1 | 退休出队时清 0 |
| `trap_flag` | 1 位 | 异常映射(rmp)报来时置 1 | flush 时清 0 |

**退休有效逻辑**（按序提交的核心）：只有队首满足条件才退休，且后续 uop 的退休被「链式」卡在前一个之后——

- `rd_valid[0] = valid[0] & (done[0] | trap[0])`：队首只要完成（或遇到 trap）就可出队。
- `rd_valid[i] = valid[i] & done[i] & rd_valid[i-1] & ~trap[prev]`：第 i 个要退休，必须自己 done、且前一个也退休、且前面没有 trap。一旦队首链上出现 trap，**退休在该 trap 处停下**，实现精确异常。

**乱序写回、按序出队为何能并存**：写入用 `rob_entry`（PU 给的物理下标）直寻址 `res_mem`，与队首指针无关，所以慢指令的结果可以晚到；而出队用 `rptr`（队首逻辑顺序），只有队首 done 才前进，所以最终提交顺序严格等于程序顺序。

**异常（trap）处理**：当某 uop 触发 trap，`trap_flag` 在对应 entry 置位；等这个带 trap 的 uop 升到队首并被退休读出时，ROB 拉起 `trap_flush_rvv` 持续 2 拍，把所有 FIFO 清空——即「丢弃 trap 之后所有已派发的投机指令」，保证精确异常。

#### 4.2.3 源码精读

**模块自述（带宽数字的权威来源）**——

[rvv_backend_rob.sv:L1-L14](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_rob.sv#L1-L14) 的注释直接写明三大带宽：每周期最多 **入队 2 个 uop**、**接收 9（微结构限制为 8）个 PU 结果**、**退休 4 个 uop**，并强调「bypass 给派发端的信息必须按程序顺序排序」。注释里的 9/8 是 `NUM_SMPORT` 在某配置下的值；该端口数 = `NUM_PU`（除非 `ARBITER_ON` 把它折叠成 4，见 [rvv_backend_define.svh:L87-L91](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend_define.svh#L87-L91)）。

**按序队列出队与反压**——ROB 用 `multi_fifo` 实例化两张并行 FIFO：

[rvv_backend_rob.sv:L104-L170](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_rob.sv#L104-L170)。其中：

- `u_uop_info_fifo`（[L104-L132](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_rob.sv#L104-L132)）：存 `DP2ROB_t` 指令信息，M=`NUM_DP_UOP`(2) 入、N=`NUM_RT_UOP`(4) 出。`almost_full` 反压派发端（[L135](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_rob.sv#L135) `uop_ready_rob2dp = ~almost_full`），保证 ROB 满时不再入队。
- `u_uop_valid_fifo`（[L141-L170](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_rob.sv#L141-L170)）：存 1 位 `entry_valid`，配 `POP_CLEAR`，弹出即清零。

`multi_fifo` 的关键特性（[multi_fifo.sv:L1-L11](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/common/multi_fifo.sv#L1-L11)、[L122-L131](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/common/multi_fifo.sv#L122-L131)）是「**输出全部 FIFO 数据并按读指针排序**」——`fifo_data[i] = mem[wind_rptr[i]]`（`wind_rptr[i]=rptr+i`）。这就解释了 ROB 如何把物理存储 `mem` 按「从队首起第 i 个」的逻辑顺序暴露给下游，无需额外排序逻辑。

**乱序写回：PU 结果直寻址写进 res_mem**——

[rvv_backend_rob.sv:L173-L191](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_rob.sv#L173-L191)。每个 PU 端口 `k` 用 `wr_pu2rob[k].rob_entry` 作为下标，把 `w_valid/w_data/vsaturate`（及可选的 `fpexp`）写进 `res_mem[rob_entry]`。注意它**不看队首**，所以哪个 PU 先回都行——这就是「乱序写回」。

**done 标志：连接乱序写回与按序出队**——

[rvv_backend_rob.sv:L206-L221](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_rob.sv#L206-L221)：

```systemverilog
for (k<NUM_RT_UOP) if (退休成交) uop_done[wind_uop_rptr[k]] <= 0;  // 出队清 done
for (k<NUM_SMPORT) if (wr_valid_pu2rob[k]) uop_done[wr_pu2rob[k].rob_entry] <= 1; // 写回置 done
```

这里 `wind_uop_rptr[k] = uop_rptr + k` 把「队首起第 k 个退休槽」映射回物理 entry。`uop_done` 是「乱序写回」与「按序出队」之间的唯一桥梁。

> **LSU 的反馈如何进来**：执行单元（含 LSU）的结果不是直接进 ROB，而是先经仲裁器 `rvv_backend_arb` 汇成 `res_arb2rob`（见 [rvv_backend.sv:L1071-L1072](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend.sv#L1071-L1072) `.wr_valid_pu2rob(res_valid_arb2rob), .wr_pu2rob(res_arb2rob)`）。LSU 通过 `UOP_LSU2RVV_t`（含 `lsu_vstore_last` 标志 store 完成、`vregfile_write_data` 回送 load 数据，见 [rvv_backend.svh:L499-L511](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/inc/rvv_backend.svh#L499-L511)）把结果送进仲裁器，再以 `PU2ROB_t` 形式回灌 ROB 置 done。所以一条向量 store 只有等 LSU 回送 `lsu_vstore_last` 后，其 ROB entry 才会被标 done、才有资格退休。

**按序退休有效逻辑（精确异常的关键）**——

[rvv_backend_rob.sv:L268-L293](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_rob.sv#L268-L293)：

```systemverilog
rd_valid[0] = valid[0] & (done[0] | trap[0]);
rd_valid[i] = valid[i] & done[i] & rd_valid[i-1] & ~trap_flag[队首起第(i-1)个];
```

这保证：(1) 队首 done（或 trap）才退休；(2) 后续 uop 必须前面都退休、且链上无 trap 才能跟着退休；(3) 一旦链上出现 trap，退休在 trap 处停住，触发冲刷。同时 [L281-L291](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_rob.sv#L281-L291) 把 `res_mem` 的结果与 `uop_info` 的写类型/地址拼成 `ROB2RT_t` 送给退休。

**trap 冲刷**——

[rvv_backend_rob.sv:L297-L300](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_rob.sv#L297-L300)：当队首是 trap 且被退休读出，`trap_in` 拉起，经一个 `edff` 让 `trap_flush_rvv` 保持 2 拍，把所有 FIFO 的 `clear` 端口点亮（见各 `multi_fifo` 的 `.clear(trap_flush_rvv)`）。

**结果前递（forwarding）给派发端**——

[rvv_backend_rob.sv:L303-L316](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_rob.sv#L303-L316)：ROB 把**全部** entry 的 `w_valid/w_index/w_data/w_type`（按程序顺序 `wind_uop_rptr` 排好）旁路给 dispatch。这样派发端发现某源操作数对应的 uop 虽然还没退休、但已 done，就可以直接从 ROB 前递取值，而不必等它退休写回 VRF——这是乱序后端提升吞吐的关键。

#### 4.2.4 代码实践

**实践目标**：跟踪「一条依赖前一条结果的向量指令」如何等待、何时乱序执行、何时按序提交。

**操作步骤（源码阅读 + 推演型）**：

设想 ROB 当前队列（队首→队尾）有三条 uop：

```
entry 队首:  uop_A = vadd.vv  v3, v1, v2      (ALU, 1 拍完成)
entry 队中:  uop_B = vmul.vv  v4, v3, v5      (MUL, 依赖 v3，即依赖 uop_A)
entry 队尾:  uop_C = vdiv.vv  v6, v7, v8      (DIV, 不依赖前两者，但 DIV 慢)
```

1. 打开 [rvv_backend_rob.sv:L173-L191](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_rob.sv#L173-L191)：假设 ALU 1 拍后把 uop_A 结果写回 `res_mem[A]` 并 `uop_done[A]=1`；DIV 慢，uop_C 晚若干拍才回。注意 `uop_B` 因为操作数 v3 要等 uop_A，派发端会用 [L303-L316](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_rob.sv#L303-L316) 的旁路拿到 v3。
2. 打开 [rvv_backend_rob.sv:L268-L275](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_rob.sv#L268-L275)：即使 uop_C（DIV）先于 uop_B 完成（done[C]=1, done[B]=0），由于 `rd_valid` 链要求「前一个 done 且无 trap」，队首是 A，A done 后可退休；但 B 未 done，于是 B 不能退休，C 虽 done 也排在 B 后面只能等。
3. 结合 [L206-L221](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_rob.sv#L206-L221) 的 done 清零：A 退休成交后 `uop_done[A]` 被清，队首指针前进到 B。

**需要观察的现象 / 预期结果**：

- **乱序执行**：uop_C（DIV）可能在 uop_B（MUL）之前完成并写回 ROB，体现乱序。
- **按序退休**：但退休顺序恒为 A → B → C，因为出队严格按 `rptr`。
- **依赖解除了 stall**：uop_B 不必等 uop_A 退休写回 VRF，靠 ROB 旁路拿到 v3 就能尽快进入 MUL 单元。

> 待本地验证：在仿真中可人为把 DIV 延迟拉大，观察 `uop_done[C]` 先于 `uop_done[B]` 置位，但 `rd_valid` 仍按 A→B→C 顺序逐拍拉起。

#### 4.2.5 小练习与答案

**练习 1**：ROB 既然支持乱序写回，为什么还需要 `uop_done` 这一位？直接看 `res_mem` 有没有值不行吗？
**参考答案**：`res_mem` 在复位时是 0、出队后也不主动清空，无法区分「这条还没写回」与「这条写回了 0 / 旧值」。`uop_done` 是一个明确的「已完成」标志，PU 写回时置 1、退休时清 0、flush 时清 0，是判断 entry 是否可退休的唯一可靠依据。

**练习 2**：如果队首 uop 触发了 trap，它后面那条已经 done 的 uop 会在本周期退休吗？
**参考答案**：不会。`rd_valid[i]` 要求 `~trap_flag[prev]`，一旦队首链上出现 trap，退休在该 trap 处停下；随后 `trap_flush_rvv` 会清空整个 ROB，丢弃 trap 之后的所有投机 uop，保证精确异常。

**练习 3**：派发端反压（`uop_ready_rob2dp = ~almost_full`）为什么用 `almost_full` 而不是 `full`？
**参考答案**：因为每周期可能入队最多 2 个 uop。若等到 `full` 才停，本周期入 2 个就会溢出。`almost_full[i]` 表示「剩余空间 ≤ i」，给入队逻辑留出提前量，避免越界（`multi_fifo` 也有相应的 overflow 断言，见 [multi_fifo.sv:L353-L362](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/common/multi_fifo.sv#L353-L362)）。

---

### 4.3 退休阶段 Retire

#### 4.3.1 概念说明

退休（retire）是 uop 的「毕业典礼」——只有经过退休，一条 uop 的结果才算真正更新了架构状态（寄存器堆、CSR）。ROB 已经保证了「按序出队」，退休要做的是把每个出队的 uop **按它的写类型分发到正确的目的地**，并处理同周期多条 uop 之间的冲突与反压。

退休要写回的目的地有（按 `W_DATA_TYPE_e` 分）：

- **VRF**：绝大多数向量指令的结果（`w_type == VRF`）。
- **XRF**：把向量归约（reduction）等指令的标量结果写回标量核的整数寄存器堆（经 `RT2RVS_t`，只取低 32 位）。
- **FRF**：浮点标量结果（仅 `ZVE32F_ON`，本仓库实际构建开启）。

此外还要更新一些「附带」架构状态：

- **vcsr**（向量配置寄存器：vl/vstart/vtype 等）：仅在 trap 时，由队首 uop 的 `vector_csr` 更新。
- **vxsat**（定点饱和标志）：若该向量指令发生了饱和，置位 `vcsr.vxsat` 的粘性位。
- **fcsr**（浮点异常标志，`ZVE32F_ON`）：聚合本周期退休 uop 的 nv/dz/of/uf/nx。

#### 4.3.2 核心流程

每周期退休最多处理 `NUM_RT_UOP = 4` 个 ROB 送来的 uop，流程：

1. **解包**：从 `ROB2RT_t` 取出 `w_index`（写地址）、`w_data`（数据）、`w_type`（写类型）、`trap_flag`、`vd_type`（每字节是 BODY_ACTIVE / TAIL / BODY_INACTIVE / NOT_CHANGE）、`vxsaturate`、`vector_csr` 等。
2. **trap 检查**：若队首（slot0）是 trap，则本周期只处理 vcsr 更新、不写寄存器堆；`w_valid_chkTrap[j]` 要求「前面所有 slot 都没有 trap」该 slot 才有效——保证 trap 之后的 uop 不写。
3. **WAW 合并**：对写 VRF 的 uop，调用 `rvv_backend_retire_waw` 检查同周期是否有更年轻的 uop 写同一向量寄存器；若有，年长的那个对应字节被屏蔽（`hit_waw`），只让最新的值生效。
4. **按类型分发**：VRF 写经 `rt2vrf_*`（带字节 strobe），XRF/FRF 写经 `rt2rvs_*`。
5. **反压链**：`rt2rob_write_ready[j]` 链式依赖 `rt2rob_write_ready[j-1]`——后一个 slot 能成交必须前一个也能成交，保证按序；同时根据写类型挂上对应目的地的 ready（VRF 要看 vxsat/fcsr 是否能收，XRF 看 rvs ready）。

**WAW（写后写）为什么要在退休段处理**：因为退休是按序的，同一周期退休的若干 uop 必然是程序顺序上连续的；如果它们写同一向量寄存器的重叠字节，架构语义要求「后写的覆盖先写的」。退休段在写进 VRF 之前把同周期冲突合并好，VRF 收到的就是最终结果。

#### 4.3.3 源码精读

**解包与每字节掩码生成**——

[rvv_backend_retire.sv:L124-L174](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_retire.sv#L124-L174)。其中 [L132-L142](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_retire.sv#L132-L142) 把每字节的 `vd_type` 译成 `w_strobe`（只有 `BODY_ACTIVE` 字节才写），并把饱和标志按字节筛选。`w_vxsat[j] = |w_vxsaturate[j]`（[L144](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_retire.sv#L144)）把任意字节饱和归约成一个位。

**trap 门控（精确异常在退休侧的体现）**——

[rvv_backend_retire.sv:L159-L163](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_retire.sv#L159-L163)：

```systemverilog
w_valid_chkTrap[0] = !trap_flag[0] && rob2rt_write_valid[0];
w_valid_chkTrap[j] = !(|trap_flag[j-1:0]) && rob2rt_write_valid[j];  // 前面任一 slot 有 trap 则本 slot 无效
```

这保证 trap 之后的 uop 不会写任何寄存器。

**WAW 合并**——

[rvv_backend_retire.sv:L176-L202](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_retire.sv#L176-L202) 为每个 slot（j≥1）实例化一个 `rvv_backend_retire_waw`，传入 `j+1` 个候选写请求，输出合并后的 `vrfres[j]`/`vrfres_strobe[j]` 与冲突标志 `waw[j]`；`hit_waw` 汇总后用 `vrfres_valid = w_vrf_valid & ready & ~hit_waw`（[L202](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_retire.sv#L202)）屏蔽被覆盖的年长 uop。

WAW 子模块本身很简洁——[rvv_backend_retire_waw.sv:L31-L48](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_retire_waw.sv#L31-L48)：它把「最年轻 slot（`UOP_NUM-1`）」作为基准，凡是有更年长 slot 写同一寄存器（`w_index` 相同）且都有效，就把年长 slot 的对应字节并进 `res`，并置 `waw` 标志（[L48](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_retire_waw.sv#L48)），从而让年长 uop 整体被 `hit_waw` 屏蔽掉（它的字节已经被并进年轻 uop 的结果了）。

**反压链（按序提交的退休侧）**——

[rvv_backend_retire.sv:L225-L263](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_retire.sv#L225-L263)。`rt2rob_write_ready[0]` 依 slot0 的写类型决定（VRF 看 `vxsat2rt_ready`、XRF 看 `rvs2rt_write_ready`、trap 看 `vcsr2rt_write_ready`）；`rt2rob_write_ready[j] = rt2rob_write_ready[j-1] & (对应目的地 ready)`。这条链把 4 个 slot 的成交条件串成「前一个能成，后一个才能成」，与 ROB 的 `rd_valid` 链遥相呼应，共同锁死按序提交。

**写 VRF / 写 XRF 打包**——

- 写 VRF：[rvv_backend_retire.sv:L265-L282](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_retire.sv#L265-L282) 把 WAW 合并后的 `vrfres`/`vrfres_strobe` 装进 `RT2VRF_t`（`rt_index`/`rt_data`/`rt_strobe`），这正是 4.1 节 VRF 收到的写请求来源。
- 写 XRF：[rvv_backend_retire.sv:L284-L310](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_retire.sv#L284-L310) 取 `w_data[31:0]` 装进 `RT2RVS_t` 送回标量核。

**附带状态更新**——

- vcsr（trap 时）：[rvv_backend_retire.sv:L206-L207](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_retire.sv#L206-L207) 只在 slot0 trap 时写 `vector_csr`。
- vxsat：[L210-L212](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_retire.sv#L210-L212) 当本周期有 VRF 写且发生饱和时置位粘性 `vxsat`。
- fcsr（`ZVE32F_ON`）：[L214-L223](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_retire.sv#L214-L223) 把本周期各 slot 的浮点异常标志按位或聚合写回 fcsr。

**顶层把三者串起来**——

[rvv_backend.sv:L1058-L1159](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend.sv#L1058-L1159) 实例化 `u_rob`、`u_retire`、`u_vrf` 三者：ROB 的 `rd_rob2rt` 接到退休的 `rob2rt_write_data`，退休的 `rt2vrf_write_*` 接到 VRF 的 `rt2vrf_wr_*`，形成「ROB → Retire → VRF」的提交主干。这也是为什么本讲把三者放在一起讲——它们是一条连续的提交流水线。

#### 4.3.4 代码实践

**实践目标**：理解 WAW 合并与按序反压，验证「同周期多条 uop 写同一向量寄存器时只保留最新字节」。

**操作步骤（源码阅读 + 推演型）**：

设想某周期退休槽同时有两条写 VRF 的 uop（程序顺序 slot0 在前、slot1 在后）：

```
slot0: w_index=v3, w_data=0x11..11, strobe=全1（写满 v3）
slot1: w_index=v3, w_data=0x22..22, strobe=仅高半字节点亮（改 v3 的高半）
```

1. 打开 [rvv_backend_retire_waw.sv:L36-L45](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_retire_waw.sv#L36-L45)：以 slot1（最年轻）为基准，`vd_hit[0]` = slot0 也写 v3 且 slot1 有效 → 命中。对 slot0 点亮的、与 slot1 重叠的字节，`res` 取 slot1 的值（0x22..），不重叠的字节由 slot1 自己的 strobe 决定。
2. 打开 [rvv_backend_retire.sv:L194-L202](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_retire.sv#L194-L202)：`hit_waw` 置位 → `vrfres_valid[0]` 被屏蔽，slot0 不再单独写 VRF；只有合并后的 slot1 结果写回。
3. 打开 [rvv_backend_retire.sv:L244-L263](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/rvv/design/rvv_backend_retire.sv#L244-L263)：确认 `rt2rob_write_ready[1]` 依赖 `[0]`，二者要么一起成交、要么一起被反压。

**需要观察的现象 / 预期结果**：

- 最终写入 VRF 的 v3 = slot0 的低半（未被 slot1 覆盖部分）+ slot1 的高半，符合「后写覆盖先写」的程序语义。
- slot0 不会重复写一遍（避免先写旧值再被 slot1 盖掉的浪费与短暂错值）。
- 若 VRF/vxsat/fcsr 任一目的地未就绪，整组退休被反压，ROB 队首不动。

> 待本地验证：可在 cocotb/UVM 测试中构造「连续两条写同一向量寄存器、且字节部分重叠」的程序，对比仿真波形中 `rt2vrf_write_valid` 与最终 `vreg` 值，确认 WAW 合并行为。

#### 4.3.5 小练习与答案

**练习 1**：为什么 WAW 合并只在「同周期退休」的 uop 之间做，而不处理跨周期的 WAW？
**参考答案**：跨周期的 WAW 天然按序——上一周期已把「较旧」的值写进 VRF，本周期再写「较新」的值会覆盖它，VRF 的字节写使能天然实现覆盖。只有同周期退休的多条 uop 会在同一拍竞争 VRF 同一寄存器，必须在写进 VRF 前合并，否则会出现「先写新值、再被旧值覆盖」的错序。

**练习 2**：退休段的 `rt2rob_write_ready` 链与 ROB 的 `rd_valid` 链，分别保证什么？
**参考答案**：ROB 的 `rd_valid` 链保证「**出队**严格按程序顺序、并在 trap 处停下」（精确异常的源头）；退休的 `rt2rob_write_ready` 链保证「**写回**也按程序顺序、整组要么一起成交要么一起等」（反压回传给 ROB，控制队首是否前进）。两条链一前一后，共同把「乱序执行」收束回「按序更新架构状态」。

---

## 5. 综合实践

把三个模块串起来，完成一次完整的「提交流水线」追踪。

**任务**：构造一段含数据依赖与饱和运算的向量程序（伪汇编即可），在源码层面跟踪它的 uop 走完 VRF→ROB→Retire 全流程。

示例伪程序：

```
1. vadd.vv   v3, v1, v2          # ALU，1 拍，写 v3
2. vsadd.vi  v4, v3, 100         # MUL/MAC 路径，定点饱和加，依赖 v3，可能饱和，写 v4
3. vredsum.vs v5, v4             # reduction，标量结果写回 XRF（x 标量寄存器）
```

**要求**：

1. **VRF 侧**（4.1）：标出指令 1/2 读 `v1/v2/v3` 走哪些读口，v0 是否被用作掩码；标出 `v3`、`v4` 各占哪些字节、`w_strobe` 如何由 `vd_type` 生成。
2. **ROB 侧**（4.2）：画出三条 uop 的 `rob_entry` 分配、谁先 done、退休顺序；说明指令 2 如何通过 ROB 旁路拿到 `v3`（而不等指令 1 退休写回 VRF）。
3. **Retire 侧**（4.3）：标出指令 2 若发生饱和，`vxsat` 如何被置位；标出指令 3 的标量结果经 `RT2RVS_t` 写回 XRF 的路径；检查本组是否有 WAW（本例三条写不同寄存器，应无 WAW）。
4. **总结**：用一句话写出「为什么这套设计能在保证程序语义不变的前提下，让慢指令不阻塞快指令」。

**预期产出**：一张标注了读口/写口/rob_entry/done 顺序/写回目的地的时序草图，以及对「乱序执行、按序退休」如何落到这三个模块的一句话解释。

---

## 6. 本讲小结

- **VRF** 是 32 个向量寄存器（`NUM_VRF=32`）、每寄存器 `VLEN` 位（实际构建 `VLEN_128`=128 位）的 **纯触发器阵列**：读口几乎免费（派发 4 口 + 置换 1 口 + v0 专用），写口 4 个退休写口，写支持 **字节级使能** 以满足 RVV 的 tail/mask 语义。
- **ROB** 是深 8 的环形队列（`multi_fifo`）：按序入队（2/周期）、乱序接收 PU 结果（按 `rob_entry` 直寻址写 `res_mem` 并置 `uop_done`）、按序出队（4/周期，队首 done 才前进）。`uop_done` 是乱序写回与按序出队之间的桥梁；`rd_valid` 链保证精确异常。
- **Retire** 把按序出队的 uop 按 `w_type` 分发到 VRF/XRF/FRF，并在写 VRF 前做 **WAW 合并**（同周期多条 uop 写同一向量寄存器只保留最新字节）；附带更新 vcsr（trap）、vxsat（饱和）、fcsr（浮点异常）。
- **LSU 反馈** 通过仲裁器 `res_arb2rob` 回灌 ROB：向量 store 等 `lsu_vstore_last`、向量 load 回送数据，置 `uop_done` 后才有资格退休。
- **两处文档/实现差异**需记牢：overview 写「64 个 v0..v63、256 位」，实际 RTL 是 **32 个 v0..v31**、实际构建 **VLEN=128 位**；读源码与做推演时一律以 RTL 宏定义为准。
- ROB 还会把全部 entry 的结果 **旁路给派发端**，使后续 uop 不必等前序退休写回 VRF 即可拿到操作数——这是乱序后端提升吞吐的关键。

---

## 7. 下一步学习建议

- **横向对照标量核**：本讲的「乱序执行、按序退休」可对照 u4-l4（标量核派发/记分板/退休）。CoralNPU 标量核是「顺序派发、乱序退休」的轻量 `RetirementBuffer`，而 RVV 后端是完整的乱序执行 + ROB；比较二者能加深对「为何标量核不需要完整 ROB」的理解。
- **继续 RVV 后端**：下一篇 u7-l4（MAC 外积乘累加引擎）将讲解真正吃算力的 `rvv_backend_mulmac`——它每周期产生 256 MACs，结果最终也经本讲的 ROB→Retire→VRF 回写，可把两讲连起来看 MAC 结果如何退休。
- **验证视角**：若想看本讲模块的实际波形，参看 u11-l2（VCS/UVM）与 `hdl/verilog/rvv/sve/rvv_backend_tb`——那里的 agent/scoreboard 正是对 VRF/ROB/Retire 行为做定向 + 随机回归的。
- **源码延伸阅读**：`rvv_backend_arb.sv`（PU 结果仲裁）、`rvv_backend.svh` 中 `ROB_t`/`RES_ROB_t`/`ROB2RT_t` 的字段定义，能帮你把本讲涉及的每个信号在数据结构层面定位清楚。
