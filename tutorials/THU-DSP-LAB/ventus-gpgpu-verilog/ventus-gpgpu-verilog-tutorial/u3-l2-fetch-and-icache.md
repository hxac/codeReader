# 取指与指令缓存 icache

## 1. 本讲目标

本讲聚焦 SM 流水线的最前端——**取指（Instruction Fetch）**。学完本讲你应当能够：

- 说清楚一条取指请求从 `warp_scheduler` 发出，到 `instruction_cache` 查询、命中或缺失，再到结果回送的全过程；
- 用「组（set）× 路（way）× 块字数」描述 icache 的几何结构，并把一个 32 位地址正确拆成 tag / set / block offset / word offset；
- 解释 `tag_access_icache` 如何读 tag、`tag_checker_icache` 如何判定命中与命中路；
- 解释 `mshr_icache` 如何用「主缺失 / 次缺失 + 子项」结构合并对同一块的多次缺失，并在数据返回后让重发的取指命中；
- 理解 `NUM_FETCH` 这个取指宽度参数如何同时影响「一次取几条指令」和「一个 cache 块装几条指令」。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**（1）谁在取指？** GPU 的执行单位是 warp（见 u2-l3）。每个 warp 有自己的 PC，由 `warp_scheduler` 维护并驱动取指。所以「取指请求」不是 CPU 里那种单一 PC 流，而是「某个 wid（warp 编号）按它的 PC 去取若干条指令」。

**（2）为什么需要 icache？** 若每次取指都去片外内存搬指令，延迟极高。icache 把最近用过的指令块缓存在 SM 内部，命中时一拍出数据，缺失时才向 L2 发请求。这和 dcache（见 u6-l1）思路一致，只是 icache 只读不写、行为更简单。

**（3）什么是「组相联」与「MSHR」？**

- *组相联（set-associative）*：把缓存分成若干「组」，每组有若干「路」。一个内存块只能落在某一组里（由地址的 set 字段决定），但可以放在该组的任意一路上。本讲 icache 是 32 组 × 2 路。
- *MSHR（Miss Status Holding Register）*：缓存缺失后，需要等内存把数据送回来。这期间如果又有请求来要**同一块**，不能重复向内存发请求，得把后来者「挂」在同一个缺失项上一起等。MSHR 就是记录「哪些请求在等哪一块」的表。本讲用 `mshr_icache` 实现它。

> 数学提示：地址各字段位宽都用 `\$clog2` 派生。若一组有 \(N\) 路、\(S\) 组、每块 \(W\) 个字、每字 \(B\) 字节，则 set 索引位宽 \(\lceil\log_2 S\rceil\)、block offset 位宽 \(\lceil\log_2 W\rceil\)、word offset 位宽 \(\lceil\log_2 B\rceil\)，tag 占剩余高位。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [instruction_cache.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v) | icache 顶层。把取指请求接到 tag/data SRAM，命中出数据、缺失交 MSHR，是本讲的「主板」。 |
| [tag_access_icache.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/tag_access_icache.v) | 维护 tag 体、way 有效位、每组的 LRU 替换矩阵，并例化 tag 检查器。 |
| [tag_checker_icache.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/tag_checker_icache.v) | 纯组合逻辑：把当前请求的 tag 与该组各路 tag 比较，得出「是否命中 / 命中哪一路」。 |
| [mshr_icache.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/mshr_icache.v) | 缺失状态表。区分主/次缺失，向内存发请求，数据返回后驱动回填。 |
| [get_setid.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/get_setid.v) | 小工具：从地址或块地址里抠出 set 索引位。 |

此外两个「连接点」也会用到（不精读，只看接线）：

- [pipe.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v)：`warp_scheduler` 在此例化，发出 `icache_req_*`、接收 `icache_rsp_*`。
- [sm_wrapper.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v)：在这里例化 `instruction_cache`，把它接到 `pipe_icache_req_*` 与对外存储接口（`l1cache_arb` 或 `NO_CACHE` 直连）。
- [define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v)：提供 `NUM_FETCH` 与全部 `DCACHE_*` 几何参数。

---

## 4. 核心概念与源码讲解

### 4.1 NUM_FETCH 取指宽度与取指请求的发起

#### 4.1.1 概念说明

「一次取几条指令」是 icache 设计的总开关。Ventus 用宏 `NUM_FETCH` 定义它：

[define.v:L19](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L19)

