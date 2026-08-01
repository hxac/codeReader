# 共享内存 shared_memory

## 1. 本讲目标

在 [u5-l1（访存单元 LSU）](u5-l1-lsu.md) 中，我们跟到 LSU 的 `addrcalculate` 用 `addr < SHAREMEM_SIZE` 这一条判定把请求**分流**：落在这条线以下的，不是去 D-cache，而是去另一片叫「共享内存」的存储。本讲就专门打开这片存储。

读完本讲，你应当能够：

1. 说清共享内存（shared memory / LDS）**是什么、为什么需要它**：它是 SM 内部、同 workgroup 内线程之间共享数据的低延迟暂存区，与 D-cache 是两条并行的通路，且**不经过 L2、不可缓存、不会缺失**。
2. 算出当前配置下共享内存的总容量 `SHAREMEM_SIZE`，并解释 `SHAREDMEM_DEPTH`/`SHAREDMEM_NWAYS`/`SHAREDMEM_BLOCKWORDS`/`SHAREMEM_NBANKS` 这组参数如何决定存储几何，以及它们与 `NUM_THREAD` 的派生关系。
3. 描述多 bank 划分的地址解码方式，并讲清 `bankconflict_arb` 如何**检测** bank 冲突、如何用「每拍每 bank 只服务一个 lane、剩余 lane 存寄存器下拍再处理」的方式把一次冲突访问**拆分成多拍**串行执行。

> 本讲是 expert 层第二篇，默认你已学过 u5-l1（LSU 的地址计算与分流）和 [u1-l3（define.v 参数）](u1-l3-define-parameters.md)。共享内存的请求接口字段（`tag`/`setidx`/`blockoffset`/`wordoffset1h`/`activemask`）与 D-cache 同源，请随时回看 u5-l1。

---

## 2. 前置知识

### 2.1 为什么 GPU 要有共享内存

GPU 的主存（经 L2、AXI 到外存）离运算核很远，一次访问要付出几十上百拍的延迟。但有一类数据访问模式非常常见：**同一个 workgroup（CTA）内的线程需要互相交换中间结果**——例如矩阵分块乘法中，先把一个 tile 加载到片上，然后多个线程反复读取它做计算。如果把这种「被反复读、且只需本 workgroup 可见」的数据放到全局主存里走 cache，既慢又浪费带宽。

**共享内存（shared memory，OpenCL 里叫 LDS，Local Data Share）** 就是为这种场景准备的：它是**每个 SM 内部的一块 SRAM**，访问延迟只有几拍，且天然只对同一个 workgroup 内的线程可见。软件通过地址落在共享内存地址段来显式使用它。

### 2.2 bank 与 bank 冺突

为了让一个 warp 内的多个线程能**在同一拍里并行**读到不同数据，共享内存被切成多个**bank**（存储体），每个 bank 是一条独立的 SRAM，可以独立读端口。只要 warp 内各线程访问的地址分布在不同 bank 上，就能一拍全部读出，带宽等于 bank 数。

但若两个或更多线程**同一拍访问同一个 bank**的不同地址，就会发生 **bank 冲突（bank conflict）**：一条 SRAM 同一拍只能出一个地址的数据，冲突的请求只能**串行**处理。这是共享内存实现里最关键、也最精巧的部分，正是本讲 `bankconflict_arb` 要解决的问题。

### 2.3 它与 D-cache 的关系

两者是 SM 里并行的两条访存通路，由 LSU 按地址分流：

- 地址 `< SHAREMEM_SIZE` → 共享内存（本片 SRAM，无缺失、不进 L2）。
- 否则 → D-cache（可能缺失，缺失时经 `l1cache_arb` 进 L2，详见 [u6-l1](u6-l1-dcache-and-mshr.md)）。

共享内存**没有 tag 检查、没有 MSHR、没有缺失概念**——它的地址直接就是 SRAM 地址，命中是必然的。所以它比 D-cache 简单得多，唯一要精心处理的只剩 bank 冲突。

---

## 3. 本讲源码地图

本讲核心源码位于 `src/gpgpu_top/sm/l1cache/shared_memory/`，参数定义在 `src/define/define.v`，上游分流在 LSU。

