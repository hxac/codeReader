# L2 Cache Scheduler 架构

## 1. 本讲目标

本讲精读 Ventus GPGPU 片上最后一级缓存——L2 Cache。它源自 SiFive 的开源 `block-inclusivecache`（包名 `inclusivecache`），由苏州国芯（C\*Core）用 Verilog 重写。学完本讲你应当能够：

- 说清 L2 的「包含式（inclusive）目录 + banked store」整体结构，以及它在系统中作为 L1 与外部存储（经 AXI）之间桥梁的位置。
- 对照 TileLink 四条通道（`in_a`/`in_d`/`out_a`/`out_d`），指出 `sinkA`/`sourceD`/`sourceA`/`sinkD` 各自负责哪一条、为什么 L2 内部要拆成「收（sink）」「发（source）」两类模块。
- 看懂 `Scheduler.v` 顶层的编排逻辑：directory 查找 → MSHR 分配/合并 → 轮询调度 → banked_store 读写 → 回响应，这条主链如何被若干 FIFO 解耦。
- 解释 MSHR 如何合并次缺失、`Listbuffer` 如何同时充当 putbuffer 与次缺失请求队列、`sourceD` 的 8 态状态机如何处理读命中/读缺失/写命中/写回/冲刷。
- 说明 `finish_issue`（即 `l2cache_finish_issue`）在 workgroup 完成冲刷流程中如何产生，以及它如何最终拉起 `host_rsp_valid_o`。

本讲是单元 7 的核心，承接 u7-l1（TileLink 协议与 source 编码）与 u6-l1（L1 D-cache 与它的 MSHR），向下衔接 u7-l3（cluster→L2 互联）。

## 2. 前置知识

在进入 L2 之前，请确认你已经掌握以下概念（前面讲义已建立）：

- **包含式缓存（inclusive cache）**：L2 的目录不仅记录自己拥有的块，还「包含」所有 L1 中缓存的块。当某个块要从 L2 驱逐时，L2 必须保证下级（L1）里不会有它的过期副本——这是后面 `directory` 需要做命中判定、`flush` 需要扫表清空的根源。
- **TileLink 的 A/D 两通道**（u7-l1）：A 通道发请求（带 address、mask），D 通道回响应（带 data、source）。`source` 字段是「回信地址」，L1 把自己的 MSHR entry 号打包进 source，L2 响应时原样回填，用来路由。
- **MSHR（缺失状态保持寄存器）**（u6-l1）：缓存缺失时不阻塞后续命中，用一个表项记录「这个块正在被取，取回来后要唤醒谁」。L1 D-cache 的 MSHR 跟踪自己的缺失；L2 也有自己的 MSHR，结构与职责不同，本讲详述。
- **L1 操作码**：`GET`(4)=读、`PUTFULLDATA`(0)=整块写、`PUTPARTIALDATA`(1)=部分写、`HINT`(5)=提示（`param=0` 为 flush 刷回、`param=1` 为 invalidate 无效化）。响应码 `ACCESSACKDATA`(1)=带数据回应、`ACCESSACK`(0)=无数据回应、`HINTACK`(2)=提示回应。这些在 [src/define/define.v:376-398](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L376-L398) 定义。
- **关键字 budget 提示**：本讲大量出现 `way`（路号）、`set`（组号）、`tag`、`victim`（被替换的牺牲块）。若已忘记，可回看 u6-l1。

## 3. 本讲源码地图

本讲涉及的全部源码集中在 `src/gpgpu_top/l2cache/`（共 11 个文件，由 `model_list.f` 汇总）：

| 文件 | 行数 | 作用 |
|------|------|------|
| `Scheduler.v` | 1429 | L2 顶层。例化所有子模块，包含目录读写仲裁、MSHR 分配/合并、轮询调度、FIFO 解耦等全部编排逻辑。 |
| `directory_test.v` | 573 | 包含式目录。tag SRAM + valid/dirty 位 + LRU 替换 + flush/invalidate 扫表。 |
| `sourceD.v` | 619 | 响应通路核心。8 态状态机，决定读/写 banked_store、向内存写回、向 L1 回 D 通道。产生 `finish_issue`。 |
| `MSHR.v` | 355 | 单条缺失的记录表项。schedule_a（向内存发 GET）、schedule_d（向 L1 回响应）、schedule_dir（回填 tag）、merge（合并次缺失写数据）。 |
| `banked_store.v` | 145 | 数据 SRAM 阵列。按 bank 分体，被 sourceD 读、被 sinkD/sourceD 写。 |
| `sinkA.v` | 211 | 接收 L1 的 A 通道请求。地址译码、写数据存入 putbuffer、阻塞反压。 |
| `sinkD.v` | 81 | 接收内存的 D 通道响应。寄存一拍，按 source 路由回对应 MSHR。 |
| `SourceA.v` | 60 | 把内部请求拼成发往内存的 A 通道（重组地址）。 |
| `Listbuffer.v` | 157 | 链表缓冲。在 sinkA 中作 putbuffer 存写数据；在 Scheduler 中作 `requests` 存次缺失请求。 |
| `Listbuffer_no_push_opc_put_source.v` | 133 | `Listbuffer` 的简化版（不存 opcode/put/source），sinkA 用它存写数据。 |
| `lru_matrix.v` | 125 | 每 set 一份的 LRU 矩阵，为 directory 选牺牲路。 |

此外会少量引用 `src/gpgpu_top/GPGPU_top.v`（顶层如何接 `finish_issue`）与 `src/define/define.v`（L2 参数宏）。

## 4. 核心概念与源码讲解

### 4.1 Scheduler 顶层：四通道架构与系统定位

#### 4.1.1 概念说明

`Scheduler` 是 L2 的顶层模块名（沿用 SiFive 原版命名）。它本身几乎不存数据，职责是「连线 + 编排」：把 L1 来的请求、内存来的响应，按 TileLink 四条通道分发到内部子模块，再控制目录查找、MSHR 调度、数据 SRAM 读写，最后把结果回送出去。

理解 L2 的关键，是先建立「**四通道 = 两收两发**」的视角：

| Scheduler 端口方向 | TileLink 通道 | 对端 | L2 内部负责模块 | 语义 |
|----|----|----|----|----|
| 输入 `sche_in_a` | A | 来自 L1（经互联） | `sinkA` | L1 向 L2 发的请求（GET/PUT/HINT） |
| 输出 `sche_in_d` | D | 送回 L1 | `sourceD` | L2 向 L1 回的响应（数据/ACK） |
| 输出 `sche_out_a` | A | 发往外部存储（经 AXI） | `sourceA` | L2 向内存发的请求（GET 取块/PUT 写回） |
| 输入 `sche_out_d` | D | 来自外部存储 | `sinkD` | 内存向 L2 回的取数响应 |

注意命名规律：`sink*` 是「吞」外部进来的流（被动接收），`source*` 是「吐」向外部的流（主动发起）。`sinkA` 吞 L1 请求、`sinkD` 吞内存响应；`sourceA` 吐内存请求、`sourceD` 吐 L1 响应。D 通道响应全靠 `source` 字段路由，这部分原理已在 u7-l1 讲透，本讲只关注 L2 内部如何填/读 source。

