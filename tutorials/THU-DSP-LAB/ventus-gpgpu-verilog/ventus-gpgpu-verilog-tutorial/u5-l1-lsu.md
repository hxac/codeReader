# 访存单元 LSU

## 1. 本讲目标

本讲聚焦 Ventus GPGPU SM 流水线中的**访存执行单元（Load/Store Unit，LSU）**。学完后你应当能够：

- 说清一条向量 load/store 指令（如 `VLE32_V`）从进入 LSU 到写回寄存器堆的**完整数据通路**与每一道工序的职责。
- 理解 LSU 如何为 warp 内 `NUM_THREAD` 个 lane **逐 lane 计算地址**，并按地址落在共享内存还是 D-cache 把请求分流。
- 掌握 LSU 自带的 **MSHR（Miss Status Holding Register，此处实为「合并器/coalescer」）** 如何用一个表项跟踪一条向量访存指令拆出的多个子请求、合并乱序返回的响应、判定一条指令何时真正完成。
- 理解字节加载的字/半字/字节宽度（`MEM_W/H/B`）如何变成 `wordoffset1h` 字节使能掩码，以及 `byte_extract` 如何做对齐与符号扩展。
- 看懂 `lsu2wb` 如何把 LSU 结果按目的寄存器类型分流到标量/向量写回端口。

## 2. 前置知识

在进入 LSU 之前，先建立几个直觉性的概念。

**什么是向量访存？** Ventus 是 SIMT 架构（详见 u1-l1）：一条 `VLE32_V`（向量加载）指令广播给整个 warp，warp 内 `NUM_THREAD` 个线程（lane）**同时**各算各的地址、各取各的数据。所以一条向量 load 实际上是 `NUM_THREAD` 个独立的内存访问捆绑在一起。LSU 的核心工作，就是把这「一捆」访问拆开、算地址、发给下层存储、再把乱序回来的数据重新拼回一个向量写回寄存器堆。

**为什么需要 MSHR？** 这 `NUM_THREAD` 个 lane 的地址很可能落在**不同的 cache 块**里（甚至一部分落共享内存、一部分落 D-cache），所以一条向量访存会被拆成多个对下层的子请求；这些子请求会**乱序、分多次**返回。LSU 必须有个地方记住「这条指令还差哪些 lane 的数据没回来」——这就是 MSHR（在本讲义里实例名为 `coalscer`，即 coalescer 合并器）。它与 D-cache 内部的 MSHR（见 u6-l1）不是同一个东西，请不要混淆：D-cache MSHR 跟踪的是 cache 缺失，LSU MSHR 跟踪的是**一条向量访存指令的多个子请求**。

**共享内存 vs 全局内存。** SM 内部有一块低延迟的**共享内存（Shared Memory / LDS）**，地址空间从 0 开始；全局内存（Global Memory）走 D-cache → 互联 → L2。LSU 用一个简单的地址比较（`addr < SHAREMEM_SIZE`）来判定每个 lane 该走哪条路。

**几个关键宏（来自 [define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v)）先记一下：**

| 宏 | 值/含义 | 行号 |
|---|---|---|
| `BYTESOFWORD` | 4（一个字 4 字节） | [define.v:81](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L81) |
| `LSU_NMSHRENTRY` | = `NUM_WARP`（LSU MSHR 表项数） | [define.v:67](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L67) |
| `LSU_NUM_ENTRY_EACH_WARP` | 4（每 warp 的 inflight 配额，用于 fence） | [define.v:65](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L65) |
| `DCACHE_BLOCKWORDS` | 2（一个 cache 块含 2 个字） | [define.v:73](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L73) |
| `DCACHE_BLOCKOFFSETBITS` | `$clog2(DCACHE_BLOCKWORDS)` | [define.v:85](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L85) |
| `DCACHE_TAGBITS` | `XLEN-(SETIDXBITS+BLOCKOFFSETBITS+WORDOFFSETBITS)` | [define.v:87](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L87) |
| `MEM_W / MEM_H / MEM_B` | 字/半字/字节访存宽度编码（`2'b11/2'b01/2'b10`） | [define.v:462-464](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L462-L464) |
| `SHAREMEM_SIZE` | `SHAREDMEM_DEPTH * BLOCKWORDS * 4`（共享内存容量上限） | [define.v:123](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L123) |

承接 u4-l1：操作数采集器（operand_collector）已为指令凑齐了源操作数。对于访存指令，`in1`/`in2` 是参与**地址计算**的基址与偏移操作数，`in3` 是 **store 要写入的数据**，这些连同控制信号一起由 `issue` 路由进 LSU。

## 3. 本讲源码地图

本讲涉及的关键文件全部位于 `src/gpgpu_top/sm/pipeline/lsu/` 下：

| 文件 | 作用 |
|---|---|
| [lsu_exe.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/lsu_exe.v) | LSU 顶层，只做例化与连线，把 5 个子模块串成一条流水线 |
| [input_fifo.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/input_fifo.v) | 入口缓冲（深度 1），把请求字段打包后用 stream FIFO 削峰、解耦前后级时序 |
| [addrcalculate.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/addrcalculate.v) | **地址计算**：逐 lane 算地址、判定 shared/dcache、拆分 tag/setidx/offset、用状态机把请求逐组发出 |
| [mshr_reg.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/mshr_reg.v) | **LSU MSHR（合并器）**：分配表项、缓存指令元信息、累积响应数据、判定完成并输出 |
| [rsp_arb.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/rsp_arb.v) | 响应仲裁器：在 dcache 响应与 shared 响应之间二选一送给 MSHR |
| [byte_extract.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/byte_extract.v) | 单 lane 字节选择：按 `wordoffset1h` 从 32 位字中抽出字节/半字并做符号/零扩展 |
| [lsu2wb.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/lsu2wb.v) | 写回分流：把 LSU 结果按 `wxd`/`wfd` 接到标量或向量写回端口 |
| [shiftboard.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/shiftboard.v) | 每 warp 一个移位寄存器，统计该 warp 在途 LSU 请求数，供 fence 判定完成 |