| 文件 | 作用 |
| --- | --- |
| [sharemem.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/shared_memory/sharemem.v) | 共享内存顶层 `shared_mem`。例化 `bankconflict_arb` 与多 bank SRAM、读写 crossbar、响应 FIFO，是本讲的「主板」。 |
| [bankconflict_arb.v](https://github.com/THU-DSP-LAB-ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/shared_memory/bankconflict_arb.v) | bank 冲突检测与拆分仲裁器：算每个 bank 有几个 lane 请求、冲突时每 bank 选一个 lane 服务、剩余 lane 存寄存器下拍再处理。 |
| [src/define/define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v) | `SHAREDMEM_*` / `SHAREMEM_*` 参数族的总开关。 |
| [src/common_cell/fixed_pri_arb.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/fixed_pri_arb.v) | 固定优先级仲裁器：冲突时每 bank 选编号最小的 lane。 |
| [src/common_cell/pop_cnt.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/pop_cnt.v) | 数 1 个数：统计每个 bank 收到了几个 lane 请求，>1 即冲突。 |

上游衔接：`sm_wrapper.v` 把 `pipe` 的 `shared_req_*` 接口直接连到 `shared_mem` 实例（不经 `l1cache_arb`）；LSU 的 `lsu_exe.v` 里 `SHARED_ADDR_MAX = `SHAREMEM_SIZE`，`addrcalculate` 据此把地址段内的请求导到共享内存。

---

## 4. 核心概念与源码讲解

### 4.1 SHAREDMEM 参数族：共享内存的几何与容量

#### 4.1.1 概念说明

和 D-cache 一样，共享内存用「深度 × 路 × 块字数」三件套描述几何，但它多了一个 `NBANKS`（bank 数）。这组宏全在 [define.v:117-133](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L117-L133)：

```verilog
`define SHAREDMEM_DEPTH 128                                  // 组数（深度）
`define SHAREDMEM_NWAYS 1                                    // 路数（恒为 1，非组相联）
`define SHAREDMEM_BLOCKWORDS /*`NUM_THREAD*/ `DCACHE_BLOCKWORDS // 每块的“字”数
`define SHAREMEM_SIZE (`SHAREDMEM_DEPTH * `SHAREDMEM_BLOCKWORDS * 4) // 总容量(字节)
`define SHAREMEM_NLANES `NUM_THREAD                          // lane 数 = 线程数
`define SHAREMEM_NBANKS `DCACHE_BLOCKWORDS                   // bank 数 = 块字数
`define SHAREDMEM_BLOCKOFFSETBITS $clog2(`SHAREDMEM_BLOCKWORDS)
`define SHAREMEM_BANKIDXBITS $clog2(`SHAREMEM_NBANKS)
`define SHAREMEM_BANKOFFSET ((`SHAREDMEM_BLOCKOFFSETBITS > `SHAREMEM_BANKIDXBITS) ? (`SHAREDMEM_BLOCKOFFSETBITS - `SHAREMEM_BANKIDXBITS) : 1 )
```

几个要点先记住：

- **`SHAREDMEM_BLOCKWORDS = DCACHE_BLOCKWORDS`**（默认 2，[define.v:73](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L73)）。注释里留着 `/*NUM_THREAD*/` 说明设计上曾考虑让块字数随线程数变，但当前锁死与 D-cache 同一个块字数，这样**块内偏移位（blockoffset）可以同时给 D-cache 和共享内存复用**。
- **`SHAREMEM_NBANKS = DCACHE_BLOCKWORDS`**（默认 2），且注释明说「no bigger than DCACHE_BLOCKWORDS」。这是一个关键设计选择：**bank 数等于块字数**，于是块内偏移的低位正好就是 bank 编号，地址解码最简。
- **`SHAREMEM_NLANES = NUM_THREAD`**：lane 数 = 每 warp 线程数，一次向量访存最多有 `NUM_THREAD` 个 lane 同时发起。
- **`SHAREDMEM_NWAYS = 1`**：共享内存不是组相联 cache，路数恒为 1，没有 tag 比较。

#### 4.1.2 容量计算

总容量公式：

\[
\text{SHAREMEM\_SIZE} = \text{SHAREDMEM\_DEPTH} \times \text{SHAREDMEM\_BLOCKWORDS} \times 4
\]

代入默认配置（`SHAREDMEM_DEPTH=128`、`SHAREDMEM_BLOCKWORDS=DCACHE_BLOCKWORDS=2`、每字 4 字节）：

\[
\text{SHAREMEM\_SIZE} = 128 \times 2 \times 4 = 1024 \;\text{字节} = 1\;\text{KB}
\]