一个额外端口 `finish_issue_o` 不属于 TileLink 事务流，而是「冲刷完成」信号，下面 4.6 详述。

#### 4.1.2 核心流程

把 Scheduler 想象成一个工厂流水线，一次「L1 读缺失」的全过程是：

```
L1 GET ──in_a──► sinkA ──FullRequest──► directory(查tag)
                                            │ miss
                                            ▼
                                     分配/合并 MSHR
                                            │
                  ┌─────────────────────────┴──────────────┐
                  ▼                                          ▼
        MSHR.schedule_a ──► sourceA ──out_a──► 内存(GET)
                                                  │
        内存响应 ──out_d──► sinkD ──► MSHR.sinked ◄┘(按source路由)
                                  │
                                  ▼ 填充数据
                            banked_store(写)
                                  │
        MSHR.schedule_d ──► sourceD ──读banked_store──► in_d ──► L1(数据)
        MSHR.schedule_dir ──► directory(写tag,完成回填)
```

这条链上有四个关键编排点（都在 Scheduler.v 里）：

1. **目录读取的放行条件**：不是每个请求都立刻查目录，要等有空闲 MSHR、putbuffer 不满、不在冲刷期。
2. **MSHR 分配 vs 合并**：目录判 miss 后，若该块已有在途 MSHR（tag/set 都匹配）则合并（次缺失），否则分配新 MSHR（主缺失）。
3. **轮询调度（mshr_select）**：多个 MSHR 同时有事要办（发内存/回 L1/写目录），但共享资源（sourceA、sourceD、directory 写口、banked_store）每拍只能服务一个，靠轮询仲裁选一个。
4. **FIFO 解耦**：目录结果、sourceD 的内存写请求，都经 FIFO 缓冲，避免上游卡住下游。

#### 4.1.3 源码精读

Scheduler 的对外端口就是上表四通道加 `finish_issue`，定义在 [Scheduler.v:18-71](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L18-L71)。注意 `sche_in_a_*`（L1 请求进）、`sche_in_d_*`（L1 响应出）、`sche_out_a_*`（内存请求出）、`sche_out_d_*`（内存响应进）四组，以及单独的 `finish_issue_o`。

Scheduler 例化了全部子模块，例化位置一览（后续各节展开）：

- `SourceA_dut`：[Scheduler.v:501-527](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L501-L527)
- `sourceD_dut`：[Scheduler.v:529-590](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L529-L590)
- `sinkA_dut`：[Scheduler.v:594-629](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L594-L629)
- `sinkD_dut`：[Scheduler.v:630-647](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L630-L647)
- `directory_test_dut`：[Scheduler.v:648-695](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L648-L695)
- `banked_store_dut`：[Scheduler.v:696-719](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L696-L719)
- `Listbuffer_dut`（次缺失请求队列 `requests`）：[Scheduler.v:720-744](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L720-L744)
- MSHR 阵列（`generate for` 例化 `MSHRS` 个）：[Scheduler.v:745-872](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L745-L872)

四通道到子模块的接续关系（关键连线）：

```verilog
// in_a(L1请求) 直接送 sinkA；in_a 的 ready 也由 sinkA 给
assign sinkA_a_opcode_i = sche_in_a_opcode_i ;  // Scheduler.v:928
assign sche_in_a_ready_o = sinkA_a_ready_o   ;  // Scheduler.v:937

// in_d(L1响应) 由 sourceD 产生
assign sche_in_d_valid_o = SourceD_d_valid_o ;  // Scheduler.v:938

// out_a(内存请求) 由 sourceA 产生
assign sche_out_a_valid_o = sourceA_a_valid_o;  // Scheduler.v:875

// out_d(内存响应) 送 sinkD
assign sinkD_d_opcode_i = sche_out_d_opcode_i;  // Scheduler.v:896
```

L2 的几何参数（默认仿真配置）由 `define.v` 决定，本讲全程用到，列此备查：

- 组数 `L2CACHE_NSETS = 2`、路数 `L2CACHE_NWAYS = 4`（[define.v:135-137](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L135-L137)），即 2 组 × 4 路。
- 块字数 `L2CACHE_BLOCKWORDS = DCACHE_BLOCKWORDS = 2`（[define.v:73](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L73)、[define.v:139](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L139)），故每块 8 字节、一拍传完（`BEATBYTES=8`、`DATA_BITS=64`、`MASK_BITS=8`）。
- MSHR 数 `MSHRS = 4`、putbuffer 链数 `PUTLISTS = 4`、每链深度 `PUTBEATS = 4`（[define.v:345-351](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L345-L351)），`MSHRS=4` 正是后面 `mshr_selectOH` 用 `4'b0001..4'b1000` 四种情况的来源。

> 说明：仿真默认配置下 L2 容量极小（仅 64 字节），这是为了快速验证逻辑，真实综合会放大。`MSHRS` 由 `L2CACHE_MEMCYCLES`（一次访存周期数）派生，反映「在等内存期间最多能容忍几个并发缺失」。

#### 4.1.4 代码实践

**实践目标**：建立「四通道—子模块」的映射，确认你分得清谁是 sink、谁是 source。

**操作步骤**：

1. 打开 [Scheduler.v:18-71](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L18-L71) 的端口列表。
2. 对四组端口，分别找到它们在 Scheduler 内部连到哪个子模块（提示：`sche_in_a_*` → sinkA 的 `sinkA_a_*`；`sche_out_d_*` → sinkD 的 `d_*`；`sche_out_a_*` ← sourceA 的 `sourceA_a_*`；`sche_in_d_*` ← sourceD 的 `d_*`）。
3. 验证方向：`output` 端口的 valid 一定由「source 类」或响应方驱动，`input` 端口的 valid 一定进「sink 类」。

**需要观察的现象**：`sche_in_a` 与 `sche_out_a` 虽都是 A 通道，但前者是 input（收 L1）、后者是 output（发内存）；D 通道同理。这正是「同一协议、不同方向」容易混淆之处。

**预期结果**：你能画出 4.1.2 那张流向图，并标注每个子模块接哪条通道。若无法确定某条连线，标注「待本地验证」并在仿真波形中确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `sinkA` 模块叫「sink」而 `sourceD` 叫「source」？它们各自处理的是哪条 TileLink 通道？

> **答**：「sink」表示该模块「吞」一条进入 L2 的流——`sinkA` 吞 L1 发来的 A 通道请求，`sinkD` 吞内存发来的 D 通道响应。「source」表示该模块「吐」一条离开 L2 的流——`sourceA` 吐发往内存的 A 通道请求，`sourceD` 吐送回 L1 的 D 通道响应。

**练习 2**：默认配置下 L2 容量是多少字节？多少组、多少路？

> **答**：`L2CACHE_SIZEBYTES = L2CACHE_BLOCKS × L2CACHE_BLOCKBYTES = (NSETS×NWAYS) × (BLOCKWORDS×4) = (2×4) × (2×4) = 8 × 8 = 64` 字节。2 组、4 路。

---

### 4.2 directory_test：包含式目录与命中/替换/冲刷

#### 4.2.1 概念说明