## 4. 核心概念与源码讲解

### 4.1 LSU 总览：一条流水线把请求「拆开—发出—合并—写回」

#### 4.1.1 概念说明

`lsu_exe` 是 LSU 的对外门面，自身几乎不含逻辑，只例化 5 个子模块并用 wire 把它们串起来。可以把 LSU 想象成一条**单向流水线**，请求从左进、响应从右出，中间被「拆」成多个子请求发给两个下游（D-cache 与共享内存），再被「合」回来。理解 LSU 的关键是先建立这条流水线的全景图，再去逐个看子模块。

#### 4.1.2 核心流程

LSU 的整体数据流可以这样描述（伪代码）：

```
pipe.issue ──lsu_req──▶ input_fifo ──▶ addrcalculate ──┬──to_dcache──▶ D-cache
                                  │                    │
                                  │                    └──to_shared──▶ SharedMem
                                  │
              ┌──idx_entry─────────┘ (申请一个 MSHR 表项, instrid)
              ▼
            mshr(MSHR)  ◀──rsp_arb─── (dcache_rsp | shared_rsp, 携带 instrid 回路由)
              │
              └──lsu_rsp──▶ lsu2wb ──┬──out_x──▶ 标量写回
                                    └──out_v──▶ 向量写回
```

要点：
1. **input_fifo** 缓存来自 `issue` 的请求，深度为 1，主要起时序解耦作用。
2. **addrcalculate** 是请求侧的主角：算地址、分流、申请 MSHR 表项、把请求**按 cache 块分组**逐组发出。每发一组，就用同一个 `instrid`（=MSHR 表项号）打标。
3. **MSHR（coalscer）** 是响应侧的主角：它给每个新请求分配一个空闲表项，把指令的「元信息」（哪个 warp、写哪个寄存器、激活掩码、字节使能等）存进 tag SRAM，等响应回来时按 `instrid` 把数据写进 data SRAM、并清掉对应 lane 的「待完成」位；当一条指令所有 lane 的数据都到齐，就把整条指令的结果送出。
4. **rsp_arb** 在 D-cache 响应和共享内存响应之间做固定优先级选择。
5. **byte_extract** 在 MSHR 输出时对每个 lane 做字节对齐与符号扩展。
6. **lsu2wb** 按目的寄存器是标量（`wxd`）还是向量（`wfd`）把结果分流到写回总线。

#### 4.1.3 源码精读

`lsu_exe` 的端口清晰地刻画了「一进二出二回一出」的结构。请求侧输入（来自 pipe 的 `issue`）携带三个向量操作数与一束控制信号：