这就是 `addrcalculate` 里那条分流线 `addr < 1024` 的由来（`SHARED_ADDR_MAX` 在 [lsu_exe.v:112](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/lsu_exe.v#L112) 被赋为 `` `SHAREMEM_SIZE ``，再传入 `addrcalculate` 的 `SHARED_ADDR_MAX` 参数，于 [addrcalculate.v:169](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/lsu/addrcalculate.v#L169) 处做 `addr[i] < SHARED_ADDR_MAX` 判定）。

#### 4.1.3 与 NUM_THREAD 的关系

`NUM_THREAD`（[define.v:11](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L11)，默认 4）通过两条路径影响共享内存：

1. **直接**：`SHAREMEM_NLANES = NUM_THREAD`，决定一次向量访问的 lane 数与所有 per-lane 向量位宽（数据、activemask、blockoffset、wordoffset 都是 `NLANES` 份并排）。
2. **间接**：改变 `NUM_THREAD` 时若同步放开 `SHAREDMEM_BLOCKWORDS`（注释里的 `NUM_THREAD` 分支），bank 数与每拍并行度也会随之放大。但当前默认配置下两者解耦，bank 数只跟随 `DCACHE_BLOCKWORDS`。

⚠ 注意：`SHAREMEM_NBANKS` 当前被 `DCACHE_BLOCKWORDS` 钳在 2，**小于** `NUM_THREAD`（默认 4）。这意味着即便没有 bank 冲突，4 个 lane 也只有 2 个 bank，必然有 lane 落到同 bank——下一节就会看到硬件如何处理。

#### 4.1.4 代码实践：改参数算容量

**实践目标**：建立「改一行 define、容量跟着变」的直觉。

1. 打开 [define.v:117-133](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L117-L133)。
2. 把 `SHAREDMEM_DEPTH` 从 128 改为 256（**仅思想实验，勿提交**）。
3. 手算新容量：\( 256 \times 2 \times 4 = 2048 \) 字节 = 2 KB。
4. 推论：若真改了，必须同步检查 `addrcalculate` 的地址分流线 `addr < SHAREMEM_SIZE` 是否仍覆盖全部共享地址（它是宏派生的，会自动跟上），以及 kernel 软件是否仍假设 1 KB 边界。

**预期结果**：容量随 `SHAREDMEM_DEPTH` 线性变化；`SHAREMEM_BANKIDXBITS`/`SHAREDMEM_BLOCKOFFSETBITS` 因为只依赖 blockwords/banks 而不变。**待本地验证**：实际综合后 SRAM 深度翻倍是否满足时序。

#### 4.1.5 小练习与答案

**练习 1**：若把 `SHAREDMEM_BLOCKWORDS` 改为 4（同时 `DCACHE_BLOCKWORDS` 也为 4），`SHAREMEM_NBANKS`、`SHAREMEM_SIZE`、`SHAREMEM_BANKIDXBITS` 各变成多少？

**答案**：`NBANKS = DCACHE_BLOCKWORDS = 4`；`SIZE = 128 × 4 × 4 = 2048` 字节；`BANKIDXBITS = $clog2(4) = 2`。

**练习 2**：为什么共享内存不需要 `SHAREDMEM_NWAYS > 1`？

**答案**：它是软件显式管理的暂存区，地址直接映射到 SRAM 行，不存在「多个候选块二选一」的替换问题，故无需组相联，路数恒为 1 即可。

---

### 4.2 sharemem 顶层：接口、流水与多 bank SRAM

#### 4.2.1 概念说明

顶层模块 `shared_mem`（[sharemem.v:17](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/shared_memory/sharemem.v#L17)）可看作一个「多端口小仓库」：

- **`core_req_*`（请求口）**：来自 LSU/pipe，携带 `instrid`（属于哪条向量访存指令）、`iswrite`（读/写）、地址（拆成 `tag`+`setidx`）、各 lane 的 `activemask`、块内偏移 `blockoffset`、字内字节使能 `wordoffset1h`、写数据 `data`。
- **`core_rsp_*`（响应口）**：把读到的数据（或写完成标志）连同 `instrid`、`activemask` 还给 LSU。

它的核心数据结构是 `SHAREMEM_NBANKS` 条独立 SRAM，外加一个 `bankconflict_arb` 来调度各 lane 与各 bank 之间的连接（crossbar）。

#### 4.2.2 核心流程

一次向量访存从进到出的主线：

1. **st0（输入寄存）**：`core_req` 握手成功后，把全部字段锁存进 `_st0` 寄存器（[sharemem.v:55-97](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/shared_memory/sharemem.v#L55-L97)）。
2. **冲突检测**：`bankconflict_arb` 用 st0 的 `blockoffset`/`wordoffset1h`/`activemask` 组合地算出：哪些 lane 本拍能被服务、各 bank 该接哪个 lane 的数据、是否发生冲突（详见 4.3）。
3. **st1（数据寄存）**：写数据从 `_st0` 打到 `_st1`（[sharemem.v:111-161](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/shared_memory/sharemem.v#L111-L161)），与冲突仲裁结果一起驱动 SRAM 写口；读则用 st0 的地址直接发起。
4. **SRAM 读写**：每个 bank 一块 `sram_template`（[sharemem.v:304-320](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/shared_memory/sharemem.v#L304-L320)），按 `bank_idx` 选通的 lane 写入、按 bank 输出读出。
5. **读 crossbar + st2**：读出的数据再经 crossbar 按 lane 回填，与 select 信号一同寄存到 `_st2`（[sharemem.v:336-362](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/shared_memory/sharemem.v#L336-L362)）。
6. **响应 FIFO**：组装好的响应进 `core_rsp_q`（深度 4 的 `stream_fifo_pipe_true_with_count`，[sharemem.v:380-395](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/shared_memory/sharemem.v#L380-L395)）排队送出。

冲突期间，输入口会被反压（见 4.2.4），当前请求在 st0/st1 寄存器里「原地不动」，每拍只消化被冲突挑中的那部分 lane，直到全部 lane 处理完。

#### 4.2.3 源码精读：bank SRAM 的几何与地址解码

每个 bank 的 SRAM 容量由 [sharemem.v:38-39](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/shared_memory/sharemem.v#L38-L39) 决定：

```verilog
localparam SRAM_SET   = `SHAREDMEM_DEPTH*`SHAREDMEM_NWAYS*`SHAREDMEM_BLOCKWORDS/`SHAREMEM_NBANKS;
localparam SETIDXBITS = $clog2(SRAM_SET);
```

含义：总存储行数（深度×路×块字数）**平均分到各 bank**，每个 bank 分到 `SRAM_SET` 行。默认配置下 \( \text{SRAM\_SET} = 128 \times 1 \times 2 / 2 = 128 \)，`SETIDXBITS = 7`。

bank 内的地址由 `{tag, setidx}` 的低位拼接得到（[sharemem.v:266-269](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/shared_memory/sharemem.v#L266-L269)）：

```verilog
assign core_req_ba_st0          = {core_req_tag_st0,core_req_setidx_st0};
assign core_req_setidx_bank_st0 = core_req_ba_st0[SETIDXBITS-1:0]; // 取低 7 位
```

即：bank 的行地址 = 全地址（`BABITS` 位 = tag+setidx）的最低 `SETIDXBITS` 位。块内选哪个字、字内选哪些字节，则由 `bankoffset` 与 `wordoffset1h` 经 crossbar 给到 SRAM 的 way mask。

`SRAM_IN` 生成块（[sharemem.v:271-334](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/shared_memory/sharemem.v#L271-L334)）用 `generate for` 把同一份 `sram_template` 复制 `SHAREMEM_NBANKS` 份。注意其参数：`GEN_WIDTH=8`（每次写 8 位=1 字节）、`NUM_WAY=BYTESOFWORD=4`（每行 4 字节）、`SET_DEPTH=SETIDXBITS`（128 行）。每 bank 容量 \( 128 \times 4 \times 8 = 4096 \) 位 = 512 字节，两 bank 合计 1024 字节，与 `SHAREMEM_SIZE` 吻合。

> `BANKOFFSET_ISZERO`（[sharemem.v:40](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/shared_memory/sharemem.v#L40)）是个分支开关：当 `DCACHE_BLOCKOFFSETBITS <= SHAREMEM_BANKIDXBITS`（默认 1≤1 成立）时，块内偏移位全被 bank 编号吃掉，`bankoffset` 恒为 0，地址解码退化到最简分支。

#### 4.2.4 源码精读：反压与冲突期间的保持

输入口何时准备好接收新请求，由 [sharemem.v:398](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/shared_memory/sharemem.v#L398) 决定：

```verilog
assign core_req_ready_o = !bankconflict_reg && !rsp_q_alm_full && !core_req_isvalid_write_st1;
```

三个条件任一不满足就反压上游：

- `bankconflict_reg`：当前正在拆分冲突，必须把当前请求处理完才能接下一个；
- `rsp_q_alm_full`：响应 FIFO 快满了（`count == 深度-3`），不能再灌；
- `core_req_isvalid_write_st1`：上一笔写还在 st1 等落库。

冲突期间 `_st1` 的有效位会被「粘住」，见 [sharemem.v:244-245](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/shared_memory/sharemem.v#L244-L245)：

```verilog
core_req_isvalid_write_st1 <= (core_req_fire_st0 && core_req_iswrite_st0) || (core_req_isvalid_write_st1 && bankconflict_reg);
core_req_isvalid_read_st1  <= (core_req_fire_st0 && !core_req_iswrite_st0) || (core_req_isvalid_read_st1  && bankconflict_reg);
```

后半句 `(isvalid && bankconflict_reg)` 的含义是：**只要还在冲突，这笔请求就一直有效**，配合 `bankconflict_arb` 每拍只放行部分 lane，实现「一笔向量访问拆成多拍落库/读出」。

#### 4.2.5 代码实践：跟踪一笔共享内存写

**实践目标**：把「请求 → 冲突仲裁 → bank 写入」串起来。

1. 在 [sm_wrapper.v:498-517](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v#L498-L517)（`shared_mem` 的例化处）确认 `core_req_data_i` 来自 `pipe_shared_req_data`。
2. 顺着 `core_req_data_st1_wire[i]`（[sharemem.v:159](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/shared_memory/sharemem.v#L159)）→ `data_for_write[j]`（[sharemem.v:273](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/shared_memory/sharemem.v#L273)）→ SRAM `w_req_data_i`（[sharemem.v:319](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/shared_memory/sharemem.v#L319)）走一遍。
3. 注意 `data_for_write[j] = core_req_data_st1_wire[crsbar_sel_for_write[j]]`：bank j 实际写的是「被选中那个 lane」的数据，`crsbar_sel_for_write[j]` 由 `one2bin` 把 one-hot 的 `data_crsbar_write_sel1h` 转成二进制 lane 号（[sharemem.v:284-291](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/shared_memory/sharemem.v#L284-L291)）。

**需要观察的现象**：发生 bank 冲突时，同一笔写请求里不同 lane 的数据会**分多拍**进入不同 SRAM 行；无冲突时一拍写完。**预期结果**：波形里 `core_req_valid` 拉高后 `core_req_ready` 在冲突期间为 0，多个周期后才看到响应。**待本地验证**。

#### 4.2.6 小练习与答案

**练习 1**：默认配置下每 bank SRAM 多大？为什么 `NUM_WAY` 设成 `BYTESOFWORD`？

**答案**：每 bank \( 128 \times 4 \times 8 = 4096 \) 位 = 512 字节。`NUM_WAY=BYTESOFWORD=4` 是为了让 `wordoffset1h`（4 位字节使能）能直接当作 SRAM 的 way mask，实现「按字节写」而无需读改写。

**练习 2**：读路径用 st0 地址、写路径用 st1 地址，为何能这么分？

**答案**：读只需把地址送进 SRAM 组合地出数据（`read_setidx` 用 st0），再寄存到 st2 对齐；写则要等冲突仲裁结果（在 st0 组合算出）与数据（打到 st1）都就绪后才落库，所以写地址用 st1。两者时序错开，避免同拍读写同一 SRAM 的端口冲突。

---

### 4.3 bankconflict_arb：冲突检测与拆分仲裁

#### 4.3.1 概念说明

`bankconflict_arb`（[bankconflict_arb.v:17](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/shared_memory/bankconflict_arb.v#L17)）解决的问题是：一次向量访问有 `NLANES` 个 lane，但只有 `NBANKS` 个 bank，若多个 lane 的地址落在同一 bank，单口 SRAM 一拍只能服务一个。

它的策略可以一句话概括：**「每个 bank 每拍只挑一个 lane 服务；没服务上的 lane 存进寄存器，下拍接着挑，直到全部服务完。」** 这是典型的「用时间换端口」——把冲突串行化。

#### 4.3.2 核心流程

整个模块是「计算 → 检测 → 选择 → 保留」的组合+时序闭环：

1. **解码每个 lane 的 bank 号**：从 per-lane 的 `blockoffset` 抽出 `bankidx`（块内偏移低位）。
2. **生成 per-bank 的请求位图**：把每个 lane 的 bank 号转成 one-hot，再乘上该 lane 的 `activemask`（不活跃的 lane 不算请求），得到「bank n 收到了哪些 lane 的请求」。
3. **数 1 检测冲突**：每个 bank 用 `pop_cnt` 数请求个数，`>1` 即该 bank 冲突。
4. **固定优先级选一个**：每个 bank 用 `fixed_pri_arb` 在请求它的 lane 里选编号最小的那个服务。
5. **保留剩余 lane**：把没被选中的活跃 lane 存进 `perlane_conf_req_*_reg`，下拍重新走 2~4，直到无冲突。
6. **输出 crossbar 选择信号**：告诉 SRAM 每个 bank 该接哪个 lane 的数据（写）、每个 lane 该从哪个 bank 读数据（读）。

被服务 lane 的掩码 `active_lane_o` 还会随响应回传，供 LSU 知道哪些 lane 的数据这次有效。

#### 4.3.3 源码精读：per-lane bank 号解码

[bankconflict_arb.v:54-62](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/shared_memory/bankconflict_arb.v#L54-L62) 把每个 lane 的 `blockoffset`（`DCACHE_BLOCKOFFSETBITS` 位）切成两段：

```verilog
assign perlane_req_bankidx[i]    = core_req_arb_blockoffset_i[i*`DCACHE_BLOCKOFFSETBITS +:`SHAREMEM_BANKIDXBITS]; // 低位=bank号
assign perlane_req_bankoffset[i] = BANKOFFSET_ISZERO ? 'd0 : core_req_arb_blockoffset_i[...];                       // 高位=bank内块偏移
assign perlane_req_wordoffset1h[i] = core_req_arb_wordoffset1h_i[...];                                            // 字节使能
```

即 `blockoffset` 的低位是 bank 编号、剩余位是 bank 内的字偏移。`BANKOFFSET_ISZERO` 时高位段消失（默认情形）。

#### 4.3.4 源码精读：bank 请求位图与冲突判定

[BANK_MASK 块, bankconflict_arb.v:72-88](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/shared_memory/bankconflict_arb.v#L72-L88) 把 bank 号转 one-hot 并屏蔽不活跃 lane：

```verilog
assign bank_idxmasked[j]  = bank_idx1h[j] & {`SHAREMEM_NBANKS{lane_activemask[j]}};
```

`bank_idx1h[j]` 由 `bin2one` 把二进制 bank 号展开成 one-hot；再 `& activemask`，使被掩蔽的 lane 在所有 bank 上都清零。`bank_idxmasked[m][n]=1` 即「lane m 请求 bank n」。

[COUNT 块, bankconflict_arb.v:97-132](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/shared_memory/bankconflict_arb.v#L97-L132) 对每个 bank 统计与挑选：

```verilog
pop_cnt #(.DATA_LEN(`SHAREMEM_NLANES),...) bankreq_count (
  .data_i (perbank_req_bin[n]), .data_o (perbank_req_count[n]));   // 数该 bank 几个 lane 请求
fixed_pri_arb #(.ARB_WIDTH(`SHAREMEM_NLANES)) conf1h (
  .req (perbank_req_bin[n]), .grant (perbank_activelane_when_conf1h[n])); // 选一个 lane
assign perbank_req_conf[n] = (perbank_req_count[n] > 1);           // >1 即冲突
```

`fixed_pri_arb`（[fixed_pri_arb.v:24-27](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/fixed_pri_arb.v#L24-L27)）用经典的「屏蔽低位已有请求」实现固定优先级：`grant = req & ~(低位的或)`，即选最低位的那个 1。

只要任一 bank 冲突且当前有有效请求，整体即冲突（[bankconflict_arb.v:134](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/shared_memory/bankconflict_arb.v#L134)）：

```verilog
assign bankconflict = (|perbank_req_conf) && (core_req_arb_enable_i || bankconflict_reg);
```

#### 4.3.5 源码精读：剩余 lane 的保留与重试

本拍被各 bank 选中的 lane 合并成 `activelane_when_conf1h`（[bankconflict_arb.v:136-143](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/shared_memory/bankconflict_arb.v#L136-L143)），没被选中的活跃 lane 即「保留集」：

```verilog
assign reservelane_when_conf1h = ~activelane_when_conf1h & lane_activemask;
```

[PRELANE_REQ_REG 块, bankconflict_arb.v:158-188](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/shared_memory/bankconflict_arb.v#L158-L188) 把保留集的 bankidx/bankoffset/wordoffset/activemask 存进寄存器：

```verilog
if(reservelane_when_conf1h[x]) begin
  perlane_conf_req_activemask_reg[x] <= reservelane_when_conf1h[x];          // 保留
  perlane_conf_req_bankidx_reg[...]  <= perlane_conf_req_bankidx[x];
  ...
end else begin
  perlane_conf_req_activemask_reg[x] <= 1'b0;                                // 已服务，清掉
end
```

下一拍 `bankconflict_reg=1` 时，模块改用寄存器里的保留集作为输入（见 [bankconflict_arb.v:160-163](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/shared_memory/bankconflict_arb.v#L160-L163) 的三目选择），再次执行检测—挑选—保留，如此循环，直到保留集为空、`bankconflict` 落到 0。

#### 4.3.6 源码精读：输出给 SRAM 的 crossbar 选择

[OUTPUT_WRITE/READ 块, bankconflict_arb.v:193-206](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/shared_memory/bankconflict_arb.v#L193-L206) 产出 crossbar 控制信号：

```verilog
// 写：每个 bank 接哪个 lane 的数据（one-hot）
data_crsbar_write_sel1h_o[...]      = perbank_activelane_when_conf1h[y];
// 读：每个 lane 从哪个 bank 读（one-hot，=该 lane 的 bank_idxmasked）
data_crsbar_read_sel1h_o[...]       = bank_idxmasked[z];
// bank 使能与字内偏移
data_array_en_o[y]                  = |perbank_activelane_when_conf1h[y];
```

注意读写选择的不对称：**写**是「bank 视角」（每 bank 选一个 lane 提供数据），因为写口在 bank 侧；**读**是「lane 视角」（每 lane 指明从哪个 bank 取数据），因为读结果要回到 lane。两套 one-hot 经 `one2bin` 转成二进制 lane/bank 号，分别驱动 `sharemem.v` 里的 `data_for_write[j]` 与 `data_crsbar_out[k]` 两个 crossbar。

#### 4.3.7 代码实践：构造一组 bank 冲突请求

**实践目标**：亲眼看到「冲突 → 拆分多拍」。

1. 设默认配置（`NUM_THREAD=4`、`SHAREMEM_NBANKS=2`、块字数 2）。bank 号 = `blockoffset` 最低位。
2. 构造一条向量 load，让 4 个 lane 的地址满足：
   - lane0、lane1 落 bank0（地址末位让 `blockoffset[0]=0`）；
   - lane2、lane3 落 bank1（`blockoffset[0]=1`）。
3. 推演 `bankconflict_arb` 行为：
   - bank0 收到 lane0、lane1 两个请求 → `perbank_req_count=2` → 冲突；
   - bank1 收到 lane2、lane3 两个请求 → 同样冲突；
   - `fixed_pri_arb` 在 bank0 选 lane0、bank1 选 lane2 → 本拍服务 lane0、lane2；
   - lane1、lane3 进保留寄存器，下拍再各被对应 bank 服务。
4. 结论：这条向量 load 需要 **2 拍**才能读完全部 4 个 lane（每拍每 bank 1 个）。

**需要观察的现象**：波形上 `bankconflict_o` 在第一拍为 1，第二拍才回 0；`core_req_ready_o` 在此期间为 0（上游被反压）。**预期结果**：响应 `core_rsp_activemask` 第一拍只点亮被服务 lane、累计两拍后 4 个 lane 全亮。**待本地验证**：用 `make run-vcs-4w4t` 跑含共享内存的用例，在 `test.fsdb` 里抓 `shared_mem` 内部信号。

#### 4.3.8 小练习与答案

**练习 1**：若 4 个 lane 的地址恰好两两分布在不同 bank（lane0→bank0、lane1→bank1、lane2→bank0、lane3→bank1），是否冲突？需几拍？

**答案**：冲突。bank0 有 lane0、lane2 两个请求，bank1 有 lane1、lane3 两个请求。第一拍服务 lane0、lane1，第二拍服务 lane2、lane3，共 2 拍。bank 数（2）小于 lane 数（4）时几乎必然有冲突。

**练习 2**：把 `SHAREMEM_NBANKS` 增大到等于 `SHAREMEM_NLANES`（注释里被划掉的方案）会带来什么好处与代价？

**答案**：好处是「各 lane 落不同 bank」时一拍即可完成，理想带宽最大化、无冲突。代价是 bank 数翻倍带来更多 SRAM 实例与更宽 crossbar，面积与时序压力上升；且当前 `NBANKS` 被 `DCACHE_BLOCKWORDS` 钳制，需同时改 cache 块字数才能生效。

**练习 3**：`bankconflict_o` 为 1 时，上游 `core_req_ready_o` 一定为 0 吗？为什么？

**答案**：是。`core_req_ready_o = !bankconflict_reg && ...`，冲突期间 `bankconflict_reg=1` 必然拉低 ready，迫使上游停止发新请求，让硬件专心把当前冲突请求拆分完毕。

---

## 5. 综合实践

**任务**：把本讲三件事（容量计算、地址解码、bank 冲突拆分）串成一张完整的「共享内存访问图」。

1. **算容量**：在 [define.v:117-133](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L117-L133) 用默认参数算出 `SHAREMEM_SIZE`（应为 1024 字节），并写出每个 bank 的 SRAM 行数（128）与每行字节数（4）。
2. **画地址解码**：画一张 32 位地址的拆分图，标出 `wordoffset`（字节使能）、`blockoffset`（bank 号 + bank 内偏移）、`setidx`（bank 行地址）各自的位段，并标出 `addr < SHAREMEM_SIZE` 这条分流线。
3. **跟踪一次冲突访问**：选定一条向量共享内存 load 指令，假设 4 个 lane 的 bank 分布为 `[0,0,1,1]`，画出：
   - 第 1 拍：哪些 lane 被服务、哪些进保留寄存器、`bankconflict_o` 与 `core_req_ready_o` 的值；
   - 第 2 拍：保留 lane 如何被重新挑选、最终 `core_rsp_activemask` 何时全亮。
4. **对照源码**：在 `bankconflict_arb.v` 里找到 `perbank_req_conf`、`reservelane_when_conf1h`、`fixed_pri_arb` 三处，确认你画的时序与代码一致。

交付物：一张地址解码图 + 一张两拍时序表。**待本地验证**：用 VCS 波形核对时序表。

---

## 6. 本讲小结

- **共享内存是 SM 内的低延迟暂存区**：地址 `< SHAREMEM_SIZE` 的请求走它，不进 L2、不会缺失、无 tag 比较，是同 workgroup 内线程通信的快通道；LSU 用 `SHARED_ADDR_MAX = `SHAREMEM_SIZE`` 做分流。
- **容量由 `SHAREDMEM_DEPTH × SHAREDMEM_BLOCKWORDS × 4` 决定**：默认 128×2×4 = 1024 字节；`SHAREMEM_NBANKS = DCACHE_BLOCKWORDS`（默认 2），`SHAREMEM_NLANES = NUM_THREAD`（默认 4）。
- **多 bank 划分让并行成为可能**：bank 号取自块内偏移低位，每个 bank 是一条独立 `sram_template`（默认每 bank 128 行×4 字节）。
- **bank 冲突靠拆分串行化解决**：`bankconflict_arb` 用 `pop_cnt` 检测（每 bank 请求数 >1 即冲突）、用 `fixed_pri_arb` 每拍每 bank 选一个 lane、把剩余 lane 存寄存器下拍重试。
- **冲突期间反压上游**：`core_req_ready_o = !bankconflict_reg && !rsp_q_alm_full && !core_req_isvalid_write_st1`，保证一笔冲突访问完整处理完才接下一笔。
- **读写 crossbar 视角不对称**：写按 bank 选 lane（`write_sel1h`），读按 lane 选 bank（`read_sel1h`），分别经 `one2bin` 驱动两套 crossbar。

---

## 7. 下一步学习建议

- 本讲的共享内存与 [u6-l1（D-cache）](u6-l1-dcache-and-mshr.md) 是 SM 内并行的两条访存通路。建议接着读 [u6-l3（L1 cache 仲裁 l1cache_arb）](u6-l3-l1cache-arbiter.md)，看 D-cache 与 icache 的请求如何被仲裁后统一对外；注意共享内存**不**进 `l1cache_arb`，它直接挂在 `pipe` 上，这是它与 D-cache 在系统连接上的关键差别。
- 若想看共享内存请求从何而来，回到 [u5-l1（LSU）](u5-l1-lsu.md) 的 `addrcalculate`，对照 `is_shared = addr < SHARED_ADDR_MAX` 与 `S_SHARED` 状态机，理解 LSU 如何把一条向量访存按 lane 分流。
- 对本模块用到的公共单元（`fixed_pri_arb`/`pop_cnt`/`bin2one`/`one2bin`）想系统了解的，可读 [u8-l2（公共单元库 common_cell）](u8-l2-common-cell-library.md)。