```verilog
`define NUM_FETCH 2 //a fetch refers to the number of instructions, should be power of 2
```

`NUM_FETCH=2` 意味着每次取指搬 **2 条 32 位指令**。它必须是 2 的幂，这样一次取指的指令总数对应一个连续对齐的块。配套的对齐宏是：

[define.v:L59](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L59)

```verilog
`define ICACHE_ALIGN `NUM_FETCH * 4   // = 8 字节
```

即取指地址按 8 字节对齐——恰好等于一个 cache 块的大小（见 4.2 的几何推导）。这就是 `NUM_FETCH` 的双重身份：它既决定「一次取 2 条指令」，又决定「一个 cache 块装 2 条指令」，二者被刻意设计成一致，使一次取指恰好命中一个块、一次缺失填充也恰好满足一次取指。

#### 4.1.2 核心流程

取指请求由 `warp_scheduler` 发起，其接口在 `pipe.v` 中声明：

[pipe.v:L26-L36](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L26-L36)

```verilog
output                        icache_req_valid_o,
output [`XLEN-1:0]            icache_req_addr_o,
output [`NUM_FETCH-1:0]       icache_req_mask_o,   // 2 位：本次取回的 2 条指令各自是否有效
output [`DEPTH_WARP-1:0]      icache_req_wid_o,
...
input                         icache_rsp_status_i,  // 0 = hit, 1 = miss
```

注意 `icache_req_mask_o` 是 `NUM_FETCH` 位宽——每一位对应一条指令是否有效（例如取指到一段代码末尾时，可能只有第一条有效）。`warp_scheduler` 在 `pipe.v` 中例化，把它的 `pc_req_*` 输出连到上述端口：

[pipe.v:L915-L924](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L915-L924)

请求/响应的握手关系可概括为：

1. `warp_scheduler` 选出一个就绪 warp，按其 PC 发出 `{valid, addr, mask, wid}`；
2. icache 一拍读 tag、一拍判定（见 4.2），回送 `{valid, data(2条指令), mask, wid, status}`；
3. `status=0`（命中）→ 指令进入 ibuffer；`status=1`（缺失）→ `warp_scheduler` **保持 PC、重发取指**，等块填充后再命中。

#### 4.1.3 源码精读：响应侧如何区分命中/缺失

在 `pipe.v` 里，icache 响应被这样消费：

[pipe.v:L706-L714](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L706-L714)

```verilog
assign {decode_in_inst1,decode_in_inst0} = icache_rsp_data_i;          // 2 条指令
assign warp_sche_status = ibuffer_in_ready ? icache_rsp_status_i : 1'b1; // ibuffer 满则伪装 miss
assign {decode_inst_mask_1,decode_inst_mask_0} = (icache_rsp_valid_i && (!icache_rsp_status_i)) ? icache_rsp_mask_i : 'b0;
assign ibuffer_in_valid = icache_rsp_valid_i && (!icache_rsp_status_i);  // 只有命中才进 ibuffer
```

这段揭示了两个关键设计（承接 u3-l1）：

- **只有命中（`status==0`）的指令才会送进 ibuffer**；缺失响应被丢弃，由 `warp_scheduler` 重发。
- **ibuffer 满时 `warp_sche_status` 被强行置 1（伪装缺失）**，从而反压取指——这是流水线前端最简单有效的反压手段。

#### 4.1.4 代码实践

**目标**：确认 `NUM_FETCH` 在整条取指通路里的一致性。

1. 在 [define.v:L19](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L19) 读到 `NUM_FETCH=2`；
2. 在 [pipe.v:L28](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L28) 看到 `icache_req_mask_o` 位宽为 `NUM_FETCH`，[pipe.v:L33](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L33) 看到 `icache_rsp_data_i` 位宽为 `NUM_FETCH*XLEN`；
3. 在 [instruction_cache.v:L33](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L33) 看到 `core_rsp_data_o` 同样是 `NUM_FETCH*XLEN`。

**观察/预期**：三处位宽都随 `NUM_FETCH` 派生。若把 `NUM_FETCH` 改为 4（同时确认 kernel 二进制按新宽度重排，见 u1-l3 关于 VLEN 的约束），这三处会自动变宽；但要注意 `NUM_FETCH * 4` 必须仍等于一个 cache 块的字节数，否则掩码与块对齐会错位（这点留到 4.2 验证）。**待本地验证**：改动后能否仍跑通 `tc_vecadd`。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `NUM_FETCH` 必须是 2 的幂？
  - **答**：取指地址要按 `NUM_FETCH*4` 字节对齐，掩码也要按位对应每条指令。2 的幂才能用简单的位拼接和地址低位对齐来实现，避免跨块取指。
- **练习 2**：`icache_req_mask_o` 何时会出现「只有第 1 位有效、第 0 位无效」的情况？
  - **答**：当取指地址恰好对应一个块、但程序在该块的前半字处已经结束（如代码段末尾），或因对齐只能取到块内第二条指令时，掩码会标记某条指令无效。

---

### 4.2 instruction_cache：取指缓存顶层

#### 4.2.1 概念说明

`instruction_cache` 是 icache 的顶层模块，自己不做复杂运算，负责把取指请求调度到 tag/data SRAM、判定命中、把缺失交给 MSHR、把命中数据/状态回送。它的接口分三类：**core 侧**（对 `warp_scheduler`）、**mem 侧**（对 L2/`l1cache_arb`）、**控制侧**（`invalid_i` 刷新、`flush_pipe_*` 冲刷）。

[instruction_cache.v:L17-L49](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L17-L49)

#### 4.2.2 核心流程：两拍流水 + 命中/缺失分流

icache 内部是一条极简的两级流水（`st0` → `st1`，部分信号再打到 `st2/st3`）：

```
st0: core_req_addr 进来
     ├─ 用 set 位 → 同时读 tag RAM（tagAccess）和 data RAM（dataAccess）
     └─ 记录 wid/addr/mask 到 st1 寄存器
st1: tag 检查结果出来
     ├─ 命中: 用 wayid_hit 从 dataAccess 结果里选出该路的数据块
     └─ 缺失: cacheMiss_st1=1 → 交给 mshrAccess 登记并请求内存
st2: 把 {data, mask, wid, addr, status} 回送给 core
```

几个关键派生（先看地址怎么切）：

[instruction_cache.v:L51-L53](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L51-L53)

```verilog
localparam BLOCK_BITS = `DCACHE_BLOCKWORDS*32,                              // 一个块的数据宽度 = 2*32 = 64 bit
           BA_BITS    = `DCACHE_TAGBITS+`DCACHE_SETIDXBITS,                 // 块地址位宽 = 24+5 = 29
           FIFO_BITS  = `DEPTH_WARP+`XLEN+(`DCACHE_BLOCKWORDS*`XLEN);       // memRsp FIFO 数据宽
```

把这些宏展开（依据 [define.v:L69-L105](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L69-L105)），得到 icache 的完整几何：

| 参数 | 宏 | 值（默认配置） | 含义 |
|------|-----|------|------|
| 组数 | `DCACHE_NSETS` | 32 | set 数 |
| 路数 | `DCACHE_NWAYS` | 2 | 每组 way 数（2 路组相联） |
| 块字数 | `DCACHE_BLOCKWORDS` | 2 | 每块含几个 32 位字（I/D 共用） |
| 字字节 | `BYTESOFWORD` | 4 | 每字 4 字节 |
| set 位 | `DCACHE_SETIDXBITS` | 5 | `\$clog2(32)` |
| way 位 | `DCACHE_WAYIDXBITS` | 1 | `\$clog2(2)` |
| 字偏移位 | `DCACHE_WORDOFFSETBITS` | 2 | `\$clog2(4)`（字节选字） |
| 块偏移位 | `DCACHE_BLOCKOFFSETBITS` | 1 | `\$clog2(2)`（块内选字） |
| tag 位 | `DCACHE_TAGBITS` | 24 | `32-(5+1+2)` |

于是 32 位地址切分为：

```
[ tag (24) | set (5) | blockoffset (1) | wordoffset (2) ]
```

一个 cache 块 = `DCACHE_BLOCKWORDS × 32 bit` = 64 bit = **2 条指令**，正好 = `NUM_FETCH`。这就是「一次取指 = 一个块」的几何根源。

#### 4.2.3 源码精读

**(a) 命中/缺失判定与数据选择**

[instruction_cache.v:L150-L170](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L150-L170)

```verilog
assign cacheMiss_st1 = (!tagAccess_hit_st1 && core_req_fire_st1);   // 未命中且请求有效
...
assign tagAccess_r_req_valid = core_req_valid_i && (!shouldFlushCoreRsp_st0);  // st0 读 tag
assign tagAccess_tagFromCore_st1 = addr_st1 >> (`XLEN-`DCACHE_TAGBITS);        // st1: 取高 24 位当 tag
...
assign dataAccess_r_req_valid = core_req_valid_i && !shouldFlushCoreRsp_st0;   // st0 读 data
assign dataAccess_w_req_data  = {`DCACHE_NWAYS{mem_rsp_d_data_o}};             // 回填：两路都备好，由 waymask 选
assign data_after_wayid_st1   = dataAccess_data[(wayid_hit_st1+1)*BLOCK_BITS-1 -: BLOCK_BITS]; // 命中路的数据
```

`data_after_wayid_st1` 用命中路号 `wayid_hit_st1` 从 `DCACHE_NWAYS` 路并行读出的数据里**选出命中那一路的整块数据**（64 bit = 2 条指令）。

**(b) 回送 core 响应与 status**

[instruction_cache.v:L173-L183](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L173-L183)

```verilog
assign core_rsp_valid_o = core_req_fire_st2;
assign core_rsp_data_o  = data_after_wayid_st2;
assign core_rsp_wid_o   = wid_st2;
...
assign status_st1 = shouldFlushCoreRsp_st0_r ? 1'b0 : cacheMiss_st1;  // 被冲刷的请求强制 status=0
assign status_st2 = shouldFlushCoreRsp_st1_r ? 1'b0 : status_st1_r;
assign core_rsp_status_o = status_st2;                                // 0=hit, 1=miss
```

注意 `core_rsp_status_o` 的语义：**0 表示命中、1 表示缺失**（见端口注释 [instruction_cache.v:L36](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L36)）。被 `flush_pipe_*` 冲刷的请求，其 status 被强制改成 0——意思是「当作命中但数据无意义」，配合上游丢弃，避免误触发缺失处理。

**(c) 冲刷（flush）机制**

[instruction_cache.v:L152-L155](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L152-L155)

```verilog
assign shouldFlushCoreRsp_st0 = (core_req_wid_i == flush_pipe_wid_i) && flush_pipe_valid_i;
assign shouldFlushCoreRsp_st1 = (wid_st1         == flush_pipe_wid_i) && flush_pipe_valid_i;
```

当某 warp 发生分支跳转或结束（见 u5-l2/u5-l3 的 `branch_back`/`flush`），`flush_pipe_valid` 拉起并指明 wid。icache 把该 wid **正在流水里的取指请求作废**：既不让它读 tag（`tagAccess_r_req_valid` 被屏蔽），也不让它登记缺失（`cacheMiss_st1` 因 `core_req_fire_st1=0` 而为 0）。

**(d) 防重复登记：order_violation**

[instruction_cache.v:L189-L190](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L189-L190)

```verilog
assign order_violation_st1 = ((wid_st1 == wid_st2) && cacheMiss_st2 && !order_violation_st2) ||
                              ((wid_st1 == wid_st3) && cacheMiss_st3 && !order_violation_st3);
```

它检测「同一 wid 的更早取指还在 st2/st3 缺失中」。其设计意图是避免同一 warp 的连续重发被重复当缺失处理。注意：当前 `core_rsp_valid_o` 的赋值（[L173](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L173)）并未使用 `order_violation`（带 `!order_violation` 的旧写法在 [L172](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L172) 被注释掉了），该信号目前是「算出来但暂不 gating」的预留逻辑——读源码时要意识到这一点，不要误以为它已生效。

#### 4.2.4 代码实践

**目标**：在顶层把请求/回填两条数据通路找出来。

1. 跟踪 **命中通路**：`core_req_addr_i` → `get_setid_tagAccess_r_req`（[L192-L202](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L192-L202)）算 set → `dataAccess`（[L245-L261](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L245-L261)）读出所有路 → `data_after_wayid_st1`（[L170](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L170)）选路 → `core_rsp_data_o`（[L175](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L175)）。
2. 跟踪 **回填通路**：`mem_rsp_d_data_i` → `stream_fifo memRsp`（[L288-L300](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L288-L300)）→ `memRsp_fire` 同时触发 `dataAccess` 写（[L257-L260](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L257-L260)）和 `tagAccess` 写（[L229-L231](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L229-L231)）。

**预期**：`memRsp_fire` 一拍内同时驱动 tag 与 data 两块 SRAM 的写，写入的 set 由 `get_setid_tagAccess_w_req` 从「块地址」算出（[L204-L214](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L204-L214)），写入的 way 由 LRU 替换结果 `wayid_replace_st0_one` 决定（见 4.3）。

#### 4.2.5 小练习与答案

- **练习 1**：顶层没有 `core_req_ready_o`（被注释，见 [L22](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L22)/[L147](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L147)）。这意味着 icache 永远接收请求，那么 MSHR 满了怎么办？
  - **答**：MSHR 满时（`entry_full && primary_miss`）`miss_req_ready=0`，本次缺失**不会被登记、也不会发往内存**，但顶层仍按缺失回送 `status=1`；`warp_scheduler` 收到 miss 后会重发取指，等到 MSHR 释放并填满该块，重发即命中。这是一种「靠重试自然等待」的设计。
- **练习 2**：为什么 `dataAccess_w_req_data` 要把回填数据复制成 `{DCACHE_NWAYS{...}}`？
  - **答**：data SRAM 是按「set × way」组织的，写时只应写命中/替换的那一路。把同一数据铺到所有路、再用 `w_req_waymask_i`（`wayid_replace_st0_one`，独热）只使能一路，是 SRAM 模板的标准「选路写」用法。

---

### 4.3 tag_access_icache + tag_checker_icache：组相联与命中判定

#### 4.3.1 概念说明

`tag_access_icache` 是 tag 体的「管家」：它存每路每组的 tag、维护每路的有效位、用每组的 LRU 矩阵给出「下次该替换哪一路」，并例化纯组合的 `tag_checker_icache` 做 tag 比较得出命中。

#### 4.3.2 核心流程

```
st0: r_req_valid + setid → sram_template(tagBodyAccess) 读出该组所有路的 tag
st1: tag_checker_icache 把「该组各路 tag」与「请求 tag(tagFromCore_st1)」逐路比较
     ├─ 任一路相等且该路有效 → cache_hit=1，输出命中路号 wayid_hit(独热→二进制)
     └─ 否则 cache_hit=0
替换: 每组一个 lru_matrix，命中或回填时更新；平时输出 wayid_replacement 给回填用
```

#### 4.3.3 源码精读

**(a) tag 体与有效位**

tag 体由 `sram_template` 实现（参数化 SRAM，1 拍读延迟，故读请求在 st0、结果在 st1 可用）：

[tag_access_icache.v:L122-L138](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/tag_access_icache.v#L122-L138)

每路有效位 `way_valid` 在回填时置位、在 `invalid_i`（整表刷新，对应 u1-l5 的 `cache_invalid`）时清零：

[tag_access_icache.v:L75-L88](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/tag_access_icache.v#L75-L88)

```verilog
else if(w_req_valid_i) begin
  way_valid[(w_req_setid_i*NUM_WAY)+wayid_replacement_o[...]] <= 'h1;  // 回填：标记替换路有效
end
else if(invalid_i) begin
  way_valid <= 'h0;                                                    // 整表失效
end
```

**(b) tag 检查器（纯组合比较）**

[tag_checker_icache.v:L31-L37](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/tag_checker_icache.v#L31-L37)

```verilog
generate for(i=0;i<NUM_WAY;i=i+1) begin:B1
  assign wayid_oh[i] = (r_req_valid_i
                        && (tag_of_set_i[TAG_WIDTH*(i+1)-1 -: TAG_WIDTH] == tag_from_pipe_i)
                        && way_valid_i[i]) ? 'h1 : 'h0;
end endgenerate
assign cache_hit_o = |wayid_oh;     // 任一路命中即命中
```

逻辑直白：对每一路，**当请求有效 + tag 相等 + 该路有效** 时该路命中位拉高；任一路命中则 `cache_hit_o=1`。独热的 `wayid_oh` 再经 `one2bin` 转成二进制 `wayid_o` 供顶层选数据（[L39-L45](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/tag_checker_icache.v#L39-L45)）。

> 注意三处 `tag_checker_icache` 的连接：`tag_of_set_i` 是 st1 的 tag 体读出，`tag_from_pipe_i` 是 `tagAccess_tagFromCore_st1`（地址高 24 位，在 [instruction_cache.v:L156](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L156) 算出），`way_valid_i` 是当前组的有效位（[tag_access_icache.v:L148](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/tag_access_icache.v#L148)）。三者齐全才能比较。

**(c) LRU 替换矩阵**

[tag_access_icache.v:L90-L120](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/tag_access_icache.v#L90-L120)

```verilog
generate for(i=0;i<NUM_SET;i=i+1) begin:B1
  assign lru_valid[i] = ((hit_st1_o && (i==r_req_setid_i_r)) || (w_req_valid_i && (i==w_req_setid_i))) ? 1'h1 : 1'h0;
  assign lru_update_index[...] = (hit && ...) ? wayid_hit_st1_o : (w_req && ...) ? wayid_replacement_o[...] : 'h0;
  lru_matrix #(.NUM_WAY(NUM_WAY), .WAY_DEPTH(WAY_DEPTH)) replacement (
    .update_entry_i(lru_valid[i]),
    .update_index_i(lru_update_index[...]),
    .lru_index_o   (wayid_replacement_o[...])   // 该组最久未用的路
  );
end endgenerate
```

每组一个 `lru_matrix`：**命中或回填时都更新该组的 LRU 状态**，并持续输出 `wayid_replacement_o`（每组一段）作为「下次缺失该替换的路」。顶层在回填时取对应组的 `wayid_replace_st0`（[instruction_cache.v:L104](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L104)），转独热后作为 data/tag SRAM 的写 waymask。

#### 4.3.4 代码实践

**目标**：动手核对一次命中的判定条件。

1. 设想一个请求地址 `0x0000_0040`。按 `[tag(24)|set(5)|bo(1)|wo(2)]` 拆分：低 2 位 `wo=00`，第 2 位 `bo=0`，第 3–7 位 `set = 0x40>>3 = 0x8`，高 24 位为 tag。
2. 在 [tag_checker_icache.v:L33](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/tag_checker_icache.v#L33) 确认：只有当某路的 `tag_of_set` 等于该 tag **且** `way_valid_i[i]=1` 时才命中。
3. 在 [tag_access_icache.v:L80](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/tag_access_icache.v#L80) 确认：`way_valid` 只在回填（`w_req_valid_i`）时才被置 1，复位或 `invalid_i` 时为 0。

**预期/观察**：冷启动后所有 `way_valid=0`，因此**第一次取任何地址必缺失**；只有经回填后对应路才可能命中。这正是「冷缺失」的物理解释。

#### 4.3.5 小练习与答案

- **练习 1**：2 路组相联下，同一 set 连续访问 3 个不同 tag 会发生什么？
  - **答**：第 1 个 tag 缺失→填入路 A；第 2 个 tag 缺失→填入路 B；第 3 个 tag 缺失→组已满，LRU 选出最久未用的路（路 A）替换掉。
- **练习 2**：`tag_checker_icache` 为什么是纯组合逻辑、没有时钟？
  - **答**：它只做「同一时刻的 tag 比较」，输入 `tag_of_set` 已是 st1 的寄存器输出，比较结果直接供 st1 使用，无需再寄存一拍。

---

### 4.4 mshr_icache：缺失处理与重发命中

#### 4.4.1 概念说明

`mshr_icache` 解决「缺失后怎么办」。它的核心是一张二维表：**若干 entry（主缺失项），每个 entry 下若干 subentry（次缺失挂载点）**。一个 entry 跟踪一个「正在被取的块」；对该块后续的缺失不再发内存请求，只追加一个 subentry；内存返回后，该 entry 把数据回填进 cache，并一次性释放所有挂在它下面的 subentry。等 `warp_scheduler` 重发取指时，块已在 cache 中，自然命中。

容量参数（[define.v:L89-L105](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L89-L105)）：`DCACHE_MSHRENTRY=4` 个 entry，每个 entry 下 `DCACHE_MSHRSUBENTRY=2` 个 subentry。

#### 4.4.2 核心流程

```
缺失请求进来 (miss_req_*)
 ├─ 主缺失(primary miss): 该块无 entry → get_entry_status 找空 entry_next
 │    ├─ 写 blockAddr_Access[entry_next] = 块地址
 │    ├─ 写 targetInfo_Access[subentry 0] = {wid, 低位地址}
 │    └─ 置 subentry_valid[entry_next][0]=1；has_send2mem 触发 → miss2mem_* 向内存发请求
 ├─ 次缺失(secondary miss): 该块已有 entry → 在该 entry 下找空 subentry 追加 targetInfo
 │
内存响应回来 (miss_rsp_in_*, 经 stream_fifo)
 ├─ 用块地址匹配 entryMatchMissRsp 找到对应 entry
 ├─ 输出 miss_rsp_out_block_addr → 驱动顶层回填 tag/data SRAM
 └─ miss_rsp_out_fire 后清空该 entry 全部 subentry_valid，回收 entry
```

#### 4.4.3 源码精读

**(a) 主/次缺失判定**

[mshr_icache.v:L185-L189](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/mshr_icache.v#L185-L189)

```verilog
assign primary_miss   = !secondary_miss;
assign secondary_miss = |entryMatchMissReq;          // 已有同块 entry 在跟踪
assign entry_id       = secondary_miss ? entryMatchMissReq_bin : entry_next;
assign subentry_id    = secondary_miss ? subentry_next_req : 'h0;   // 主缺失总是写到 subentry 0
```

`entryMatchMissReq[i]`（[L119](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/mshr_icache.v#L119)）把每个 entry 的块地址与当前请求块地址比较：若任一 entry 命中且有效，即为次缺失。主缺失写入新 entry 的 subentry 0；次缺失写入已存在 entry 的下一个空 subentry。

**(b) 向内存发请求（每 entry 只发一次）**

[mshr_icache.v:L194-L196](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/mshr_icache.v#L194-L196)

```verilog
assign miss2mem_valid_o      = !has_send2mem[hasSendStatus_next] && entry_valid[hasSendStatus_next];
assign miss2mem_block_addr_o = blockAddr_Access[(BA_BITS*(hasSendStatus_next+1)-1)-:BA_BITS];
assign miss2mem_instr_id_o   = targetInfo_Access[(TI_WIDTH*(hasSendStatus_next+1)-1)-:TI_WIDTH];
```

`has_send2mem[i]` 标记该 entry 是否已向内存发过请求（[L147-L160](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/mshr_icache.v#L147-L160)），确保**同一块只发一次**。`miss2mem_instr_id_o` 即顶层 `mem_req_a_source_o`（[instruction_cache.v:L285/L47-L48](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L285)），携带请求方身份（wid 等 target_info），供内存响应路由回来。

发出的地址是「块地址补零」到 32 位：

[instruction_cache.v:L164](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L164)

```verilog
assign mem_req_a_addr_o = {mshrAccess_miss2mem_block_addr,{(32-BA_BITS){1'h0}}};  // 块地址补零
```

**(c) 响应匹配与回填驱动**

[mshr_icache.v:L117/L191-L198](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/mshr_icache.v#L191-L198)

```verilog
assign entryMatchMissRsp[i] = (blockAddr_Access[...] == miss_rsp_in_block_addr_i) && entry_valid[i];
assign miss_rsp_in_ready_o  = miss_rsp_out_ready_i;
assign miss_rsp_out_valid   = miss_rsp_in_fire;
assign miss_rsp_out_block_addr_o = blockAddr_Access[(BA_BITS*(entryMatchMissRsp_bin+1)-1)-:BA_BITS];
```

内存响应（经顶层 `stream_fifo memRsp` 削峰，[instruction_cache.v:L288-L300](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L288-L300)）进来后，按块地址匹配到 entry，输出 `miss_rsp_out_block_addr`；顶层据此算 set、连同 `mem_rsp_d_data_o` 写回 data RAM 与 tag RAM（见 4.2.3 的回填通路）。`miss_rsp_out_fire` 后该 entry 的所有 `subentry_valid` 被清零（[L174-L176](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/mshr_icache.v#L174-L176)），entry 回收。

**(d) 反压与满判**

[mshr_icache.v:L202](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/mshr_icache.v#L202)

```verilog
assign miss_req_ready = !((entry_full && primary_miss) || (subentry_full_req && secondary_miss) || ReqConflictWithRsp);
```

主缺失但 entry 满、或次缺失但该 entry 的 subentry 满、或与正在回来的响应冲突时，本次缺失不被登记（`miss_req_ready=0`）。配合顶层「无 core 反压 + 报 miss 重发」的设计，请求方会重试直到 MSHR 有空位。

#### 4.4.4 代码实践

**目标**：把「缺失→发内存→回填→重发命中」四步在源码里串起来。

1. **缺失登记**：[instruction_cache.v:L150](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L150) `cacheMiss_st1` → [mshr_icache.v:L185-L189](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/mshr_icache.v#L185-L189) 主缺失分配 entry；
2. **发内存**：[mshr_icache.v:L194-L196](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/mshr_icache.v#L194-L196) `miss2mem_valid_o` → [instruction_cache.v:L46-L48](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L46-L48) `mem_req_valid_o / mem_req_a_addr_o / mem_req_a_source_o`；
3. **回填**：响应经 `stream_fifo memRsp`（[L288-L300](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L288-L300)）→ `mshrAccess_miss_rsp_in_*` 匹配 entry → `miss_rsp_out_block_addr_o` → `memRsp_fire` 同时写 data RAM（[L257-L260](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L257-L260)）与 tag RAM（[L229-L231](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L229-L231)），并置 `way_valid`；
4. **重发命中**：顶层把 `status=1` 回送（[L183](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/icache/instruction_cache.v#L183)）→ `warp_scheduler` 保持 PC 重发 → 此时 tag 命中、`status=0` → 指令进 ibuffer。

**预期**：一次冷缺失应观察到 `mem_req_valid_o` 仅在 `has_send2mem` 置位期间有效一次；块返回后 `core_rsp_status_o` 在重发周期回到 0。**待本地验证**：在 Verdi 里把 `icache.cacheMiss_st1`、`mshrAccess.miss2mem_valid_o`、`memRsp_fire`、`icache_core_rsp_status` 加到波形，跑 `tc_vecadd` 观察这四个信号的先后顺序。

#### 4.4.5 小练习与答案

- **练习 1**：主缺失与次缺失在「是否向内存发请求」上有何区别？
  - **答**：主缺失会（通过 `has_send2mem` 置位后）向内存发一次请求；次缺失不再发请求，只在该 entry 下追加一个 subentry，复用同一次 fill 的结果。
- **练习 2**：`DCACHE_MSHRENTRY=4`、`DCACHE_MSHRSUBENTRY=2` 限制了什么？
  - **答**：最多同时跟踪 4 个不同块的缺失；每个缺失块最多挂 2 个等待的子请求。超过则 `miss_req_ready=0`，靠上游重发等待。这决定了 icache 能容忍的「同时在途缺失并发度」。

---

## 5. 综合实践

**任务**：画出一张完整的「取指请求生命周期图」，把本讲四个模块串起来。

要求：

1. 从 `warp_scheduler` 发出 `icache_req_*` 开始（[pipe.v:L915-L918](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L915-L918)），到 `sm_wrapper` 例化 `instruction_cache`（[sm_wrapper.v:L465-L496](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v#L465-L496)）；
2. 在图上标出 **st0（读 tag/data SRAM）** 与 **st1（tag 检查 + 命中/缺失分流）** 两个节拍；
3. 画出两条分支：
   - **命中分支**：`tag_checker_icache` 命中 → `data_after_wayid_st1` 选路 → `core_rsp_data_o`（status=0）→ ibuffer；
   - **缺失分支**：`cacheMiss_st1` → `mshr_icache` 主缺失登记 → `miss2mem` 发请求 → `stream_fifo memRsp` 收响应 → 回填 data/tag RAM、置 `way_valid` → 上游重发 → 命中；
4. 标出 `flush_pipe_*` 在 st0/st1 作废请求的位置、`invalid_i` 清空 `way_valid` 的位置；
5. 在图侧注明关键参数：`NUM_FETCH=2`、`32 组 × 2 路 × 2 字/块`、`4 entry × 2 subentry`。

完成后再回答一个问题：**若把 `NUM_FETCH` 从 2 改为 4，但保持 `DCACHE_BLOCKWORDS=2` 不变，会发生什么不一致？**（提示：一次取指要 4 条指令 = 16 字节，但一个块只有 2 字 = 8 字节；取指会跨块，而当前 `core_rsp_data_o` 的位宽与掩码、以及「一次取指=一次缺失填充」的前提都会被破坏。正确做法是同步把 `DCACHE_BLOCKWORDS` 调成 4，恢复二者一致。）

## 6. 本讲小结

- 取指由 `warp_scheduler` 按 warp 的 PC 驱动，一次取 `NUM_FETCH=2` 条指令，对应 `ICACHE_ALIGN=8` 字节对齐。
- `instruction_cache` 是两拍流水（st0 读 SRAM、st1 判定），icache 几何为 **32 组 × 2 路 × 每块 2 字**；地址按 `[tag24|set5|blockoffset1|wordoffset2]` 拆分，一个块恰装 2 条指令 = 一次取指量。
- 命中由 `tag_checker_icache` 纯组合比较得出（tag 相等且 way 有效），替换由每组 `lru_matrix` 决定，`tag_access_icache` 统管 tag 体、有效位与 LRU。
- 缺失由 `mshr_icache` 处理：主缺失分配 entry 并向内存发一次请求（`has_send2mem` 保证不重发），次缺失只追加 subentry；响应回来驱动 tag/data 回填，并清空 entry。
- 顶层不向 core 反压：缺失时回送 `status=1`，由 `warp_scheduler` 保持 PC 重发，块填充后重发即命中——这是「重发命中」机制的本质。
- `flush_pipe_*` 作废指定 wid 的在途取指；`invalid_i`（来自 GPGPU_top 的 `cache_invalid`）整表清空有效位。

## 7. 下一步学习建议

本讲覆盖了**取指与 icache**。建议接下来：

- 顺着流水线往下走，学习 **u3-l3（ibuffer 与 decodeUnit）**：icache 命中回送的指令如何被暂存、译码成控制信号。
- 横向对比 **u6-l1（dcache 与 MSHR）**：dcache 的 MSHR 结构与本讲 `mshr_icache` 高度相似但有写回/写缓冲，对比阅读能加深对 MSHR 范式的理解。
- 想搞清 icache 的对外请求如何走到 L2，可跳读 **u6-l3（l1cache_arb）** 与 **u7（TileLink/L2）**，看 `mem_req_*` 经仲裁与互联到达 `Scheduler` 的完整链路。