[lsu_exe.v:26-47](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/lsu_exe.v#L26-L47) —— `lsu_req_*` 输入端口。`in1/in2/in3` 各为 `XLEN*NUM_THREAD` 位（每 lane 32 位），分别是地址基址、地址偏移、store 数据；`mask` 是激活线程掩码；`mem_whb` 是访存宽度；`mem_cmd` 区分 load/store；`alu_fn` 在原子操作时携带原子功能码；`atomic/aq/rl/fence` 控制原子与栅栏行为。

顶层把四个核心子模块按数据流顺序例化。input_fifo 在最前：

[lsu_exe.v:142-189](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/lsu_exe.v#L142-L189) —— `input_fifo infilo(...)` 例化，把 `lsu_req_*` 全部接入 enq 口、deq 口送给 addrcalculate。

随后是 addrcalculate（请求侧）、rsp_arb（响应仲裁）、mshr（合并器）：

[lsu_exe.v:279-330](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/lsu_exe.v#L279-L330) —— 这段同时可见三个关键连接：`rsp_arb` 把 dcache/shared 两路响应汇总成 `arb_mshr_*` 喂给 `mshr`（实例名 `coalscer`）；而 addrcalculate 申请到的表项号 `mshr_addr_idx_entry` 回送给 addrcalculate 作为 `instrid`。注意第 297 行实例名 `coalscer` 是「coalescer（合并器）」的拼写，恰好点明 MSHR 在此的职责。

此外，LSU 还有一组用于 **fence** 的 `shiftboard`，按 warp 统计在途请求数：

[lsu_exe.v:338-360](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/lsu_exe.v#L338-L360) —— 用 `generate for` 为每个 warp 例化一个 `shiftboard`：`left_move`（请求进入）左移置 1、`right_move`（响应返回）右移清 0；当某 warp 的板全空（`empty[i]`）时 `fence_end_o[i]` 拉高，表示该 warp 的所有访存已落地，fence 可以放行。同时，若某 warp 的板已满（`full`），LSU 会**拒收**该 warp 的新请求（第 359-360 行把 `input_fifo_enq_valid` 和 `lsu_req_ready_o` 清零），这是结构反压。

#### 4.1.4 代码实践

**实践目标：建立 LSU 顶层的数据通路全景图。**

1. 打开 [lsu_exe.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/lsu_exe.v)。
2. 找到五个例化语句（`infifo`、`addrcalc`、`rsparbiter`、`coalscer`、`board`），在纸上画出它们之间的连线。
3. 标注每条连线的方向与字段含义（例如 `addr_mshr_valid` 是 addrcalculate→MSHR 的请求、`mshr_addr_idx_entry` 是 MSHR→addrcalculate 的表项号回送）。
4. 对照 [pipe.v:1679-1760](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1679-L1760) 中 `lsu_exe lsu(...)` 的例化，确认 LSU 对外的 `dcache_req_*`、`shared_req_*`、`dcache_rsp_*`、`shared_rsp_*` 最终连到了 `sm_wrapper` 的存储接口。

**需要观察的现象：** LSU 顶层确实只做连线、不含运算；请求侧（addrcalculate）与响应侧（mshr）通过 `instrid` 这个表项号建立「请求-响应」的对应关系。

**预期结果：** 你能得到一张与 4.1.2 节伪代码一致的框图，并理解 `instrid` 是贯穿请求与响应的「身份编号」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 LSU 入口要放一个 `input_fifo`？去掉它直接把 `lsu_req` 接到 addrcalculate 行不行？

> **参考答案**：addrcalculate 的地址计算与状态机是组合+时序混合逻辑，且它处理一条向量访存要花多拍（逐组发出）。`input_fifo`（深度 1 的 stream FIFO）把 `issue` 的瞬时握手与 addrcalculate 的多拍处理解耦，让 `lsu_req_ready_o` 能快速回应 `issue`，避免上游因下游忙而长时间阻塞。直接相连会延长组合路径并使握手耦合到状态机时序上。

**练习 2**：`fence_end_o` 何时对一个 warp 拉高？它与 shiftboard 的 `empty` 是什么关系？

> **参考答案**：当某 warp 的 shiftboard 完全清空（`empty[i]==1`，即该 warp 进入 LSU 的请求数等于返回的响应数、在途为 0）时，`fence_end_o[i]` 拉高。这告诉流水线该 warp 此前的所有访存已全部落地，`fence` 指令可以放行后续指令。

---

### 4.2 地址计算 addrcalculate：逐 lane 算地址、分流、分组发出

#### 4.2.1 概念说明

addrcalculate 是请求侧的核心，回答三个问题：(1) 每个 lane 的访存地址是多少？(2) 每个 lane 该走共享内存还是 D-cache？(3) 怎么把这一捆访问**分批**发给下层？

关键难点在于：一条向量访存的 `NUM_THREAD` 个 lane 地址可能落在多个不同的 cache 块里。D-cache 一次只能处理一个块（一个 tag），所以 addrcalculate 必须**按 tag 分组**——挑出一组地址落在同一 cache 块的 lane，发一次 D-cache 请求；然后把这组 lane 从掩码里清掉，再处理下一组，直到所有活跃 lane 都被处理。共享内存同理。

#### 4.2.2 核心流程

addrcalculate 用一个六态状态机驱动：

```
S_IDLE ──有请求──▶ S_SAVE ──all_shared──▶ S_SHARED (逐组发共享内存)
                        │                       │
                        └──否则──▶ S_DCACHE ──┬─▶ S_DCACHE_1 ─▶ S_DCACHE_2 (原子 aq/rl 多轮)
                              (逐组发 D-cache)│
                                              └─▶ S_IDLE (全部 lane 处理完)
```

- **S_IDLE**：等请求。来一个请求就锁存到 `reg_save_*`，并从 MSHR 申请一个表项号。
- **S_SAVE**：根据 `all_shared`（所有活跃 lane 都落共享内存吗？）决定走 `S_SHARED` 还是 `S_DCACHE`。同时把这条指令的元信息（warp_id、reg_idxw、mask、字节使能、iswrite 等）写入 MSHR 表项。
- **S_SHARED / S_DCACHE**：按 tag 分组发出请求。每成功发一组，用 `mask_next` 把已处理 lane 从 `reg_save_mask` 中清掉；当 `cnt >= NUM_THREAD` 或 `mask_next == 0`（所有活跃 lane 处理完），回 `S_IDLE`。
- **S_DCACHE_1 / S_DCACHE_2**：原子操作（`aq`/`rl`）需要额外的读-改-写轮次，故多两个状态。

每个 lane 的地址计算（普通向量访存，非 `disable_mask` 的 strided/indexed 类型）为：

\[
\text{addr}[i] = \text{in1}[i] + \text{stride}(i)
\]

其中步长 `stride` 由 `mop`（memory operand）决定：`mop==00` 是单位步长（连续访存，`i<<2`，即每 lane 4 字节）、`mop==11` 是 indexed（用 `in2[i]` 作每 lane 偏移）、其余是常量步长（`i * in2[i]`）。

地址判定共享内存的条件非常简单：

\[
\text{is\_shared}[i] = (\neg \text{mask}[i]) \;\lor\; (\text{addr}[i] < \text{SHAREMEM\_SIZE})
\]

即该 lane 非活跃，或地址落在共享内存地址区间内。

#### 4.2.3 源码精读

逐 lane 地址计算与共享内存判定是核心的组合逻辑（注释掉的旧版用的是 `reg_save_*`，当前生效版本直接用 `from_fifo_*`）：

[addrcalculate.v:175-211](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/addrcalculate.v#L175-L211) —— `generate for` 为每个 lane 计算地址 `addr[i]`：向量单位步长时 `addr[i] = in1[i] + (i<<2)`；并把结果寄存一拍得到 `addr_reg[i]`。第 196 行给出 `is_shared[i]` 判定，第 213 行 `all_shared` 汇总（向量需所有 lane 都 shared，标量看 lane 0）。

地址算出后，按 cache 几何切成 tag / setidx / blockoffset / wordoffset：

[addrcalculate.v:257-258](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/addrcalculate.v#L257-L258) —— 从优先编码器选出的代表地址 `addr_wire` 中切出 `tag` 与 `setidx`（用 `$clog2` 派生的位宽）。一个 32 位地址的布局为：

\[
\underbrace{\text{tag}}_{\text{DCACHE\_TAGBITS}}\;
\underbrace{\text{setidx}}_{\text{DCACHE\_SETIDXBITS}}\;
\underbrace{\text{blockoffset}}_{\text{DCACHE\_BLOCKOFFSETBITS}}\;
\underbrace{\text{wordoffset}}_{\text{DCACHE\_WORDOFFSETBITS(=2)}}
\]

访存宽度 `mem_whb` 被翻译成 4 位的字节使能 `wordoffset1h`，这正是后面 `byte_extract` 选择字节的依据：

[addrcalculate.v:277-290](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/addrcalculate.v#L277-L290) —— `MEM_W`（字）使能 `4'b1111`；`MEM_H`（半字）按 `addr[1]` 选 `4'b0011` 或 `4'b1100`；`MEM_B`（字节）按 `addr[1:0]` 移位得到 `4'b0001<<偏移`。

「同 tag」判定 `same_tag` 决定哪些 lane 可共用同一次 D-cache 请求：

[addrcalculate.v:271-308](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/addrcalculate.v#L271-L308) —— `same_tag[j]` 比较该 lane 地址的高位（tag+setidx）是否等于代表 lane 的 tag+setidx。第 427 行 `to_dcache_activemask_o[m] = reg_save_mask[m] && same_tag[m]`：只有与代表 lane 同块的活跃 lane 才纳入本次请求。

分组循环靠 `mask_next` 逐步清空已处理 lane：

[addrcalculate.v:434-438](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/addrcalculate.v#L434-L438) 与 [addrcalculate.v:717-724](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/addrcalculate.v#L717-L724) —— 每成功发出一次 D-cache 请求，`reg_save_mask <= mask_next`（清掉 `same_tag` 命中的 lane），下一轮再从剩余 lane 里挑代表、继续分组，直到 `mask_next==0`。

发往 D-cache 的 TileLink 风格 `opcode`/`param` 在普通 load/store 时很简单（`opcode=mem_cmd[1]`，0=load/1=store），但原子操作要把 `alu_fn` 映射成 AMO 的 param：

[addrcalculate.v:376-421](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/addrcalculate.v#L376-L421) —— 把 `FN_AMOADD/FN_MIN/FN_MAX/FN_XOR/...`（见 [define.v:558-559](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L558-L559) 等）映射成 TileLink 的 AMO param 码（如 AMOADD=0、MIN=4、MAX=5）。

#### 4.2.4 代码实践

**实践目标：手动模拟一条向量 load 的分组发出过程。**

假设 `NUM_THREAD=4`，一条 `VLE32_V` 的四个 lane 地址经计算为：

| lane | addr（字节） | tag+setidx | blockoffset | wordoffset1h |
|---|---|---|---|---|
| 0 | 0x100 | A | 0 | 1111 |
| 1 | 0x104 | A | 0 | 1111 |
| 2 | 0x200 | B | 0 | 1111 |
| 3 | 0x204 | B | 0 | 1111 |

`DCACHE_BLOCKWORDS=2`（一块 8 字节，含 2 个字）。

1. 第一轮：优先编码选 lane 0 为代表，tag+setidx=A。`same_tag=[1,1,0,0]`（lane0、1 同块）。`activemask=1110`... 请你按 [addrcalculate.v:427](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/addrcalculate.v#L427) 算出本次 `to_dcache_activemask` 与 `mask_next`。
2. 第二轮：`reg_save_mask` 已更新为 `mask_next`，再选下一个代表 lane（lane 2），tag+setidx=B，处理 lane 2、3。
3. 直到 `mask_next==0`，回 `S_IDLE`。

**需要观察的现象：** 4 个 lane 落在 2 个不同的 cache 块（A、B），所以这条向量 load 被**拆成 2 次** D-cache 请求；这两次请求共用同一个 `instrid`（同一个 MSHR 表项）。

**预期结果：** 两次请求发出，每次 `activemask` 分别覆盖 lane{0,1} 与 lane{2,3}。这正是 MSHR 需要合并多次响应的原因。若你无法确定具体地址编码，可标注「待本地验证」并在仿真中用波形核对 `to_dcache_activemask_o`。

#### 4.2.5 小练习与答案

**练习 1**：`MEM_B`（字节加载）时，`wordoffset1h` 如何由地址得到？为什么是 `4'b0001 << addr[1:0]`？

> **参考答案**：一个 32 位字有 4 个字节，地址最低 2 位 `addr[1:0]` 指示要取哪个字节（0~3）。`4'b0001 << addr[1:0]` 生成一个 4 位的 one-hot 字节使能，指明该 lane 要从字的哪个字节位置取数据，后续 `byte_extract` 据此对齐。

**练习 2**：为什么 addrcalculate 要循环多拍发出，而不是一拍把所有 lane 的请求一起发给 D-cache？

> **参考答案**：D-cache 一次请求只携带一个 tag/setidx（一个 cache 块）。当多个 lane 落在不同块时，无法用一次请求表达。故 addrcalculate 按 tag 分组，每组发一次，靠 `mask_next` 逐组清空掩码、循环处理，直到覆盖所有活跃 lane。

---

### 4.3 LSU MSHR（mshr_reg）：跟踪、合并、判定完成

#### 4.3.1 概念说明

`mshr_reg`（实例名 `coalscer`）是 LSU 响应侧的灵魂。它要解决的问题是：一条向量访存指令被 addrcalculate 拆成了若干次子请求（可能一部分发去共享内存、一部分发去 D-cache，且 D-cache 那部分又分多个块），这些子请求会**乱序、分多次**返回。MSHR 必须用一个表项把这条指令的所有子响应「攒」起来，直到全部到齐，再作为一条完整结果送回 pipe。

它用两块双端口 SRAM 实现：
- **tag SRAM**：存指令的元信息（warp_id、目的寄存器号、激活掩码、字节使能、unsigned、iswrite、wfd/wxd）。
- **data SRAM**：按 lane 累积返回的数据。

并用一个 `current_mask`（每个表项 `NUM_THREAD` 位）记录「这条指令还差哪些 lane 没回」——每来一个响应，就把该响应激活的那些 lane 在 `current_mask` 中清掉；当某表项的 `current_mask` 全 0，该表项「完成（complete）」，可以输出。

#### 4.3.2 核心流程

MSHR 表项数为 `LSU_NMSHRENTRY = NUM_WARP`。其四态状态机：

```
S_IDLE ──(来响应 或 来新请求)──▶ S_ADD/S_OUT_1
S_ADD  : 同时接受了新请求与一个响应，先登记新表项
S_OUT_1: 读 SRAM（用 output_entry 地址读 tag+data）
S_OUT_2: 把 SRAM 读出结果送 pipe（应用 byte_extract），并释放表项
```

核心数据结构：

- `used[NMSHRENTRY]`：表项占用位图。分配时找第一个 `~used` 的表项（`valid_entry`）。
- `current_mask[NUM_THREAD*NMSHRENTRY]`：每个表项的待完成 lane 掩码。
- `complete[n]`：`current_mask[n]==0 && used[n]`，即该表项所有 lane 数据到齐。输出时找第一个 `complete` 的表项（`output_entry`）。

完成判定的数学表达（对表项 n）：

\[
\text{complete}[n] = \text{used}[n] \;\land\; \big(\text{current\_mask}[n] == 0\big)
\]

响应到达时更新待完成掩码（`inv_activemask = ~from_dcache_activemask_i`）：

\[
\text{current\_mask}[n] \leftarrow \text{current\_mask}[n] \;\land\; \neg \text{activemask}
\]

即响应里激活的 lane 表示「数据已到」，从待完成掩码中清除。

#### 4.3.3 源码精读

`complete` 判定与表项选择（优先编码）：

[mshr_reg.v:108-152](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/mshr_reg.v#L108-L152) —— `complete[n]` 由 `current_mask` 全 0 且 `used` 置位得到；`fixed_pri_arb` + `one2bin` 把 one-hot 的 complete 向量转成二进制 `output_entry`（输出表项号）；同理 `~used` 经仲裁得到 `valid_entry`（空闲表项号）。`from_addr_ready_o` 在「有空闲表项且空闲态」时才拉高（第 165 行），保证不溢出。

表项号回送给 addrcalculate作为 `instrid`：

[mshr_reg.v:166](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/mshr_reg.v#L166) —— `idx_entry_o = valid_entry`，这个号随后被 addrcalculate 当作 `to_dcache_instrid_o` / `to_shared_instrid_o` 打在每次子请求上，响应带回同样的号，MSHR 就能据此把数据写回正确表项。

响应到达时，按 `instrid` 把数据写进 data SRAM 并清待完成位：

[mshr_reg.v:268-290](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/mshr_reg.v#L268-L290) —— `S_IDLE` 态收到响应：按 `from_dcache_instrid_i` 定位表项，`current_mask[...] <= current_mask[...] & inv_activemask` 清掉已到 lane；同时把 `from_dcache_data_i` 按 `activemask` 掩码写入 data SRAM（见第 397 行 `from_dcache_mask_to_sram` 把单 lane 的 activemask 广播成 32 位写掩码）。若与此同时有新请求到达，则把请求掩码存进 `reg_req_mask`，转 `S_ADD` 登记新表项。

输出阶段（`S_OUT_1` 读 SRAM，`S_OUT_2` 送 pipe）：

[mshr_reg.v:410-419](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/mshr_reg.v#L410-L419) —— `to_pipe_valid_o` 仅在 `S_OUT_2` 且存在 complete 表项时拉高；元信息从 tag SRAM 读出切片（warp_id、reg_idxw、mask、wordoffset1h、unsigned、iswrite、wfd/wxd）；数据用 `output_data`（已经过 byte_extract）。

两块双端口 SRAM：

[mshr_reg.v:421-451](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/mshr_reg.v#L421-L451) —— `mshr_data`（位宽 `XLEN*NUM_THREAD`）与 `mshr_tag`（位宽 `4+NUM_THREAD*5+REGIDX_WIDTH+REGEXT_WIDTH+DEPTH_WARP`）都是深度 `LSU_NMSHRENTRY` 的双端口 SRAM，A 口写、B 口读。

#### 4.3.4 代码实践

**实践目标：跟踪一次 dcache 缺失后多拍响应在 MSHR 中的合并过程。**

承接 4.2.4 的例子（一条向量 load 拆成 2 次 D-cache 请求，分别覆盖 lane{0,1} 与 lane{2,3}）：

1. 在 [lsu_exe.v:297](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/lsu_exe.v#L297) 的 MSHR 实例处假设分配到表项 `entry=0`。
2. 两次子请求的 `instrid` 都 = 0，故两次响应的 `from_dcache_instrid_i` 都 = 0。
3. 第一次响应 `activemask=0011`（lane0,1）：`current_mask[0]` 从初始 `1111` 变为 `1111 & ~0011 = 1100`，data SRAM 的 lane0、1 字写入。
4. 第二次响应 `activemask=1100`（lane2,3）：`current_mask[0]` 变为 `1100 & ~1100 = 0000`，data SRAM 的 lane2、3 字写入。
5. 现在 `complete[0] = (current_mask[0]==0) && used[0] = 1`，MSHR 进入 `S_OUT_1`→`S_OUT_2`，把整条指令 4 个 lane 的数据送出。

**需要观察的现象：** 两次响应即便**乱序**返回（先 lane{2,3} 后 lane{0,1}），最终 `current_mask` 都会归零、数据正确合并到同一个表项的 data SRAM 里。

**预期结果：** MSHR 在第二个响应到达后才置 `complete`，体现「攒齐才输出」。若需确认真实波形，请在仿真中观察 `coalscer.current_mask` 与 `complete` 信号（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`LSU_NMSHRENTRY = NUM_WARP`（默认）。这意味着同一时刻最多能有多少条向量访存指令在途？如果某 warp 连续发射多条 load 会怎样？

> **参考答案**：MSHR 共 `NUM_WARP` 个表项，理论上同时可在途的向量访存指令数上限为 `NUM_WARP` 条（不限定 warp）。但实际上 addrcalculate 一次只处理一条指令、且每 warp 还有 `shiftboard`（`LSU_NUM_ENTRY_EACH_WARP=4`）限制单 warp 在途数。当 MSHR 表项全满（`&used`）时，`from_addr_ready_o` 拉低，addrcalculate 停在 `S_SAVE` 不接新请求，形成反压。

**练习 2**：为什么 MSHR 用 SRAM 而不是触发器阵列来存 tag 和 data？

> **参考答案**：data SRAM 每表项 `XLEN*NUM_THREAD` 位（如 NUM_THREAD=32 时达 1024 位），tag 也上百位，共 `NUM_WARP` 项。用触发器阵列面积代价过大；SRAM 面积更省、适合大位宽存储，代价是只能按地址端口读写（故用 `output_entry`/`valid_entry` 寻址、分 `S_OUT_1/S_OUT_2` 两拍读出）。

---

### 4.4 字节提取 byte_extract：把对齐的字节拼回 32 位

#### 4.4.1 概念说明

D-cache/共享内存返回的是一个完整的 32 位字，但一条 `LB`（字节）或 `LH`（半字）只需要其中 1 或 2 个字节，并要按有符号/无符号扩展到 32 位再写回寄存器堆。`byte_extract` 就是干这件事的单 lane 组合逻辑。它由 MSHR 在输出阶段为每个 lane 例化一个（见 [mshr_reg.v:399-404](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/mshr_reg.v#L399-L404)）。

#### 4.4.2 核心流程

输入是：`sel`（4 位的 `wordoffset1h`，来自 addrcalculate，标识要取哪些字节）、`in`（32 位原始字）、`is_uint`（无符号则零扩展）。输出 `result` 为 32 位对齐并扩展后的值。逻辑就是一张查找表：按 `sel` 选中字节位置，再按 `is_uint` 决定高位补 0 还是补符号位。

#### 4.4.3 源码精读

[byte_extract.v:24-35](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/byte_extract.v#L24-L35) —— `case(sel)`：
- `4'hf`（字）：原样输出 `in`。
- `4'h3`（低半字）/`4'hc`（高半字）：取 16 位，若 `is_uint` 或符号位为 0 则高位补 0，否则补 `16'hffff`（符号扩展）。
- `4'h1/2/4/8`（4 种字节位置）：取 8 位，同理按符号位或 `is_uint` 做符号/零扩展到 24 位高位。

注意 `sel` 的取值正是 addrcalculate 在 [addrcalculate.v:277-290](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/addrcalculate.v#L277-L290) 算出的 `wordoffset1h`，二者构成严丝合缝的「编码—解码」对。

#### 4.4.4 代码实践

**实践目标：验证 byte_extract 的符号扩展行为（源码阅读型）。**

1. 打开 [byte_extract.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/byte_extract.v)。
2. 假设 `in = 0x80FF3030`，`sel = 4'h2`（取 byte1，即 `in[15:8]=0x30`），`is_uint=0`。手算 `result`：取 `0x30`，符号位 `0x30[7]=0`，故高位补 0，得 `0x00000030`。
3. 改 `in = 0x80FF8030`，`sel = 4'h4`（取 byte2，`in[23:16]=0xFF`），`is_uint=0`：符号位 `1`，得 `0xFFFFFF FF`（即 `0xFFFFFFFF`）。
4. 同样输入但 `is_uint=1`（无符号字节加载 `LBU`）：得 `0x000000FF`。

**需要观察的现象：** `is_uint` 一位之差，决定高位补 0 还是补全 1。

**预期结果：** 有符号字节加载对负数字节做符号扩展，无符号加载恒零扩展。

#### 4.4.5 小练习与答案

**练习 1**：`sel=4'hc` 时取的是哪个半字？为什么符号判定看的是 `in[31]`？

> **参考答案**：`4'hc`（即 `4'b1100`）选中高 2 个字节，取 `in[31:16]`。这是字的高半字，其最高位是 `in[31]`，故符号扩展看 `in[31]`。

**练习 2**：为什么 `byte_extract` 不需要知道地址，只要 `sel`？

> **参考答案**：地址信息已经在 addrcalculate 阶段被「翻译」成了字节使能 `wordoffset1h`（即 `sel`）。`byte_extract` 只关心从 32 位字的哪个字节位置取数、如何扩展，不再需要原始地址，职责被干净地解耦。

---

### 4.5 写回分流 lsu2wb 与响应仲裁 rsp_arb

#### 4.5.1 概念说明

MSHR 输出的结果可能是写给标量寄存器（标量 load，`wxd`）或向量寄存器（向量 load，`wfd`），而 pipe 的写回总线（详见 u3-l1）标量、向量是分开的。`lsu2wb` 就是个**二选一开关**：按 `wxd/wfd` 把同一份结果接到对应写回端口。

另外，响应侧有 D-cache 和共享内存两个来源，`rsp_arb` 用固定优先级把它们合成一路喂给 MSHR。这两个小模块虽然简单，但补齐了 LSU 的完整通路。

#### 4.5.2 核心流程

**lsu2wb**：
- 若 `lsu_rsp_wxd_i`（写标量）：把 `out_x_*` 接通，`out_x_wb_wxd_rd = data[31:0]`（只取 lane0 的标量值），`lsu_rsp_ready = out_x_ready_i`。
- 若 `lsu_rsp_wfd_i`（写向量）：把 `out_v_*` 接通，`out_v_wb_wvd_rd = data`（全部 lane），`lsu_rsp_ready = out_v_ready_i`。
- 两者皆非（例如纯 store 完成不写寄存器）：直接丢弃，`lsu_rsp_ready=1`。

**rsp_arb**：固定优先级，D-cache 响应（in0）优先于共享内存响应（in1）。

#### 4.5.3 源码精读

[lsu2wb.v:53-88](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/lsu2wb.v#L53-L88) —— `out_x_wb_wxd_rd_o = lsu_rsp_data_i[XLEN-1:0]`（标量只取低 32 位、即 lane0），`out_v_wb_wvd_rd_o = lsu_rsp_data_i`（向量取全部 lane）。`always@(*)` 块按 `wxd/wfd` 选通 valid 与 ready。

[rsp_arb.v:41-47](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/rsp_arb.v#L41-L47) —— `in1_ready_o = !in0_valid_i && out_ready_i`：只有 D-cache 无响应时才接 shared 响应；输出字段在 `in0_valid_i` 时选 in0 否则选 in1。

在 pipe 中，`lsu2wb` 的两路输出汇入写回总线：

[pipe.v:880-891](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L880-L891) —— `lsu2wb_out_x_*` 与 `lsu2wb_out_v_*` 分别与 salu/fpu/sfu/csr/mul/tensor 等执行单元的结果拼接，进入 `writeback_in_x_*` / `writeback_in_v_*` 总线，由写回仲裁器择路写入寄存器堆。

#### 4.5.4 代码实践

**实践目标：确认 store 指令不产生写回，而 load 指令产生写回。**

1. 阅读 [lsu2wb.v:68-84](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/lsu2wb.v#L68-L84) 的 `always@(*)` 块。
2. 对一条 `VSE32_V`（向量 store）：上游译码会让 `wvd=0`、`wxd=0`，故进入 `else` 分支，`out_x_valid=0`、`out_v_valid=0`、`lsu_rsp_ready=1`——即 store 完成后**不触发任何写回**，只是默默消费掉 MSHR 的输出。
3. 对一条 `VLE32_V`（向量 load）：`wfd=1`，进入 `wfd` 分支，产生 `out_v_valid` 写回向量寄存器。

**需要观察的现象：** store 与 load 走相同的 MSHR 完成通路，但只有 load 在 `lsu2wb` 处产生有效写回 valid。

**预期结果：** store 不占用写回总线槽位，写回仲裁器看不到它。

#### 4.5.5 小练习与答案

**练习 1**：标量 load（`wxd=1`）时，`out_x_wb_wxd_rd` 取的是数据的哪一部分？为什么？

> **参考答案**：取 `lsu_rsp_data_i[XLEN-1:0]`，即 lane0 的 32 位。标量寄存器只有 32 位宽，标量 load 只关心一个 lane（lane0）的值，故只取低字。

**练习 2**：`rsp_arb` 为什么给 D-cache 响应更高优先级？会不会饿死共享内存响应？

> **参考答案**：D-cache 响应通常关联更长延迟的缺失事务、且更可能阻塞后续依赖，优先处理可尽快释放资源。由于两边响应各自带 valid/ready 握手、且 MSHR 在 `S_IDLE` 态按拍接收，只要 `out_ready_i` 持续有效，共享内存响应在 D-cache 无响应的拍就会被接走，不会永久饿死。

---

## 5. 综合实践

**任务：跟踪一条 `VLE32_V`（向量 load）指令在 LSU 中的完整生命周期，画出端到端时序图。**

以 `NUM_THREAD=4`、`NUM_WARP=4`、`DCACHE_BLOCKWORDS=2` 为默认配置，按以下步骤把本讲五个最小模块串起来：

1. **入口**：`pipe.issue` 发出 `issue_out_LSU_valid` 与 `vExeData`（含 `in1`=基址、`mask`=激活掩码）。在 [pipe.v:1683-1688](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1683-L1688) 确认连线。
2. **缓冲**：请求进 `input_fifo`，握手后送达 addrcalculate（[lsu_exe.v:142](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/lsu_exe.v#L142)）。
3. **地址计算与分流**：addrcalculate 逐 lane 算地址，判定 shared/dcache，申请 MSHR 表项 `entry=0`，把元信息写入 MSHR tag SRAM（[addrcalculate.v:175-211](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/addrcalculate.v#L175-L211)）。假设 4 个 lane 落在 2 个 cache 块，分 2 次发 D-cache 请求，`instrid` 均 = 0（[addrcalculate.v:423-429](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/addrcalculate.v#L423-L429)）。
4. **响应合并**：两次 D-cache 响应经 `rsp_arb` 进入 MSHR，分别清 `current_mask[0]` 的 lane{0,1} 与 lane{2,3}，数据按掩码写入 data SRAM（[mshr_reg.v:268-290](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/mshr_reg.v#L268-L290)）。
5. **完成输出**：`current_mask[0]==0` → `complete[0]=1` → `S_OUT_1` 读 SRAM、`S_OUT_2` 输出（[mshr_reg.v:410-419](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/mshr_reg.v#L410-L419)）。每个 lane 经 `byte_extract` 对齐（对 `VLE32_V` 是字加载，`sel=4'hf`，原样输出）。
6. **写回**：`lsu2wb` 因 `wfd=1` 把 4 lane 数据接到 `out_v`，汇入向量写回总线写入目的向量寄存器（[lsu2wb.v:57-61](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/lsu2wb.v#L57-L61)、[pipe.v:886-891](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L886-L891)）。

**交付物：** 一张含「issue → fifo → addrcalculate（多拍分组）→ dcache（2 次）→ rsp_arb → mshr（合并 2 次响应、complete）→ byte_extract → lsu2wb → 写回」的时序/框图，并标注 `instrid=0` 如何贯穿请求与响应。

**进阶（可选）：** 若有 VCS 环境，进入 `testcase/test_gpgpu_axi_top/tc_vecadd`（一个含向量访存的用例，参见 u1-l4），用 `make run-vcs-4w4t` 跑通后，`make verdi` 打开 `test.fsdb`，在 `lsu_exe` 内观察 `addr_mshr_valid`、`dcache_req_instrid_o`、`arb_mshr_instrid`、`coalscer.complete`、`lsu_rsp_valid_o` 等信号，验证你画的时序图与真实波形一致。若暂无仿真环境，标注「待本地验证」即可。

## 6. 本讲小结

- LSU 是一条「**拆开—发出—合并—写回**」的单向流水线，顶层 `lsu_exe` 只做例化与连线，请求侧主角是 addrcalculate，响应侧主角是 MSHR。
- **addrcalculate** 为每个 lane 独立计算地址，用 `addr < SHAREMEM_SIZE` 分流共享内存/D-cache，按 tag 分组、靠 `mask_next` 逐组清空掩码循环发出；访存宽度 `MEM_W/H/B` 被翻译成 4 位字节使能 `wordoffset1h`。
- **LSU MSHR（coalscer）** 用两块 SRAM（tag + data）+ `current_mask` 待完成位图，把一条向量访存拆出的多个子请求（共用同一 `instrid`）的乱序响应合并；当某表项 `current_mask==0` 即 `complete`，整条指令一次性送出。
- **byte_extract** 按 `wordoffset1h` 从 32 位字中选出字节/半字并做符号/零扩展，与 addrcalculate 的 `wordoffset1h` 构成编解码对。
- **lsu2wb** 按 `wxd/wfd` 把结果分流到标量/向量写回总线，store 不产生写回；**rsp_arb** 给 D-cache 响应固定高优先级；**shiftboard** 按 warp 统计在途请求供 fence 判定完成。
- 贯穿全讲的身份编号是 **`instrid`（=MSHR 表项号）**：它由 MSHR 分配、被 addrcalculate 打在每次子请求上、随响应原样带回，是「请求-响应」对应关系的纽带。

## 7. 下一步学习建议

- **u6-l1（dcache 与 MSHR）**：本讲的 D-cache 是 LSU 的下游。建议接着学 D-cache 内部的控制状态机、tag 检查与它自己的 MSHR（注意区分 LSU MSHR 与 D-cache MSHR 两层）。
- **u6-l2（共享内存）**：了解 LSU 的另一条下游——共享内存的多 bank 组织与 bank 冲突仲裁，理解 `shared_req_*` 接口背后是什么。
- **u6-l3（L1 cache 仲裁）**：看 icache/dcache/shared 三路请求如何经 `l1cache_arb` 统一出口，把本讲的 `dcache_req`/`shared_req` 放回 SM 对外存储接口的全景。
- **源码延伸阅读**：可对照 [mshr_backup.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/mshr_backup.v)（LSU 中注释掉的备用 MSHR 实现）与当前 `mshr_reg.v` 对比，理解设计演进。