`directory_test` 是 L2 的「账本」。缓存要判断一个请求命中与否，必须查 tag 表；要替换，必须选牺牲路；要支持 invalidate/flush，必须能遍历所有表项清空。`directory_test` 把这三件事都做了。它内部用一块 `sram_template` 存所有 (set, way) 的 tag，另用两组触发器阵列 `status_reg_valid`、`status_reg_dirty` 存每个表项的有效位与脏位，再为每个 set 配一份 `lru_matrix` 做 LRU 替换。

它对外的核心是一个「读端口 + 结果端口」的握手：你给它一个请求（tag/set/opcode/...），它查表后回一个 `dir_result`，告诉你命中没有（`hit`）、命中或替换的路号（`way`）、被替换块是否脏（`dirty`）、以及原请求信息（透传给后续 MSHR/sourceD）。

「包含式」的含义在这里落地：L2 目录的 valid 位标记的是「L2 自己是否持有该块」。因为包含关系，L1 持有的块 L2 一定也登记在案，所以 L2 处理 GET 命中即可直接回 L1，处理 miss 才需要去内存取。

#### 4.2.2 核心流程

**正常查表（读端口）**：

1. `dir_read_valid_i` 拉起，带 `set`、`tag`。SRAM 按 set 读出该组所有路的 tag。
2. 组合逻辑把每路的 tag 与请求 tag 比较，得到 one-hot `hits`：命中则 `hit=1`、`hitway` 为命中路。
3. 若 miss，用 `lru_matrix` 给出的 `victimWay` 作替换路。
4. 结果寄存一拍（`ren1`），通过 `dir_result_*` 输出。

**替换时的脏处理**：若被替换的 victim 路是脏的（`status_reg_dirty` 为 1），`dir_result_dirty_o=1`，后续 sourceD 会负责把它写回内存（4.6 节）。替换前要先把脏块写回，这会拖慢缺失处理——这就是 `sourceD` 需要 STAGE_3 等状态的原因。

**冲刷/无效化（flush/invalidate）**：这是遍历式的。`directory_test` 内部有一个 `flushCount`，从 0 扫到 `NUM_SET*NUM_WAY-1`，逐个表项发出 `dir_result`（`flush_o=1`，最后一项 `last_flush_o=1`）。invalidate 会清 valid 位，flush 只清脏位（数据仍有效但与内存一致）。扫表期间 `dir_ready_o=0`，拒绝新的正常查表。

**上电初始化（wipeoff）**：复位后先用 `wipeCount` 扫一遍清空所有 valid/dirty 位（`wipeoff` 标志），扫完 `wipeDone` 才允许正常工作。

#### 4.2.3 源码精读

tag 比较产生命中信号，是 directory 的核心组合逻辑。[directory_test.v:417-436](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/directory_test.v#L417-L436) 用 `generate for` 把每一路的比较展开：

```verilog
assign ways = regout;
assign status_valid = status_reg_valid[(NUM_WAY)*set +:NUM_WAY];
for(p=0;p<NUM_WAY;p=p+1)
  assign hits[p] = ways[(p+1)*`TAG_BITS-1-:`TAG_BITS] == tag && status_valid[p];
one2bin U_one2bin(.oh(hits), .bin(hitway));  // one-hot 转 binary 路号
assign hit = |hits;
```

`status_reg_valid`/`status_reg_dirty` 的维护在 [directory_test.v:299-336](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/directory_test.v#L299-L336)：写命中 PUT 时置脏；替换时把新块 valid 置 1、dirty 清 0；flush 扫描时按 is_invalidate 决定是否清 valid。

LRU 牺牲路选择：[directory_test.v:374-392](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/directory_test.v#L374-L392) 为每个 set 例化一个 `lru_matrix`，在命中/替换/写入时更新，输出 `victimWay`：

```verilog
assign victimWay = lru_way_o[dir_result_set_o*`WAY_BITS+:`WAY_BITS];
```

冲刷扫描计数器在 [directory_test.v:232](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/directory_test.v#L232) 与 [256-268](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/directory_test.v#L256-L268)，扫完 `flushDone` 后清 `flush_issue_reg`。扫表期间产生的结果带有 `flush_o` 标志，区分于正常查表结果（[directory_test.v:552-567](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/directory_test.v#L552-L567)）：

```verilog
assign dir_result_opcode_o = flush_issue_reg_1 ? `HINT : read_bits_reg_opcode ;
assign dir_result_flush_o  = flush_issue_reg_1 ;
assign dir_result_last_flush_o = flush_issue_reg_1 ? flushDone_reg_1 : 1'b0 ;
```

注意 `dir_ready_o`（[directory_test.v:441](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/directory_test.v#L441)）在 `wipeDone` 且非 `flush_issue` 时才为真，这是上游放行查表的前提。

#### 4.2.4 代码实践

**实践目标**：跟踪一次目录查找，理解 hit/way/dirty 三个输出如何产生。

**操作步骤**：

1. 读 [directory_test.v:417-436](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/directory_test.v#L417-L436)。假设 `tag=0x5`，set 内 4 路的 tag 分别是 `{0x1,0x5,0x3,0x7}`、valid 分别是 `{1,1,0,1}`。手算 `hits`、`hitway`、`hit`。
2. 再假设该请求 miss（没有任何路的 tag 匹配且 valid）。`hit=0`，则 `dir_result_way_o` 取 `victimWay`（[directory_test.v:554](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/directory_test.v#L554)）。查 `victimWay` 由谁决定。
3. 若该 victim 路原 dirty=1，说明什么？后续谁负责处理这个脏块？

**需要观察的现象**：命中时 `way` 是真实命中路；未命中时 `way` 是即将被替换的牺牲路，且 `dirty` 位告诉你这块要不要先写回。

**预期结果**：步骤 1 中 `hits=4'b0100`、`hitway=1`、`hit=1`。步骤 3：脏 victim 必须先写回内存才能被新块覆盖，这一步由 sourceD 在 STAGE_3 完成（见 4.6）。

#### 4.2.5 小练习与答案

**练习 1**：flush 和 invalidate 在 `directory_test` 里的区别是什么？

> **答**：invalidate（`is_invalidate=1`）会清掉 valid 位（块彻底作废）；flush 只清 dirty 位（块数据保留，但因已与内存一致故视为干净）。两者都遍历所有 set×way，扫描期间 `dir_ready_o=0` 拒绝新查表。

**练习 2**：`wipeoff`/`wipeDone` 是干什么用的？

> **答**：上电复位后的初始化扫描。复位后 `wipeoff=1`，`wipeCount` 从 0 扫完所有 set，把 `status_reg_valid`/`status_reg_dirty` 全清 0，扫完置 `wipeDone=1`。只有 `wipeDone` 之后目录才允许正常读写（见 `dir_ready_o` 与 `wen_new` 的条件）。

---

### 4.3 sinkA / sinkD / SourceA：通道收发与地址译码

#### 4.3.1 概念说明

这三个模块相对简单，是「边界翻译器」：

- **sinkA**：接收 L1 的 A 通道请求。它做两件事——(a) 把 32 位 `address` 拆成 `tag/set/offset` 三段；(b) 对 PUT（写）请求，把写数据暂存进一个 `Listbuffer`（putbuffer），因为写数据要等后续 sourceD 处理时才消费，中间隔了好几拍。
- **sinkD**：接收内存的 D 通道响应。它把响应寄存一拍，按 `source` 字段选出对应的 MSHR 号，告知「那个 MSHR 等的内存数据回来了」。
- **SourceA**：反向操作。L2 内部用的是拆开的 `tag/set/offset`，发往内存时要拼回完整 `address`，SourceA 负责拼接，并把内部请求字段映射成 A 通道信号。

这三个模块体现了 TileLink 在 L2 边界的「拆/拼地址」与「按 source 路由」两个核心动作。

#### 4.3.2 核心流程

**sinkA 地址译码 + putbuffer**：

```
in_a(address,opcode,data,mask,...)
   │
   ├── tag    = address[高位]
   ├── set    = address[offset位+: SET_BITS]
   ├── offset = address[低位]
   │
   ├── opcode 是 PUT(0/1)? → hasData=1 → 写数据 push 进 putbuffer（按 freeIdx 选空闲链）
   │
   └── 输出 FullRequest(set,tag,offset,opcode,put=freeIdx,...) 给 directory
```

`put` 字段是 putbuffer 的链号（`PUT_BITS=2` 位），后续 sourceD 处理 PUT 时凭它从 putbuffer pop 出数据。

**sinkD 路由**：内存响应的 `d_source` 就是当初 sourceA 发请求时填的 source（L2 用 `a_source_o='d4` 标记自己发的内存请求，见 [sourceD.v:606](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/sourceD.v#L606)）。但要注意：sinkD 的 `source_o` 输出的是「响应对应的 MSHR 号」，Scheduler 用它选通 `mshr_sinked_valid_i[p]`（见 4.4.3）。

**SourceA 地址拼接**：把 `tag/set/offset` 拼回 `address = {tag, set, offset}`，并透传 opcode/data/mask 等。

#### 4.3.3 源码精读

sinkA 的地址拆分在 [sinkA.v:177-184](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/sinkA.v#L177-L184)：

```verilog
assign tag = a_sinkA_a_address[`ADDRESS_BITS-1-:`TAG_BITS];
assign set = a_sinkA_a_address[`OFFSET_BITS+:`SET_BITS];
assign offset = a_sinkA_a_address[`OFFSET_BITS-1:0];
```

`hasData` 判定写请求（PUT 才有数据）在 [sinkA.v:148](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/sinkA.v#L148)：`hasData = (opcode==3'b1)||(opcode==3'b0)`，即 PUTFULLDATA(0) 或 PUTPARTIALDATA(1)。sinkA 的反压（`a_ready`）综合了请求通路、putbuffer、空闲链、以及 HINT 时的 invalidate/flush ready 四个阻塞条件 [sinkA.v:159-169](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/sinkA.v#L159-L169)：

```verilog
assign req_block = !sinkA_req_ready_i;              // 后端(directory)没准备好
assign buf_block = hasData && !putbuffer_push_ready_o; // putbuffer 满
assign set_block = hasData && !free;                 // 没有空闲 putbuffer 链
```

sinkD 把内存响应寄存一拍并暴露 source，见 [sinkD.v:43-77](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/sinkD.v#L43-L77)。其 `d_ready_o=1'b1` 表示 L2 始终能收内存响应（无反压）。

SourceA 的地址拼接在 [SourceA.v:54](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/SourceA.v#L54)：

```verilog
assign sourceA_a_address_o = {sourceA_req_tag_i, sourceA_req_set_i, sourceA_req_offset_i};
```

注意它把 `sourceA_a_source_o` 直接透传内部 source、`a_param_o` 强制为 0——这是 L2 对外发请求的固定封装。

#### 4.3.4 代码实践

**实践目标**：确认地址拆/拼的一致性，理解 putbuffer 链号 `put` 的作用。

**操作步骤**：

1. 在 [sinkA.v:177-184](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/sinkA.v#L177-L184) 与 [SourceA.v:54](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/SourceA.v#L54) 中对比拆分与拼接。确认 `{tag,set,offset}` 的位序在拆和拼两端是对称的。
2. 在 sinkA 中找到 `put`（`freeIdx`）是如何分配的（[sinkA.v:129-145](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/sinkA.v#L129-L145) 的 one2bin），以及 `sinkA_req_put_o` 如何把这个链号随请求传走。
3. 在 sourceD 中找到 PUT 处理时如何用这个 `put` 去 pop putbuffer（提示：[sourceD.v:175-176](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/sourceD.v#L175-L176)）。

**需要观察的现象**：写数据从 sinkA 入 putbuffer，到 sourceD 处理时才被 pop——这中间隔着目录查找与调度，链号 `put` 是把它们关联起来的「凭证」。

**预期结果**：能讲清「sinkA 分配 put 链号 → 随请求传到 MSHR/sourceD → sourceD 凭 put 号 pop 出写数据」这条链。若不确定 putbuffer 的 pop 时机，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：sinkA 对 PUT 请求的反压条件比 GET 多了哪两项？为什么 GET 不需要这些？

> **答**：多了 `buf_block`（putbuffer 满）和 `set_block`（无空闲 putbuffer 链）。因为只有 PUT 才携带写数据需要存入 putbuffer；GET 不带数据，无需 putbuffer，故不受这两项约束。

**练习 2**：sinkD 的 `source_o` 起什么作用？

> **答**：它指明当前内存响应对应哪个 MSHR。Scheduler 据此置 `mshr_sinked_valid_i[source]=1`，让那个等待内存数据的 MSHR 进入「数据已到」状态，随后产生 schedule_d 把数据回送给 L1。

---

### 4.4 MSHR：缺失合并与多路调度

#### 4.4.1 概念说明

L2 的 MSHR 与 L1 D-cache 的 MSHR 思想一致但实现不同。L2 默认有 `MSHRS=4` 个 MSHR 表项（[Scheduler.v:745-747](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L745-L747) 用 `generate for` 例化）。每个 MSHR 记录一条缺失请求的全部信息（tag/set/opcode/source/data/mask...），并在等待内存期间承担三件事：

- **schedule_a**：向内存发 GET（取这个缺失的块）。所有 MSHR 的 schedule_a 共享一个 sourceA，靠轮询调度分时使用。
- **schedule_d**：内存数据回来后，向 L1 回 D 通道响应（带数据）。所有 MSHR 的 schedule_d 共享一个 sourceD。
- **schedule_dir**：块取回后，把新 tag 写进目录（完成「回填」）。

此外还有两个辅助机制：

- **merge（次缺失合并）**：当一个新缺失的 tag/set 与某个正在途中的 MSHR 完全相同时，不新分配 MSHR，而是把这条请求挂到那个 MSHR 的「次缺失队列」（`requests` Listbuffer）上。等块取回来，所有挂在它上面的请求一起被满足。这对 PUT（写）尤其重要——多个写合并后只发一次取块请求。
- **sinked（内存响应回收）**：内存数据回来（sinkD）时，对应 MSHR 的 `sink_d_reg` 置位，此后它才有资格通过 schedule_d 把数据回送 L1。

#### 4.4.2 核心流程

**主缺失分配 + 次缺失合并**（在 Scheduler 顶层，非 MSHR 内部）：

```
directory 结果 (miss)
   │
   ├─ tagMatches[p]: 该 miss 的 tag/set 是否与已存在的 MSHR p 相同?
   │     (tagMatches = requests_valid[p] && tag匹配 && set匹配 && !hit)
   │
   ├─ alloc = !(|tagMatches): 没有任何 MSHR 匹配 → 主缺失，分配新 MSHR
   │
   ├─ 若 alloc:  选空闲 MSHR (mshr_insertOH)，mshr_alloc_valid 拉起，写满请求信息
   │              同时把这条请求也 push 进 requests[Listbuffer] (按新 MSHR 号索引)
   │
   └─ 若 !alloc (有匹配 p): 次缺失，挂到 requests 的第 p 条链 (tagMatches2uint)
                            并触发 mshr_mixed (合并写数据)
```

**MSHR 的三路调度输出**（每个 MSHR 独立产生 valid）：

- `schedule_a_valid`：分配后即有效（要把缺失发去内存），发出去（握手）后清。受 `mshr_wait_i` 抑制（sourceD 在写回脏 victim 时要求阻塞新 GET，避免 victim 的过早 miss）。
- `schedule_d_valid`：`mshr_valid_i && sink_d_reg`——内存数据回来后才有效。
- `schedule_dir_valid`：sinked 后、且该请求是 GET（需要回填目录）时有效。

**轮询调度（mshr_select）**：把所有「有事要办」的 MSHR 汇总成 `mshr_request`，用轮询优先级编码器选出本轮服务的那个 `mshr_select`，再把它的 schedule_a/d/dir 输出 mux 到共享资源。

#### 4.4.3 源码精读

每个 MSHR 的「请求就绪」由三路输出或起来（[Scheduler.v:835](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L835)）：

```verilog
assign mshr_request[p] = (sourceA_req_ready_o && mshr_schedule_a_valid_o[p])   // 要发内存
                       || (SourceD_req_ready_o && mshr_schedule_d_valid_o[p])  // 要回L1
                       || (mshr_schedule_dir_valid_o[p] && dir_write_ready_o); // 要写目录
```

sinkD 的响应如何回到对应 MSHR：[Scheduler.v:836](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L836)，按 `sinkD_resp_source_o == p` 且 opcode 是 `ACCESSACKDATA`（带数据）来选通：

```verilog
assign mshr_sinked_valid_i[p] = sinkD_resp_valid_o
                              && (sinkD_resp_source_o == p)
                              && sinkD_resp_opcode_o == `ACCESSACKDATA;
```

主/次缺失的判定与分配：[Scheduler.v:851](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L851)（`tagMatches`）、[1280-1282](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L1280-L1282)（`alloc`/`is_pending`）：

```verilog
assign tagMatches[p] = requests_valid_o[p] && (mshr_status_tag_o[...]==dir_result_tag_o)
                    && (mshr_status_set_o[...]==dir_result_set_o) && (!dir_result_hit_o);
assign alloc         = !(|tagMatches);   // 无匹配 → 主缺失
```

新 MSHR 的选择（`mshr_insertOH` 找空闲项）在 [Scheduler.v:1291-1301](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L1291-L1301)。`requests` 队列的 push 索引由 `alloc` 决定走新 MSHR 号还是匹配的 MSHR 号（[Scheduler.v:1312](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L1312)）：

```verilog
assign requests_push_index_i = alloc ? mshr_insertOH2uint : tagMatches2uint;
```

轮询调度器在 [Scheduler.v:947-959](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L947-L959)。`robin_request = {mshr_request, mshr_request & robin_filter}` 是一轮询优先编码的常见手法：把原始请求与「被上一轮屏蔽后的请求」拼接，做左移优先编码，实现轮流优先。`robin_filter` 在 [Scheduler.v:1175-1183](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L1175-L1183) 每拍更新，记住「上一轮选中了谁，本轮让其他低位优先」。

被选中的 MSHR，其 schedule_a/d/dir 三组输出通过一个 `case(mshr_selectOH)` 大 MUX 复用到顶层 `schedule_*` 信号（[Scheduler.v:961-1165](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L961-L1165)，四种 one-hot 对应 4 个 MSHR）。这些 `schedule_*` 再驱动 sourceA/sourceD/directory 写口。

MSHR 内部的寄存与三路 valid 产生在 [MSHR.v:188-353](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/MSHR.v#L188-L353)。其中 schedule_a 固定发 `GET`（[MSHR.v:295](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/MSHR.v#L295)）；schedule_d 的 valid 依赖 `sink_d_reg`（[MSHR.v:272](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/MSHR.v#L272)）；schedule_dir 仅对 GET 触发（[MSHR.v:350](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/MSHR.v#L350)）。写数据合并 `merge_data` 在 [MSHR.v:160-163](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/MSHR.v#L160-L163)：按 mask 把新写数据覆盖到旧数据上。

#### 4.4.4 代码实践

**实践目标**：复现一次「主缺失分配 + 次缺失合并」的判定。

**操作步骤**：

1. 假设 MSHR[1] 正在途，记录的 `tag=0xA, set=0`（这是它刚分配时存入的 `mshr_status_tag_o[1]`/`..._set_o[1]`）。
2. 现在又来一个目录 miss 结果：`dir_result_tag_o=0xA, dir_result_set_o=0`。手算 `tagMatches[1]`。
3. 由此 `alloc` 是真还是假？这条新请求会被 push 到 `requests` 的第几条链？
4. 若新请求是 PUT（写），`mshr_merge` 会做什么（参考 [MSHR.v:261-264](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/MSHR.v#L261-L264) 与 [Scheduler.v:845](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L845)）？

**需要观察的现象**：次缺失不会新占一个 MSHR，而是挂到已有 MSHR 的链上；多个写合并后，等块取回时一次写入合并后的数据，只发一次内存 GET。

**预期结果**：`tagMatches[1]=1`，`alloc=0`（因为 `|tagMatches` 非零），新请求 push 到链 1。若新请求是 PUT，`mshr_merge_valid_i[1]` 在轮询选中 1 时拉起，`merge_data` 按 mask 把新写数据并入 MSHR 的 `data_reg`。**若无法确定合并后数据回送时机，标注「待本地验证」。**

#### 4.4.5 小练习与答案

**练习 1**：`alloc` 与 `is_pending` 都基于 `tagMatches`，它们有何区别？

> **答**：`alloc = !(|tagMatches)`——没有任何 MSHR 匹配时为真，表示需要分配新 MSHR（主缺失）。`is_pending = (|tagMatches) && alloc` 恒为假（因为两者条件互斥），这是从 SiFive 原版 Scala 翻译时保留的信号，实际语义上「有匹配即次缺失、不分配」，由 `requests_push_index_i = alloc ? 新号 : 匹配号` 体现。

**练习 2**：为什么 schedule_a 要受 `mshr_wait_i`（即 `SourceD_mshr_wait_o`）抑制？

> **答**：当 sourceD 正在写回一个脏的 victim 块（STAGE_3）时，它置 `mshr_wait_o=1`，阻止 MSHR 在此期间发出新的 GET。这是为了防止 victim 块对应的地址在被写回完成前又被同一 MSHR 过早地发起缺失请求，造成状态混乱。

---

### 4.5 Listbuffer 与 banked_store：putbuffer 与分体数据存储

#### 4.5.1 概念说明

这两个模块分别管「数据在等什么」和「数据存在哪」。

**Listbuffer（链表缓冲）**：一个用链表（head/tail/next 指针）组织的多队列存储。它有两个化身：

- 在 sinkA 里作 **putbuffer**：存 PUT 请求的写数据。共 `PUTLISTS=4` 条链，每条链深 `PUTBEATS=4`。每条链对应一个可能的 MSHR/请求，链号就是 sinkA 分配的 `put`。sourceD 处理 PUT 时按 `put` 号从对应链 pop 出数据。
- 在 Scheduler 顶层作 **`requests`** 队列：存次缺失请求。每条链对应一个 MSHR，链上挂的是「依赖这个 MSHR 的次缺失请求」。当 MSHR 的块取回时，从对应链逐个 pop 出请求，分别回送 L1。

链表结构让「按链号入队、按链号 FIFO 出队」变得自然——这正是 putbuffer 和次缺失队列共同需要的访问模式。

**banked_store（分体数据阵列）**：L2 的数据 SRAM。它把一块数据按字节切成 `NUM_BANKS` 个 bank（默认 8 个），每个 bank 一块 `sram_template`，所有路（way）共享同一 bank SRAM（用 waymask 选路）。读由 sourceD 发起（读命中块或刚填充的块给 L1），写由两路发起：sinkD（内存取回的整块填充）和 sourceD（PUT 写命中，部分写）。

#### 4.5.2 核心流程

**Listbuffer 的链表操作**：

```
push: 找一个空闲槽位 freeIdx → 写数据 → 接到 index 链的尾部 (更新 tail、next)
pop:  按 index 取 head 槽位的数据 → head 后移到 next[head] → 释放槽位
```

每条链是独立的 FIFO（head/tail 各一份），但共享 `PUTBEATS` 个物理槽位（用 `used` 位图管理空闲）。

**banked_store 的读写**：

```
读(sourceD_radr): 给 set → sram_template 按 set 读出所有路 → sourceD 按 way 选出目标路数据
写: 两路竞争
  - sinkD_adr (内存填充): 整块写，mask 全 1
  - sourceD_wadr (PUT命中): 部分写，按 mask
  sinkD 优先 (sourceD_wadr_ready = !sinkD_adr_valid)
```

#### 4.5.3 源码精读

Listbuffer 的链表指针与 push/pop 在 [Listbuffer.v:41-155](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Listbuffer.v#L41-L155)。关键状态：`valid[PUTLISTS]`（每条链是否有数据）、`head/tail[每链]`、`used[PUTBEATS]`（槽位占用位图）、`next[每槽]`（链表后继）。push 时找空闲槽（`freeOH`/`freeIdx`，[Listbuffer.v:55-65](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Listbuffer.v#L55-L65)），接到目标链尾：

```verilog
tail[push_index] <= freeIdx;            // 链尾指向新槽
if(push_valid) next[push_tail] <= freeIdx; // 原尾槽的后继指向新槽
else           head[push_index] <= freeIdx;// 链空则 head 也指向新槽
```

banked_store 的分体在 [banked_store.v:44-53](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/banked_store.v#L44-L53)：`NUM_BANKS = ROW_BYTES/L2CACHE_WRITEBYTES = 8/1 = 8`。`generate for` 把 `sram_template` 例化 8 份（[banked_store.v:96-130](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/banked_store.v#L96-L130)），写入时用 `waymask`（bin2one 把 way 号转 one-hot，[banked_store.v:87-94](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/banked_store.v#L87-L94)）选目标路。sinkD 与 sourceD 写入的仲裁在 [banked_store.v:78-82](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/banked_store.v#L78-L82)：

```verilog
assign data_sel = sinkD_adr_valid_i ? sinkD_dat_data_i : sourceD_wdat_data_i; // sinkD 优先
assign sram_template_wen = {NUM_BANKS{(sourceD_wadr_valid_i||sinkD_adr_valid_i)}} & mask_sel;
```

读路径在 [banked_store.v:126](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/banked_store.v#L126)：从 `sram_template_rdata` 中按 `sourceD_radr_way_i` 选出目标路的数据交给 sourceD。

#### 4.5.4 代码实践

**实践目标**：弄清 Listbuffer 同一份代码如何身兼两职，以及 banked_store 的写仲裁。

**操作步骤**：

1. 对比两处 Listbuffer 例化：sinkA 内的 putbuffer（[sinkA.v:105-124](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/sinkA.v#L105-L124)，用的是简化版 `Listbuffer_no_push_opc_put_source`）与 Scheduler 内的 `requests`（[Scheduler.v:720-744](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L720-L744)，用完整版 `Listbuffer`）。说明它们入队的 `index` 分别代表什么。
2. 在 [banked_store.v:141-143](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/banked_store.v#L141-L143) 确认三个 ready 的优先级关系：`sourceD_wadr_ready_o = !sinkD_adr_valid_i` 表示写口 sinkD 优先，读口 `sourceD_radr_ready_o = 1'b1` 永远就绪。

**需要观察的现象**：同一份 Listbuffer RTL，因端口接法不同而分别充当「写数据缓冲」和「次缺失请求队列」——这是参数化复用的典型。

**预期结果**：能说出 putbuffer 的 index = put 链号（绑定 MSHR/写请求），`requests` 的 index = MSHR 号（绑定在途缺失）。**若不确定 banked_store 读写同拍是否冲突，标注「待本地验证」（提示：SRAM 单口，写优先于读）。**

#### 4.5.5 小练习与答案

**练习 1**：Listbuffer 用链表而非简单环形 FIFO，好处是什么？

> **答**：支持「多队列共享一组物理槽位」。`PUTLISTS` 条链共享 `PUTBEATS` 个槽位，某条链繁忙时仍可借用空闲槽位给其他链，提高利用率；且按链号随机入队/出队天然匹配 putbuffer（按 put 号）和 requests（按 MSHR 号）的访问模式。

**练习 2**：banked_store 为什么把数据切成 8 个 bank？

> **答**：为了用窄 SRAM 拼出宽位（64 位）数据通路，并支持按字节 mask 的部分写。每个 bank 宽 `CODE_BITS = 8×WRITEBYTES = 8` 位，8 个 bank 并行构成 64 位，mask 的每一位控制对应 bank 是否写入。

---

### 4.6 sourceD：响应状态机与 finish_issue

#### 4.6.1 概念说明

`sourceD` 是 L2 最复杂的模块，也是一个 8 态有限状态机（FSM）。它是「响应通路」的总指挥——读命中怎么回、读缺失怎么回、写命中怎么处理、写缺失要不要写回脏块、flush/invalidate 怎么走，全在它的状态转移里。它对接几乎所有其他模块：从 MSHR/dir_result_buffer 收请求、读/写 banked_store、向 sourceA 发内存写回、向 L1 发 D 通道响应、向 directory 回填 tag。

它还承担两个特殊职责：

- **脏 victim 写回**：当一次缺失要替换掉一个脏块时，sourceD 先把脏块经 sourceA 写回内存（STAGE_3），才能让新块入住。
- **产生 `finish_issue`**：冲刷流程中，最后一拍的响应发出时，`finish_issue_o` 脉冲一次（[sourceD.v:615](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/sourceD.v#L615)）。这个信号一路传到 GPGPU_top，最终拉起 `host_rsp_valid_o`。

#### 4.6.2 核心流程

sourceD 的输入请求有两路（在 Scheduler 顶层 mux，[Scheduler.v:1388-1404](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L1388-L1404)）：

- **来自 dir_result_buffer**（`!schedule_d_valid_o`）：目录刚判完、尚未经 MSHR 的命中请求或脏 victim 通知（`from_mem=0`）。
- **来自 MSHR 的 schedule_d**（`schedule_d_valid_o`）：缺失已从内存取回（`from_mem=1`）。

FSM 核心状态（[sourceD.v:89-96](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/sourceD.v#L89-L96) 定义，[377-585](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/sourceD.v#L377-L585) 转移）：

```
STAGE_1(空闲): 收到一个请求 → 按 opcode/hit/dirty 分流
  ├─ GET 命中 / GET miss 不脏 → STAGE_4 (直接回 L1)
  ├─ GET miss 且脏          → STAGE_3 (先写回脏块) + mshr_wait
  ├─ PUT 命中 + banked就绪   → STAGE_4 (写 banked 后回 ACK)
  ├─ PUT 命中 banked未就绪   → STAGE_2 (等 banked)
  ├─ PUT miss               → STAGE_4 (写不分配,回 ACK,可能要写回)
  └─ HINT 脏                → STAGE_2/4 (冲刷脏块)
STAGE_2: 等 banked_store 写就绪 → STAGE_4
STAGE_3: 写回脏 victim 到内存 (a_valid) → STAGE_1
STAGE_4: 主响应态,向 L1 发 D (d_valid); 必要时同时向内存发 A (a_valid)
         ├─ 写miss: 需 d&&a 都握手 → 否则转 STAGE_7/8 等另一个
         └─ 其余: d 握手即完成 → STAGE_1
STAGE_7/8: 分别等 a 或 d 完成 → STAGE_1
```

读命中时：sourceD 在 STAGE_4 用 `d_data_o = bs_rdat_data_i`（从 banked_store 读出的数据）回 L1（[sourceD.v:595](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/sourceD.v#L595)）。读缺失（from_mem）时用 `s_final_req_data`（MSHR 从内存取回的数据）。D 通道 opcode 映射在 [sourceD.v:593](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/sourceD.v#L593)：GET 回 `ACCESSACKDATA`（带数据）、flush 末拍回 `HINTACK`、其余回 `ACCESSACK`。

向内存写回（sourceA）的条件在 [sourceD.v:602](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/sourceD.v#L602)：miss 且（脏 victim 或 PUT 写不分配）时，把数据用 `PUTFULLDATA` 写回内存。

**finish_issue 的产生**：[sourceD.v:615](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/sourceD.v#L615)：

```verilog
assign finish_issue_o = d_valid_o && s_final_req_last_flush;
```

即「正在发 D 通道响应」且「这是 flush 的最后一拍」。它在 Scheduler 顶层透传为 `finish_issue_o`（[Scheduler.v:905](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L905)），再上传到 GPGPU_top 的 `l2cache_finish_issue`（[GPGPU_top.v:426](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L426)），在那里：

```verilog
assign host_rsp_valid_o = l2cache_finish_issue && is_flushing;  // GPGPU_top.v:255
```

即：workgroup 完成（`wg_done`）触发冲刷（`is_flushing=1` + `cache_invalid`），L2 把整表 flush 一遍，扫到最后一拍发 `finish_issue`，主机才被告知「这个 workgroup 真正结束了，数据已刷净」。

#### 4.6.3 源码精读

读/写 banked_store 的判定（`s1_need_r`/`s1_need_w`）在 [sourceD.v:308-312](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/sourceD.v#L308-L312)：

```verilog
assign s1_need_w = (opcode==PUTFULL || opcode==PUTPARTIAL) && !from_mem && hit; // 写命中才写 banked
assign s1_need_r = (opcode==GET && hit) || (!hit && dirty) || (opcode==HINT && dirty); // 读命中或脏读
```

写 banked 的数据来自 putbuffer pop（`pb_beat_data`），见 [sourceD.v:370-374](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/sourceD.v#L370-L374)。这正是 4.3 提到的「凭 put 号 pop 写数据」的落点。

FSM 主转移 [sourceD.v:377-585](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/sourceD.v#L377-L585)。注意 STAGE_1 里 GET miss 且脏时置 `mshr_wait_reg=1`（[sourceD.v:397](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/sourceD.v#L397)），这就是 4.4 提到的、用来阻塞 MSHR 发新 GET 的 `mshr_wait_o`。

D 通道与 A 通道输出的组合逻辑在 [sourceD.v:591-613](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/sourceD.v#L591-L613)。`busy` 标志（[sourceD.v:339](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/sourceD.v#L339)）使 `req_ready_o=!busy`，即 sourceD 一次只处理一个请求（非流水）。

> **关于 FIFO 解耦**：sourceD 的内存写回请求（`a_*`）并不直接接 sourceA，而是经一个深度 8 的 `writebuffer`（[Scheduler.v:1222-1235](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L1222-L1235)）缓冲后再喂 sourceA（[Scheduler.v:1248-1259](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L1248-L1259)）。同理，目录结果经 `dir_result_buffer`（[Scheduler.v:1364-1377](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L1364-L1377)）缓冲后再进 sourceD。这两个 FIFO 把「目录读」「sourceD 处理」「内存写回」三段解耦，避免互相死锁。

#### 4.6.4 代码实践

**实践目标**：把 finish_issue 的产生一直追到 host_rsp_valid_o，理解「冲刷完成」如何闭合 workgroup 生命周期。

**操作步骤**：

1. 从 [sourceD.v:615](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/sourceD.v#L615) 出发，`finish_issue_o` 由 `d_valid_o && s_final_req_last_flush` 驱动。回查 `s_final_req_last_flush` 来自 `req_last_flush_i`，而后者在 Scheduler 中接 `dir_result_last_flush_o`（directory 扫表最后一拍，[directory_test.v:566](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/directory_test.v#L566)）。
2. 跟到 Scheduler 顶层 [Scheduler.v:905](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L905)（`finish_issue_o = SourceD_finish_issue_o`），再到 [GPGPU_top.v:426](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L426)（`.finish_issue_o(l2cache_finish_issue[j])`）。
3. 在 [GPGPU_top.v:255](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L255) 与 [257-270](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L257-L270) 看 `is_flushing` 如何由 `wg_done` 置位、由 `l2cache_finish_issue` 复位。

**需要观察的现象**：workgroup 完成 → `wg_done` → `is_flushing=1` + `cache_invalid` 触发 SM/L2 冲刷 → directory 扫表 → 最后一拍 sourceD 发 HINTACK 并产生 `finish_issue` → `host_rsp_valid_o=1` → 主机得知完成。这是一条贯穿 u1-l5、u6-l3、本讲的完整控制链。

**预期结果**：能画出从 `wg_done` 到 `host_rsp_valid_o` 的时序链。注意 `cache_invalid` 目前只触发 SM[0]（[GPGPU_top.v:253](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L253) 的 TODO 注释），这是已知限制。**多 SM 场景的冲刷行为待本地验证。**

#### 4.6.5 小练习与答案

**练习 1**：sourceD 一次只处理一个请求（`req_ready_o = !busy`），那 L2 的吞吐不会成为瓶颈吗？

> **答**：单 sourceD 确实是串行的，但前面有多级缓冲（dir_result_buffer、writebuffer、MSHR 阵列）削峰，且多个 MSHR 并发准备请求、轮询调度喂给 sourceD，使 sourceD 持续有事干。对 GPU 而言，L2 主要起聚合/包含作用，单请求处理串行化换取了控制简单、面积省，是合理的取舍。

**练习 2**：读命中和读缺失回 L1 的数据分别来自哪里？

> **答**：读命中（`s_final_req_hit`）时 `d_data_o = bs_rdat_data_i`，即从 banked_store 当场读出的数据；读缺失（from_mem，`!hit`）时 `d_data_o = s_final_req_data`，即 MSHR 从内存取回、随 schedule_d 带来的数据（见 [sourceD.v:595](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/sourceD.v#L595)）。

## 5. 综合实践

**任务**：把本讲六个最小模块串成一条完整的「L1 读缺失 → L2 处理 → 回响应」时序，画出完整的模块交互时序图，并标注每个信号跨越的握手。

请按以下步骤完成（源码阅读型实践，不修改源码）：

1. **入口**：假设 L1 某个 dcache MSHR 发出一个 GET 请求，经 u7-l3 的互联到达 L2 的 `sche_in_a`。跟踪它进入 `sinkA`（[sinkA.v:177-184](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/sinkA.v#L177-L184) 地址译码）。
2. **查目录**：sinkA 输出 `sinkA_req_*` → Scheduler 把它接成 `request_*`（[Scheduler.v:1267-1278](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L1267-L1278)）→ `dir_read_valid_i`（[Scheduler.v:1313](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L1313)）。注意放行条件包含 `mshr_free`（有空 MSHR）与 `!issue_flush_invalidate`（不在冲刷期）。directory miss → `dir_result_hit_o=0`。
3. **分配 MSHR**：`alloc=1`（无匹配），选空闲 MSHR（[Scheduler.v:1291-1301](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L1291-L1301)），`mshr_alloc_valid_i[p]=1` 写入请求信息（[MSHR.v:209-228](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/MSHR.v#L209-L228)）。
4. **发内存 GET**：MSHR 的 `schedule_a_valid` 拉起（[MSHR.v:293](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/MSHR.v#L293)），轮询选中后经 sourceA 发出 `sche_out_a`（[SourceA.v:54](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/SourceA.v#L54)）。
5. **内存响应**：`sche_out_d` → sinkD 寄存（[sinkD.v:43-56](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/sinkD.v#L43-L56)）→ `mshr_sinked_valid_i[p]`（[Scheduler.v:836](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/Scheduler.v#L836)）→ MSHR `sink_d_reg=1` → `schedule_d_valid`。
6. **回填与响应**：schedule_dir 把 tag 写回 directory（[MSHR.v:350-353](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/MSHR.v#L350-L353)）；sourceD 在 STAGE_4 把数据写入 banked_store（经 sinkD 通路）并经 `sche_in_d` 把 `ACCESSACKDATA` 回送 L1（[sourceD.v:591-595](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/l2cache/sourceD.v#L591-L595)）。

**交付物**：一张时序图，横轴为时钟拍，纵轴为各模块信号，标出：①sinkA 收请求 ②directory miss ③MSHR 分配 ④sourceA 发 GET ⑤sinkD 收响应 ⑥banked_store 写入 ⑦sourceD 回 D。若某段时序你无法从源码确定具体拍数，标注「待本地验证」并在 Verdi 波形中确认。

**进阶**：若上面流程中 victim 是脏块，额外的 STAGE_3 写回步骤插在第 4 步之前——请在图中补出这条支路，并指出 `mshr_wait` 在此期间如何阻塞其他 MSHR 的 GET。

## 6. 本讲小结

- L2 是一个**包含式缓存**，顶层 `Scheduler` 用四条 TileLink 通道（`in_a`/`in_d` 收自发往 L1，`out_a`/`out_d` 收发外部存储）连接系统，内部按「sink 收、source 发」拆成 `sinkA`/`sinkD`/`sourceA`/`sourceD` 四个边界模块。
- `directory_test` 是 tag 账本：组相联查表判命中、`lru_matrix` 选牺牲路、valid/dirty 位维护，并支持 flush/invalidate 的全表扫描。
- 缺失处理由 `MSHRS=4` 个 **MSHR** 承担：主缺失分配新表项，次缺失（tag/set 匹配）合并挂到 `requests` Listbuffer 链上；每个 MSHR 有 schedule_a（发内存 GET）、schedule_d（回 L1）、schedule_dir（回填 tag）三路输出。
- 多个 MSHR 共享 sourceA/sourceD/directory 写口，靠**轮询调度**（`mshr_select`）分时复用；目录结果与内存写回各经一个 FIFO（`dir_result_buffer`/`writebuffer`）解耦，避免死锁。
- `Listbuffer` 同一份 RTL 身兼两职：sinkA 里作 putbuffer 存写数据，Scheduler 里作 `requests` 存次缺失请求；`banked_store` 按 8 bank 分体存数据，sinkD 填充与 sourceD 写命中分时写入。
- `sourceD` 是 8 态 FSM 的响应总指挥，产生 `finish_issue`——它在 flush 最后一拍脉冲，上传到 GPGPU_top 与 `is_flushing` 一起拉起 `host_rsp_valid_o`，闭合 workgroup 的「完成→冲刷→回报主机」生命周期。

## 7. 下一步学习建议

- **u7-l3 cluster 到 L2 的互联**：本讲把 L2 当作一个有四通道端口的黑盒，下一讲打开它的外部——多个 SM 的请求如何经 `sm2cluster_arb`→`l2_distribute`→`cluster_to_l2_arb` 汇聚到 `Scheduler`，D 通道响应又如何按 source 路由回原 SM。
- **重读 u7-l1 的 source 编码**：现在你已看到 L2 内部如何产生与消费 source（L2 发内存请求时 `a_source='d4`），可回头印证 source 字段的「分级回信地址」语义。
- **对比 L1 与 L2 的 MSHR**：建议并排读 u6-l1 的 `l1_mshr`（entry×subentry 二级表）与本讲的 `MSHR` + `requests` Listbuffer，体会「L1 单 SM 私有、追求低延迟」与「L2 多 SM 共享、追求聚合与包含」的设计差异。
- **动手验证**：跑一个 tc_vecadd 用例（u1-l4），在 Verdi 中抓 `SourceD_finish_issue_o` 信号，观察它在每个 workgroup 结束时是否脉冲一次，并与 `host_rsp_valid_o` 对齐，验证本讲对 finish_issue 链路的描述。
