# 存储子系统：非阻塞缓存与 L2/主存

## 1. 本讲目标

本讲往下钻到向量核访存的「最后一公里」——缓存与主存子系统。学完后你应该能够：

- 说清 `data_cache` 这一级**非阻塞（non-blocking）数据缓存**的存储格式 `{Valid}{Dirty}[Tag][Data]`、多端口 SRAM 组织，以及它如何用一个 `serve` 状态机在标量 load/store 与向量 `mem_req` 之间仲裁。
- 说清 `main_memory` 如何用一个 `fifo_dual_ported` 请求队列 + 一个移位寄存器式延迟计数器，模拟出「REALISTIC」的 L2/主存固定延迟。
- 说清 `ld_st_buffer`（load/store 缓冲）与 `wait_buffer`（等待缓冲）如何实现 **miss-under-miss**（miss 未命中之下继续接收新 miss），以及 `ld_st_buffer` 的 search 端口如何做 **store-to-load 转发**。
- 说清一条向量 `mem_req` 如何进入缓存、被 `vdata_operation` 拼装成宽事务、跨越两条 cache 行（multi-line fetch）时如何处理，最后以 `vector_mem_resp` 返回 VMU。

本讲承接 u3-l1：VMU 的 load/store/tile-prefetch 三个引擎仲裁出一个 `vector_mem_req`，本讲就从这个请求落地开始讲。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**为什么要「非阻塞」缓存？** 普通缓存遇到一次 miss 就会把整个缓存卡住，等数据从下层取回。但向量核一拍能发射多个元素、一条指令又能展开成多个 micro-op，如果一次 miss 就 stall 整条访存流，吞吐会被严重拖累。非阻塞缓存允许「**miss 还没回来时继续接收新的 load/store 请求**」，把已经在缓存里的请求先服务掉，这就是术语 **miss-under-miss**（也叫 hit-under-miss）。代价是要用缓冲（load buffer / store buffer / wait buffer）记录所有「正在等」的请求，等下层把数据送回来再逐一服务。

**store-to-load 转发解决什么？** 假设程序里先 `store x→[addr]`，紧接着 `load [addr]`。这条 store 还没真正写进缓存（可能还在 store buffer 里排队），load 如果去读缓存就会读到旧值。解决办法是：load 来时先用它的地址去 **search** store buffer，若命中一条更老的、地址重叠的 store，就直接把 store 的数据转发给 load，不必等 store 落盘。这就叫 store-to-load forwarding。

**L2/主存模型为什么要「假装」有延迟？** 本仓库是仿真用 RTL，没有真实 DRAM。`main_memory` 用一段 `$readmemh` 加载的 RAM 当主存，但真实 DRAM 访问要几十拍。为了不让仿真里的 miss 看起来「太快」，它加了一个可配置的 `DELAY_CYCLES` 延迟计数器，用 `REALISTIC=1` 开关决定是否启用——这就是「REALISTIC 延迟模型」。

几个术语速查：

| 术语 | 含义 |
|---|---|
| cache 行（line/block） | 缓存与主存交换数据的最小单位，本项目 D-cache 行宽 512 bit（64 B） |
| `{Valid}{Dirty}[Tag][Data]` | 一条 cache 项的格式：有效位、脏位、地址标签、数据 |
| 命中（hit）/ 未命中（miss） | 请求地址在缓存里 / 不在 |
| LRU | 最近最少使用，本缓存的替换策略 |
| miss-under-miss | 一次 miss 未解决时仍能接受并处理新请求 |
| store-to-load forwarding | 把未落盘的 store 数据直接转给后续 load |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `rtl/shared/data_cache.sv` | 非阻塞数据缓存本体：SRAM 阵列、`serve` 状态机、load/store/wait 三缓冲、向量请求通路（`vld_st_buffer` + `vdata_operation`）、与 L2 的读/写/回填接口 |
| `rtl/shared/main_memory.sv` | L2/主存行为模型：`$readmemh` 加载内容、双推 FIFO 请求队列、REALISTIC 移位延迟计数器 |
| `rtl/shared/ld_st_buffer.sv` | load 缓冲与 store 缓冲的共用模块（例化两次）；提供 search 端口做 store-to-load 转发、update 端口响应 L2 回填 |
| `rtl/shared/wait_buffer.sv` | 等待缓冲：保存因更老的 miss 而必须等待的请求，IDLE/WALK 状态机在回填后批量唤醒 |
| `rtl/vector/vdata_operation.sv` | 向量宽事务的数据拼装：load 时从 cache 行里截取并对齐、store 时把数据合并进 cache 行；处理跨行（2×BLOCK_W）输入 |

辅助模块（本讲只点到为止）：`sram.sv`（带参数化读写端口的 SRAM 行为模型）、`and_or_mux.sv`（one-hot 选通的多路选择器）、`arbiter.sv`、`onehot_detect.sv`、`lru.sv`、`fifo_dual_ported.sv`、`data_operation.sv`（标量版数据拼装）、`vld_st_buffer.sv`（向量版 load/store 缓冲）。

## 4. 核心概念与源码讲解

### 4.1 非阻塞数据缓存（data_cache）

#### 4.1.1 概念说明

`data_cache` 是整个存储子系统的核心。它的设计目标是：在标量核与向量核**共享同一份缓存阵列与同一个 L2 端口**的前提下，既支持标量侧的非阻塞 load/store，又支持向量侧的宽 `mem_req`。

它的存储格式在文件头注释里写得很清楚：

> Cache configuration: `{Valid}{Dirty}[Tag][Data]`

也就是说，每一条 cache 项有四个字段：有效位（Valid）、脏位（Dirty）、地址标签（Tag）、数据（Data）。Dirty 位在替换时用来判断要不要把这一行写回 L2。

关键参数（注意模块默认值与实际例化值不同，**以 `vector_sim_top.sv` 的例化为准**）：

| 参数 | 模块默认 | 实际例化（vector_sim_top） | 含义 |
|---|---|---|---|
| `ASSOCIATIVITY` | 4 | `DC_ASC=4` | 4 路组相联 |
| `ENTRIES` | 256 | `DC_ENTRIES=32` | 每路 32 个 set |
| `BLOCK_WIDTH` | 256 | `DC_DW=512` | 每行 512 bit（64 B） |
| `DATA_WIDTH` | 32 | 32 | 单元素 32 bit |
| `BUFFER_SIZES` | 4 | 4 | load/store 缓冲深度 |
| `VECTOR_ENABLED` | 1 | 1 | 开启向量通路 |

由此可以推出地址拆分（见 [rtl/shared/data_cache.sv:78-81](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L78-L81)）：

\[
\text{OFFSET\_BITS} = \lceil \log_2(\text{BLOCK\_WIDTH}/\text{DATA\_WIDTH}) \rceil + 2 = \lceil \log_2 16 \rceil + 2 = 6
\]

\[
\text{INDEX\_BITS} = \lceil \log_2(\text{ENTRIES}) \rceil = \lceil \log_2 32 \rceil = 5
\]

\[
\text{TAG\_BITS} = 32 - 5 - 6 = 21
\]

即一个 32 位地址被切成 `[21 位 Tag][5 位 Index][6 位 Offset]`，6 位 offset 正好寻址 64 B 行内的每一个字节。整缓存数据容量 \(4 \times 32 \times 64\,\text{B} = 8\,\text{KB}\)。

#### 4.1.2 核心流程

`data_cache` 的核心是一个组合 `serve` 状态机，每拍从多个候选请求里**按固定优先级选一个服务**。候选与优先级如下（最高 → 最低）：

1. **WT**（wait buffer walk）：正在批量唤醒等待缓冲里的请求。
2. **LD_IN / ST_IN**：新进来的标量 load / store（来自 `load_valid`/`store_valid` 端口）。
3. **LD / ST**：已经在 load/store buffer 队头、且数据已取回（`*_head_isfetched`）的请求。
4. **VCT_LD / VCT_ST**：向量 load / store（来自 `vld_st_buffer` 队头）。
5. **IDLE**：本拍无事可做。

一条标量 load（`LD_IN`）的服务流程是本缓存的「标准动作」，看懂它就懂了非阻塞的精髓：

```text
load_valid 进来 → serve=LD_IN
  ├─ 先 search wait buffer：命中则转发 wt_s_frw_data（store-to-load，跨缓冲版）
  ├─ 再 search store buffer：命中则转发 st_s_data（store-to-load）
  ├─ 否则查缓存：Hit → 直接取 served_data
  └─ 都不中（miss）：
       ├─ 若同 cache 行已有在途 miss（partial hit）→ 进 wait buffer 等回填
       └─ 否则 → 进 load buffer，并向 L2 发 request_l2
```

miss 之后，L2 的回填通过 `update_l2_valid` 端口送回，缓存会用它同时做三件事：把新行写进 SRAM、置 Valid、并把 load/store buffer 里所有「等这一行」的请求标记为 `isfetched`（已取回），下一拍它们就能走 LD/ST 路径被服务。这就是 **miss-under-miss**：第一条 miss 还没回来时，后续请求照常进入缓冲排队，回填后被批量唤醒。

#### 4.1.3 源码精读

**端口总览**——注意它同时有标量端口与向量端口（[rtl/shared/data_cache.sv:39-76](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L39-L76)）：标量侧是 `load_valid/load_address/...` 与 `store_valid/store_address/store_data/...`；向量侧是 `mem_req_valid_i/mem_req_i/cache_vector_ready_o`；与 L2 之间有「写回（write_l2）」「读请求（request_l2）」「回填（update_l2）」三组端口。

**缓存阵列的组织**——采用 packed 三维数组，按路、按 set 存放 Tag/Data/Valid/Dirty（[rtl/shared/data_cache.sv:95-97](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L95-L97)）：

```systemverilog
logic [ASSOCIATIVITY-1 : 0][BLOCK_WIDTH-1 : 0]  data, overwritten_data;
logic [ASSOCIATIVITY-1 : 0][TAG_BITS-1 : 0]     tag, overwritten_tag;
logic [ASSOCIATIVITY-1 : 0][ENTRIES-1:0]        validity, dirty;
```

这里 `overwritten_data/overwritten_tag` 保存的是「即将被替换掉的那一行的旧值」，供 writeback 时使用（见 [rtl/shared/data_cache.sv:151-153](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L151-L153)）：当 L2 回填要占用一个全满 set 时，挑 LRU 那一路，若它 Dirty 则把旧数据写回 L2。

**SRAM 行为模型与读端口数**——本缓存用 `sram.sv` 例化每路的 Tag bank 与 Data bank。关键是读端口数随 `VECTOR_ENABLED` 变化（[rtl/shared/data_cache.sv:199-273](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L199-L273)）：

- `VECTOR_ENABLED=1`：每路 3 个读端口——`read_line_select`（主服务口 A）、`write_line_select`（回填写口）、`read_line_select_b`（**向量专用的第二读口 B**）。
- `VECTOR_ENABLED=0`：每路 2 个读端口，省掉向量第二口。

这就解释了「向量与标量如何共存」的一半：**向量请求享有一个独立的第二读口 B**（`FindEntry_B`，[rtl/shared/data_cache.sv:305-320](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L305-L320)），使得跨两条 cache 行的向量请求可以**同一拍同时查两口**（Hit_a 与 Hit_b），而标量请求只用 A 口。

**`serve` 状态机与优先级**——枚举与选择逻辑（[rtl/shared/data_cache.sv:133](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L133) 与 [rtl/shared/data_cache.sv:564-598](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L564-L598)）：

```systemverilog
typedef enum logic [2:0] {IDLE, LD_IN, ST_IN, LD, ST, WT, VCT_LD, VCT_ST} serve_mode;
```

注意几点：① 标量新请求（`LD_IN/ST_IN`）要求 `!output_used`（输出端口空闲）；② `LD/ST` 要求 `*_head_isfetched`（数据已取回）且 ST 还要求 `!update_l2_valid`（不要和回填抢写口）；③ 向量 `VCT_LD/VCT_ST` 只看 `vct_valid` 与 `vct_head_isstore`，且 ST 要求 `!update_l2_valid`。

**共存的关键约束**——既然标量与向量共享同一阵列与写口，二者不能在同一拍都占用服务通路。源码用断言强制这一点（[rtl/shared/data_cache.sv:922-924](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L922-L924)）：

```systemverilog
assert property (@(posedge clk) disable iff(!rst_n | !VECTOR_ENABLED)
   (store_valid | load_valid) |-> (!vct_valid && !mem_req_valid_i))
   else $error("ERROR:Data_Cache: Cannot issue scalar mem ops while vector mem op in flight");
```

即**标量与向量访存不能同时在场**——这是当前实现的简化前提。在仓库公开的向量仿真里，标量端口其实是被常 `1'b0` 拉低的（见下方实践），所以这条断言在实际跑向量程序时不会触发。

> 一个值得注意的小细节：`DataServe` 里 `serve==VCT_LD` 的分支被写了两遍（[rtl/shared/data_cache.sv:408-417](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L408-L417)），第二个分支永远不可达，是一段无害的重复代码（dead code）。读源码时不必困惑。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：搞清「向量 `mem_req` 与标量 load/store 端口如何在同一个 `data_cache` 里共存」。

**操作步骤**：

1. 打开 [vector_simulator/vector_sim_top.sv:167-218](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/vector_simulator/vector_sim_top.sv#L167-L218)，观察 `data_cache` 的例化。
2. 找到 `load_valid (1'b0)`、`store_valid (1'b0)`——确认在本仓库的向量仿真里，**标量 load/store 端口被常拉低**，只有 `.mem_req_valid_i(mem_req)` 这一对接入 VMU。
3. 回到 [rtl/shared/data_cache.sv:564-598](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L564-L598) 的 `ServePriority`，列出标量与向量各自进入 `serve` 状态机的条件。
4. 对照 [rtl/shared/data_cache.sv:922-924](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L922-L924) 的断言，回答：如果将来把标量核接回来，这条断言对双发射调度器提出了什么要求？

**需要观察的现象 / 预期结果**（无需运行，结论性）：

- 共存的三个层次：① **共享阵列与 L2 端口**（同一个 SRAM bank、同一组 `request_l2/write_l2/update_l2`）；② **共享 `serve` 仲裁**（固定优先级，WT > 标量新请求 > 标量已就绪 > 向量）；③ **向量额外享有第二读口 B** 做跨行同时查。
- 共存的代价：标量与向量**不得同拍在场**，调度器必须串行化二者（这正是上面那条断言的含义）。
- 在当前向量仿真里，因为标量端口恒 0，`serve` 实际只会在 `VCT_LD/VCT_ST/IDLE`（以及 wait buffer walk）之间切换。

> 待本地验证：若你能在 QuestaSim 里把 `load_valid` 偶尔拉高一拍同时有向量请求在途，应能看到上面那条 `$error` 断言触发——这是验证「不得共存」约束最直接的方式。

#### 4.1.5 小练习与答案

**练习 1**：把 `ASSOCIATIVITY` 从 4 改成 1，`FindEntry` 与写使能逻辑会发生什么变化？
**答案**：单路直接映射。`FindEntry` 的 for 循环仍工作但只迭代一次；写使能走 [rtl/shared/data_cache.sv:475-478](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L475-L478) 的 `ASSOCIATIVITY==1` 分支，不再需要 LRU 模块（`lru` 的 generate 条件是 `ASSOCIATIVITY>1`），替换时直接覆盖唯一一路。

**练习 2**：`cache_blocked` 的定义是 `~ld_ready | ~st_ready | ~wt_ready | wt_in_walk`（[rtl/shared/data_cache.sv:561](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L561)）。为什么它没有包含向量缓冲的满/空？
**答案**：`cache_blocked` 描述的是**标量侧**的反压（标量 load/store/wait 缓冲满了或正在 walk）。向量侧有自己的握手 `cache_vector_ready_o`（由 `vld_st_buffer.ready_o` 驱动），通过 `mem_req_valid_i & cache_vector_ready_o` 单独反压 VMU，二者各自独立。

---

### 4.2 L2/主存模型（main_memory）

#### 4.2.1 概念说明

`main_memory` 不是真实的 DRAM 控制器，而是一个**行为级（behavioral）的 L2/主存模型**，目的是给缓存提供「有合理延迟的下层存储」。它做三件事：

1. 用 `$readmemh(FILE_NAME, ram)` 在仿真启动时把一段 hex 文件加载进一个二维 RAM 数组，作为主存的初始内容（程序数据就靠它喂入，见 u1-l5 提到的 `init_main_memory.txt`）。
2. 同时服务 icache 读、dcache 读、dcache 写三类请求，用一个深度 8 的双推 FIFO 排队，**每拍只出队一个**（顺序服务、保序）。
3. 用一个移位寄存器式的计数器给每个请求加上可配置的 `DELAY_CYCLES` 延迟，模拟真实主存延迟（`REALISTIC` 开关）。

它的容量参数（实际例化）：`L2_BLOCK_DW=L2_DW=1024`（128 B/行）、`L2_ENTRIES=2048`，总容量 \(2048 \times 128\,\text{B} = 256\,\text{KB}\)。

#### 4.2.2 核心流程

```text
每拍：
  ├─ CreateNewEntries：把 icache 读 / dcache 读 / dcache 写 三路请求，
  │    组装成 SavedEntry 结构体，最多挑两路 push 进 instruction_queue
  ├─ instruction_queue（fifo_dual_ported，深度 8）保序排队
  ├─ 队头 valid 时启动 delay_counter（一位热码，逐拍左移）
  └─ delay_counter 移出边界那一拍 → delayed_valid 拉高：
       ├─ pop 队头
       ├─ 读：按 line_selector 取 ram 的一行，按 block offset 切出子块输出
       └─ 写：把 output_entry.data 写进 ram 对应行的子块
```

地址拆分（[rtl/shared/main_memory.sv:52-56](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/main_memory.sv#L52-L56)）：以 L2 块 1024 bit 为例，`OFFSET_BITS=$clog2(1024/8)=7`，`INDEX_BITS=$clog2(2048)=11`。注意 L2 块比 D-cache 块（512 bit）大一倍，所以一个 L2 块里能切出 2 个 D-cache 块，`DBLOCK_OFFSET=$clog2(1024/512)=1` 位用来选这 2 个子块之一（[rtl/shared/main_memory.sv:194-202](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/main_memory.sv#L194-L202)）。

#### 4.2.3 源码精读

**请求结构体**——一个 `SavedEntry` 记录「这是读还是写、是 icache 还是 dcache、地址、数据」（[rtl/shared/main_memory.sv:13-19](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/main_memory.sv#L13-L19)）：

```systemverilog
typedef struct packed {
    logic           read_icache ;
    logic           read_dcache ;
    logic           write_dcache;
    logic [ 32-1:0] address     ;
    logic [256-1:0] data        ;
} SavedEntry;
```

**请求仲裁与入队**——`CreateNewEntries` 是纯组合，把最多两路请求组装好并给出 `push_1/push_2`（[rtl/shared/main_memory.sv:78-131](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/main_memory.sv#L78-L131)）。icache 优先占第一槽，dcache 读/写竞争第二槽。入队后由 `fifo_dual_ported`（深度 8）保序（[rtl/shared/main_memory.sv:133-158](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/main_memory.sv#L133-L158)）。注意 `pop_2(1'b0)`——**每拍只出队一个请求**，这是保序的关键。

**REALISTIC 延迟计数器**——这是本模块最巧妙的一段（[rtl/shared/main_memory.sv:160-185](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/main_memory.sv#L160-L185)）：

```systemverilog
case (counter_active)
    1'b1 : if(~|delay_counter) counter_active <= 1'b0;
           else delay_counter <= delay_counter << 1;   // 一位热码逐拍左移
    1'b0 : if(valid) begin counter_active <= 1'b1; delay_counter <= 1; end
endcase
...
generate
    if(REALISTIC) assign delayed_valid = counter_active & ~|delay_counter;
    else          assign delayed_valid = valid;        // 理想 0 延迟
endgenerate
```

原理：请求到达时把 `delay_counter` 置为 1（bit0 热），之后每拍左移一位。它宽 `DELAY_CYCLES` 位，经过约 `DELAY_CYCLES` 拍后这一位被移出边界、全寄存器归零，`~|delay_counter` 成立，`delayed_valid` 就在这一拍拉高、同时 `pop` 出队。这等价于给每个请求加上了**约 `DELAY_CYCLES` 拍**的固定延迟。例化时 `DELAY_CYCLES=10`、`REALISTIC=1`（[rtl/shared/params.sv:42-44](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/params.sv#L42-L44)），所以一次 L2 访问大约要等 10 拍。

**RAM 写管理**——只在 `delayed_valid & output_entry.write_dcache` 时，把数据写进对应行的子块（[rtl/shared/main_memory.sv:204-208](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/main_memory.sv#L204-L208)）；读则纯组合地从 `ram[line_selector]` 切出子块（[rtl/shared/main_memory.sv:194-202](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/main_memory.sv#L194-L202)）。

#### 4.2.4 代码实践（源码阅读型 + 可选修改观察）

**实践目标**：理解 REALISTIC 延迟开关对仿真行为的影响。

**操作步骤**：

1. 阅读 [rtl/shared/main_memory.sv:179-185](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/main_memory.sv#L179-L185)，确认 `REALISTIC=0` 时 `delayed_valid=valid`（0 延迟理想主存）。
2. 阅读 [rtl/shared/params.sv:42-44](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/params.sv#L42-L44)，记录 `REALISTIC=1`、`DELAY_CYCLES=10`。
3. （可选，需本地 QuestaSim）分别用 `REALISTIC=0` 和 `REALISTIC=1` 跑同一个示例（如 saxpy），对比 `perf_results/results.log` 里的总周期数与 stall 计数。

**需要观察的现象 / 预期结果**：

- `REALISTIC=0` 时主存「0 延迟」，miss 几乎不花时间，总周期会明显更小、`stall_pending` 类指标更低。
- `REALISTIC=1` 时每次 L2 访问约 10 拍，miss 代价显著上升，总周期变大、与访存相关的 stall 上升。
- 这是调优时区分「计算瓶颈」与「访存瓶颈」的关键旋钮（详见 u4-l7）。

> 待本地验证：上述周期数对比需在本机跑仿真后才能给出具体数字。

#### 4.2.5 小练习与答案

**练习 1**：`main_memory` 每拍只 `pop` 一个请求（`pop_2=1'b0`）。如果 dcache 同时有读 miss 和写回两路请求，会怎样？
**答案**：两路请求会先竞争 `push_1/push_2` 进 `instruction_queue`（icache 优先第一槽，dcache 读/写争第二槽），进队后**严格按 FIFO 顺序、每拍出一个**被服务，写回不会插队到读前面，保证了程序顺序语义。

**练习 2**：为什么延迟计数器用「一位热码左移」而不是一个普通递减计数器？
**答案**：功能上等价（都数 `DELAY_CYCLES` 拍）。一位热码在硬件上每拍只做一次移位、无需加减法器，且 `~|delay_counter` 的「全零」检测就是一个宽 NOR，时序友好；这里它是行为模型，更多是作者的风格选择，读者用递减计数器重写也能得到同样的延迟语义。

---

### 4.3 缓冲与转发：load/store/wait 三缓冲（ld_st_buffer + wait_buffer）

#### 4.3.1 概念说明

非阻塞的「魔法」全部落在三个缓冲上：

- **load buffer**（`ld_st_buffer` 例化）：存放 miss 了、正在等 L2 回填的标量 load。
- **store buffer**（`ld_st_buffer` 例化）：存放还没写进缓存的标量 store；同时充当 **store-to-load 转发**的数据源。
- **wait buffer**（`wait_buffer`）：存放「因为更老的同类 miss 在途、必须等待」的请求；回填后用 WALK 模式批量唤醒。

`ld_st_buffer` 被例化两次（load buffer 与 store buffer 共用同一模块，只是端口接法不同），它本质上是一个**深度为 `DEPTH` 的环形 FIFO**，但额外提供两个关键功能：

1. **search 端口**：用当前要服务的地址去全表搜索，命中则输出 `search_data`——这是 store-to-load 转发的硬件基础。
2. **update 端口**：当 L2 回填某一 cache 行时，把所有「等这一行」的表项标记为 `isfetched`。

#### 4.3.2 核心流程

**store-to-load 转发**（标量 load 进来时）：

```text
load(LD_IN) 的 served_address → 同时送进：
  ├─ store_buffer.search_address
  │    └─ 命中（st_s_vhit）→ 转发 st_s_data 给 load，跳过缓存
  ├─ wait_buffer.search_address
  │    └─ 命中（wt_s_frw_hit）→ 转发 wt_s_frw_data 给 load
  └─ 缓存阵列 → Hit 则读缓存；miss 则进 load buffer 等
```

**miss-under-miss**（一条 miss 在途，又来一条同行的请求）：

```text
请求 B 发现同 cache 行已有在途 miss（partial hit: st_s_phit | ld_s_phit）
  → 不再向 L2 重复发请求，而是进 wait buffer
L2 回填该行（update_l2_valid）
  → load/store buffer 的 update 端口把等这一行的表项标 isfetched
  → wait_buffer 进入 WALK 模式，逐拍用 peek 指针把所有等这一行的请求服务掉
```

#### 4.3.3 源码精读

**`ld_st_buffer` 的存储与指针**——用 one-hot 的 `head`/`tail` 指针和一个 `stat_counter` 移位寄存器表示占用数（[rtl/shared/ld_st_buffer.sv:63-80](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/ld_st_buffer.sv#L63-L80)）。`valid=~stat_counter[0]`（非空）、`ready=~stat_counter[DEPTH-1]`（非满）。push 时 `tail` 循环左移、`stat_counter` 左移（+1）；pop 时 `head` 循环左移、`stat_counter` 右移（-1）。所有出队字段（地址/数据/microop/ticket）都经 `and_or_mux` 用 `head` 选通输出（[rtl/shared/ld_st_buffer.sv:199-245](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/ld_st_buffer.sv#L199-L245)）。

**search 的两级匹配**——这是 store-to-load 转发的核心（[rtl/shared/ld_st_buffer.sv:84-117](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/ld_st_buffer.sv#L84-L117)）：

```systemverilog
matched_search_main[i] = (search_address[ADDR-1:BLOCK_ID_START] == saved_address[i][...]); // tag+index
matched_search_sec[i]  = (search_address[BLOCK_ID_START-1:0]   == saved_address[i][...]); // 块内偏移
matched_search[i]      = matched_search_main[i] & matched_search_sec[i];
...
search_valid_hit = |(matched_search & microop_ok);   // 完全命中且尺寸兼容
search_partial_hit = |matched_search_main;            // 同 cache 行（部分命中）
```

- `search_valid_hit`（完全命中）：地址全等 **且** `microop_ok`（尺寸兼容，见下）→ 可直接转发数据。
- `search_partial_hit`（部分命中）：只有 tag+index 相同（同一 cache 行）但块内偏移不同 → 表示「同一行有在途 miss」，用来触发 miss-under-miss 的等待逻辑（在 `data_cache` 里就是 `ld_s_phit`/`st_s_phit`）。

**`microop_ok`：尺寸兼容检查**——不是任何 store 都能转发给任何 load。一条 LW（读 4 字节）只能由一条覆盖完整 4 字节的 SW 转发；反之 LB（读 1 字节）则可由任何覆盖该字节的 store 转发（[rtl/shared/ld_st_buffer.sv:94-107](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/ld_st_buffer.sv#L94-L107)）：

```systemverilog
if(search_microop_in==5'b00001)        microop_ok = (search_microop==5'b00110);   // LW ← SW
else if(search_microop_in==5'b00010
     || search_microop_in==5'b00011)   microop_ok = (search_microop==5'b00111
                                                   || search_microop==5'b00110);  // LH ← SH/SW
else if(search_microop_in==5'b00100
     || search_microop_in==5'b00101)   microop_ok = 1;                           // LB ← 任意
```

**`update` 端口：L2 回填唤醒**——回填到达时，把等这一行的表项置 `isfetched`（[rtl/shared/ld_st_buffer.sv:119-195](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/ld_st_buffer.sv#L119-L195)）。在 `data_cache` 里，`ld_head_isfetched`/`st_head_isfetched` 一旦为真，队头就能走 `LD`/`ST` 路径被服务（见 [rtl/shared/data_cache.sv:577-584](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L577-L584)）。

**store-to-load 转发的实际接线**——在 `data_cache` 里，store buffer 的 search 命中直接喂给 load 的服务逻辑（[rtl/shared/data_cache.sv:703-710](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L703-L710) 与 [rtl/shared/data_cache.sv:323-336](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L323-L336)）：

```systemverilog
// store_buffer 例化：search_data → st_s_data，search_valid_hit → st_s_vhit
.search_data      (st_s_data),
.search_valid_hit (st_s_vhit),
...
// DataServe 的 LD_IN 分支：
if(wt_s_frw_hit)        served_output.data = wt_s_frw_data;   // 1) wait buffer 转发
else if(st_s_vhit)      served_output.data = st_s_data;        // 2) store buffer 转发
else if(Hit)            served_output.data = served_data;      // 3) 缓存命中
else                    /* miss → 进缓冲 */
```

转发优先级是 **wait buffer > store buffer > 缓存**——因为 wait buffer 里存的是更老的请求，理应优先。

**`wait_buffer` 的 WALK 模式**——当一次 `LD`/`ST` 服务触发 `wt_invalidate`（[rtl/shared/data_cache.sv:728](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L728)），`wait_buffer` 进入 `WALK` 状态（[rtl/shared/wait_buffer.sv:138-155](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/wait_buffer.sv#L138-L155)）：用一个 `peek` 指针配合 `arbiter`（按年龄 oldest→newest 选，[rtl/shared/wait_buffer.sv:104-110](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/wait_buffer.sv#L104-L110)）逐拍把所有匹配刚回填行的请求服务掉、并清掉它们的 valid（[rtl/shared/wait_buffer.sv:213-225](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/wait_buffer.sv#L213-L225)），直到没有多个匹配（`!multi_found`）才回 `IDLE`。wait buffer 还有一个反向仲裁器 `frw_arbiter`（newest→oldest，[rtl/shared/wait_buffer.sv:238-244](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/wait_buffer.sv#L238-L244)）用来选**最新**的一条匹配做转发数据 `search_frw_data`——因为越新的 store 数据越「正确」。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：说清 `ld_st_buffer` 的 search 端口如何完成 store-to-load 转发。这是本讲规格里要求讲透的一条链路。

**操作步骤**：

1. 在 [rtl/shared/data_cache.sv:683-725](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L683-L725) 找到 `store_buffer` 例化，确认它的 `.search_address(served_address)`、`.search_data(st_s_data)`、`.search_valid_hit(st_s_vhit)`。
2. 在 [rtl/shared/ld_st_buffer.sv:84-117](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/ld_st_buffer.sv#L84-L117) 看 `MatchSearch`/`ValidHit`：`search_valid_hit` 需要「地址全等 **且** `microop_ok`」。
3. 在 [rtl/shared/ld_st_buffer.sv:94-107](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/ld_st_buffer.sv#L94-L107) 看 `microop_ok`：一条 LW 能从 SW 转发，但反之不行。
4. 在 [rtl/shared/data_cache.sv:323-336](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L323-L336) 看 `DataServe` 的 `LD_IN` 分支如何把 `st_s_data` 作为转发结果输出。

**需要观察的现象 / 预期结果**（结论性，无需运行）：

- 一条标量 load 进来时，它的地址 `served_address` 被**同时**送给 store buffer 的 search 端口。
- 若 store buffer 里有一条**更老的、地址完全相同、且尺寸兼容**的 store（`st_s_vhit=1`），load 直接拿 `st_s_data` 作为结果，**完全不读缓存、不等写盘**——这就是 store-to-load 转发。
- 若只命中 `search_partial_hit`（同一 cache 行、不同偏移），则触发的是 miss-under-miss 等待，而非转发。

**小提醒**：当前向量仿真里标量端口被拉低（见 4.1.4），所以 store-to-load 转发这条标量链路实际不会被触发；但 RTL 完整保留，等标量核接入即可工作。向量侧的「转发」由 `vld_st_buffer` + VMU 内部的双行 scratchpad / 计分板机制承担（见 u3-l2）。

#### 4.3.5 小练习与答案

**练习 1**：`search_partial_hit`（部分命中）只比较 tag+index，不比较块内偏移。它被 `data_cache` 用来做什么？
**答案**：它是 **miss-under-miss 的检测信号**。当一条新请求的 cache 行与某条在途 miss 的行相同（`ld_s_phit`/`st_s_phit`），说明该行已经在被取，不应再向 L2 发第二次请求，于是把新请求送进 wait buffer 等同一行回填（见 [rtl/shared/data_cache.sv:348-354](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L348-L354) 与 [rtl/shared/data_cache.sv:362-368](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L362-L368)）。

**练习 2**：`wait_buffer` 的深度被例化为 `2*BUFFER_SIZES`（[rtl/shared/data_cache.sv:761](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L761)），是 load/store buffer 的两倍。为什么它要更大？
**答案**：wait buffer 同时收纳**因同行 miss 而等待的 load 与 store**，是 load buffer 与 store buffer 两路溢流的合集，所以容量按两倍设置，避免在 miss 集中爆发时过早把 `cache_blocked` 拉高。

**练习 3**：`frw_arbiter` 为什么用 newest→oldest 的优先级，而普通 `arbiter` 用 oldest→newest？
**答案**：WALK 时要按**程序顺序**逐个服务等待项（oldest 先），所以选「下一个要服务的」用 oldest→newest；而转发数据要取**最新鲜**的 store 值（newest 最后写、值最正确），所以选转发源用 newest→oldest。两个方向服务于两个不同目的。

---

### 4.4 向量请求的接入与宽事务拼装（vdata_operation + multi-line fetch）

#### 4.4.1 概念说明

向量 `mem_req` 一次可能搬运多达 `VECTOR_REQ_WIDTH`（例化为 `VECTOR_MAX_REQ_WIDTH`，256 bit）的数据，比单条标量 load/store（32 bit）宽得多，而且一次请求可能**横跨两条 cache 行**（因为起始偏移 + 数据宽度可能越过行边界）。`data_cache` 用 `VECTOR_ENABLED` generate 块专门处理这两点：

- 用 `vld_st_buffer`（向量版 load/store 缓冲，深度 4）缓存未完成的向量请求。
- 用 `vdata_operation` 做宽事务的数据拼装（load 截取对齐、store 合并）。
- 用一个小 FSM 检测并处理**跨两条 cache 行**的请求（multi-line fetch）。

`vdata_operation` 是 `data_operation`（标量版）的「宽位版」：它的 `input_block` 是 `2*BLOCK_W` 宽，正好容下相邻两条 cache 行，从而无论请求落在行的哪个位置、是否跨行，都能用同一套移位 + 掩码逻辑切出/合并数据。

#### 4.4.2 核心流程

**向量 load（VCT_LD）**：

```text
mem_req_i → vld_st_buffer.push → 队头 vct_head_addr/size/microop
  ├─ line_select_a = addr 的 index
  ├─ line_select_b = (addr + size - 1) 的 index   ← 末字节所在行
  ├─ nxt_multi_fetches_needed = (line_select_a != line_select_b)  ← 是否跨行
  ├─ A 口查 Hit_a；若跨行，B 口查 Hit_b
  ├─ 两行都命中（或单行命中）→ vct_pop
  │    └─ vdata_operation：把 {block_b, block_a} 拼成 2×BLOCK_W，
  │        按 offset 右移、按 size 掩码 → output_vector(256 bit)
  └─ 任一行 miss → request_l2_a / request_l2_b 向 L2 取该行
向量响应：vector_resp.{ticket,size,data} → VMU
```

#### 4.4.3 源码精读

**跨行检测与 multi-fetch FSM**——末字节地址 `vct_head_addr_b = vct_head_addr + vct_head_size - 1`（[rtl/shared/data_cache.sv:815-818](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L815-L818)），比较首尾两字节所在行号判断是否跨行。`vct_head_requested[1:0]` 记录两行各自的「已请求」状态，确保 A 行 miss 先发、B 行 miss 再发，两行都到位才 pop（[rtl/shared/data_cache.sv:831-845](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L831-L845)）：

```systemverilog
assign vct_pop = (multi_fetches_needed | nxt_multi_fetches_needed)
                 ? (serve===VCT_ST | serve===VCT_LD) & Hit_a & Hit_b   // 跨行：两口都要命中
                 : (serve===VCT_ST | serve===VCT_LD) & Hit_a;           // 单行：A 口命中即可
```

**`vdata_operation` 的 load 拼装**——把双行输入 `input_block`（`2*BLOCK_W` 宽）按字节偏移右移、再按请求尺寸掩码，得到对齐到 0 位的结果（[rtl/vector/vdata_operation.sv:52-61](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vdata_operation.sv#L52-L61)）：

```systemverilog
assign offset_ammount  = input_address << 3;          // 字节偏移 → 位偏移
assign shifted_data    = input_block >> offset_ammount;
assign size_in_bits    = size << 3;                   // 字节数 → 位数
assign load_output_mask= ~('1 << size_in_bits);       // 低 size_in_bits 位为 1
assign output_vector   = load_output_mask & shifted_data[DATA_W-1:0];
```

**`vdata_operation` 的 store 合并**——store 要把 `input_data` 嵌入 cache 行的正确位置：先算出 `[offset, offset+size)` 这段比特的掩码 `store_mask`，把原 block 该段清零、再或上移位后的新数据（[rtl/vector/vdata_operation.sv:64-75](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vdata_operation.sv#L64-L75)）：

```systemverilog
assign lower_mask = ~('1 << offset_ammount);
assign upper_mask = lower_mask << size_in_bits;
assign store_mask = upper_mask & lower_mask;          // [offset, offset+size) 段
assign new_data   = (input_data << offset_ammount) & store_mask;
assign new_block  = input_block & (~store_mask);       // 原块该段清零
assign output_block = new_block | new_data;            // 合并新数据
```

**与标量拼装的复用**——`new_modded_block` 在向量服务态取 `vct_new_modded_block`，否则取标量 `scalar_new_modded_block`（[rtl/shared/data_cache.sv:903](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L903)），写回 SRAM 的通路是共享的。

**WIP 边界**：向量 store 的跨行还没实现，源码用断言告警（[rtl/shared/data_cache.sv:911](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L911)）：

```systemverilog
assert property (@(posedge clk) ... (serve === VCT_ST) |-> !nxt_multi_fetches_needed)
   else $warning("WARNING:Data_Cache: Multi-line stores not yet implemented in the Data Cache");
```

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：跟踪一条向量 load 从 `mem_req_i` 到 `vector_resp` 的完整通路，并理解跨行处理。

**操作步骤**：

1. 从 [rtl/shared/data_cache.sv:846-878](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L846-L878) 看 `vld_st_buffer` 的例化：`push = mem_req_valid_i & cache_vector_ready_o`，队头输出 `vct_head_addr/size/microop/ticket`。
2. 从 [rtl/shared/data_cache.sv:585-592](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L585-L592) 看 `VCT_LD` 如何进入 `serve`：条件是 `vct_valid && !vct_head_isstore`。
3. 从 [rtl/shared/data_cache.sv:881-901](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L881-L901) 看 `vdata_operation` 例化：输入 `block_picked_double={block_picked_b, block_picked}`（双行）、`input_data=vct_head_data`，输出 `vct_served_data`。
4. 从 [rtl/shared/data_cache.sv:904-908](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L904-L908) 看响应拼装：`vector_resp.{ticket,size,data}`。

**需要观察的现象 / 预期结果**（结论性）：

- 向量请求经 `vld_st_buffer` 排队 → `serve=VCT_LD` → 双行命中后 `vct_pop` → `vdata_operation` 切出 256 bit → `vector_resp` 回送 VMU。
- 跨行时必须 `Hit_a & Hit_b` 同时成立才 pop；任一行 miss 就向 L2 发对应请求（`vct_request_a`/`vct_request_b`），等两行都回填。
- 向量 store 若跨行，会触发上面那条 WIP 告警——这是当前已知边界。

> 待本地验证：可用一个故意让 store 跨行的向量程序触发 WIP 告警，确认该边界。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `vdata_operation` 的 `input_block` 要做成 `2*BLOCK_W` 宽？
**答案**：因为一条向量请求可能从某条 cache 行的后半段开始、延伸到下一条 cache 行的前半段。把相邻两行拼成 `2*BLOCK_W` 的双行缓冲后，无论起始偏移多少，一次右移 + 掩码就能切出连续数据，无需对「跨行」做特例分支。

**练习 2**：`vct_head_addr_b = vct_head_addr + vct_head_size - 1` 算的是「末字节地址」。如果 `vct_head_size` 的单位是字节，那么 `nxt_multi_fetches_needed` 何时为真？
**答案**：当首字节（`vct_head_addr`）与末字节（`vct_head_addr + size - 1`）落在**不同的 cache 行**（即它们的 index 位不同）时为真。例如 64 B 行、size=32 B、起始偏移在行内第 48 字节处，末字节会越过行边界进入下一行，于是需要双行取。

---

## 5. 综合实践

把本讲四块知识串起来：**跟踪一次完整的「向量 load miss → L2 取数 → 回填 → 返回 VMU」全过程**。

**任务**：以一条向量 `vld`（VMU load 引擎发出）为例，画出从 VMU 到主存再回来的时序与数据通路，并回答三个问题。

**步骤**：

1. **入缓存**：VMU 发出 `mem_req_valid_i` + `mem_req_i`（[rtl/shared/data_cache.sv:55-57](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L55-L57)）→ `vld_st_buffer.push` → 队头 `vct_head_addr`。
2. **查缓存**：`serve=VCT_LD` → A 口 `Hit_a`（跨行则加 B 口 `Hit_b`）。miss → 经 `request_l2_valid/request_l2_addr`（[rtl/shared/data_cache.sv:145-149](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L145-L149)）发给 `main_memory`。
3. **主存排队 + 延迟**：请求进 `main_memory` 的 `instruction_queue`，`delay_counter` 走约 `DELAY_CYCLES=10` 拍后 `delayed_valid` 拉高，从 `ram` 切出对应 D-cache 块经 `dcache_data_o` 回送（[rtl/shared/main_memory.sv:160-202](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/main_memory.sv#L160-L202)）。
4. **回填缓存**：`update_l2_valid/update_l2_data` 把新行写进 SRAM、置 Valid、必要时先淘汰 LRU 脏行（`write_l2_*` 写回，[rtl/shared/data_cache.sv:151-153](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L151-L153)）。
5. **命中服务**：下一拍 `Hit_a` 成立 → `vct_pop` → `vdata_operation` 切出 256 bit → `vector_resp_valid_o` + `vector_resp` 回 VMU（[rtl/shared/data_cache.sv:904-908](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L904-L908)）。
6. **回到计分板**：VMU 收到响应后写回 VRF 并 `unlock`（见 u3-l2、u4-l1）。

**需要回答的三个问题**：

- **Q1（共存）**：整个过程中，标量 load/store 端口能否同时活动？为什么？
  **A**：不能。断言（[rtl/shared/data_cache.sv:922-924](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L922-L924)）禁止标量与向量访存同拍在场；二者共享 SRAM 阵列、L2 端口与 `serve` 仲裁，必须串行化。
- **Q2（转发）**：向量 load miss 等待期间，会不会用到 `ld_st_buffer` 的 search 转发？
  **A**：不会。search 端口服务的是**标量** store-to-load 转发。向量侧的「等待与唤醒」由 `vld_st_buffer` 的 `vct_head_requested` 跟踪两行命中状态、配合 L2 回填完成，等价机制但独立实现。
- **Q3（延迟）**：把 `DELAY_CYCLES` 从 10 调到 50，这条 load miss 的总等待会增加多少？对什么 stall 指标影响最大？
  **A**：每次 L2 访问从约 10 拍增到约 50 拍，miss 代价近似线性增加；最直接影响 VMU load 引擎的 `total_ld_stalled_due_is`（u3-l2）以及 vis 侧因等待 load 结果而产生的 stall（受 `REALISTIC` 旋钮控制，见 u4-l7）。

> 待本地验证：上述周期数与 stall 变化需在本机用 `REALISTIC=1`、不同 `DELAY_CYCLES` 跑 saxpy 等示例后从 `results.log` 取数确认。

## 6. 本讲小结

- `data_cache` 是一个 **4 路、32 set、64 B 行**的非阻塞缓存，项格式 `{Valid}{Dirty}[Tag][Data]`，地址拆成 `[21 Tag][5 Index][6 Offset]`；标量与向量共享同一阵列与 L2 端口，由一个 8 态 `serve` 状态机按固定优先级仲裁。
- 非阻塞能力来自三个缓冲：load buffer / store buffer（共用 `ld_st_buffer`）+ wait buffer；`update` 端口在 L2 回填时把等待项标 `isfetched`，实现 **miss-under-miss**；`wait_buffer` 的 WALK 模式批量唤醒同行等待项。
- `ld_st_buffer` 的 **search 端口**用「地址全等 + `microop_ok` 尺寸兼容」判定命中，把未落盘的 store 数据直接转给后续 load，实现 **store-to-load forwarding**，转发优先级 wait buffer > store buffer > 缓存。
- `main_memory` 是行为级 L2/主存模型：`$readmemh` 加载内容、`fifo_dual_ported` 深度 8 保序、一位热码移位计数器提供约 `DELAY_CYCLES` 拍的 REALISTIC 延迟。
- 向量 `mem_req` 经 `vld_st_buffer` 排队、享第二读口 B 做跨行同时查（`Hit_a & Hit_b`），由 `vdata_operation` 用 `2*BLOCK_W` 双行输入 + 移位掩码完成宽 load/store 拼装；向量 store 跨行尚是 WIP。
- 当前向量仿真把标量端口常拉低，所以标量非阻塞链路（含 store-to-load 转发）实际不触发，但 RTL 完整保留，待标量核接入即生效；标量与向量不得同拍在场是由断言强制的设计前提。

## 7. 下一步学习建议

- **u4-l1（解耦执行）**：本讲的缓存 unlock/release 是从 VMU 视角看的；若想理解 load 数据如何唤醒 vis 计分板的 `locked` 位，回到 u4-l1 把 acquire-release 闭环。
- **u4-l7（性能调优）**：本讲提到的 `REALISTIC`、`DELAY_CYCLES`、lane 数等旋钮，其效果最终落在 `results.log` 的 stall 指标上，建议接着学 u4-l7 学会读数与定位瓶颈。
- **继续阅读源码**：想看标量侧完整数据拼装，读 `rtl/shared/data_operation.sv`（`vdata_operation` 的窄位孪生）；想看 SRAM 行为模型与 one-hot 选通，读 `rtl/shared/sram.sv`、`rtl/shared/and_or_mux.sv`、`rtl/shared/arbiter.sv`；想看向量版缓冲，读 `rtl/vector/vld_st_buffer.sv`。
- **扩展方向**：若要让标量核与向量核真正并发用缓存，需要松开 [rtl/shared/data_cache.sv:922-924](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/data_cache.sv#L922-L924) 的互斥断言、给 `serve` 仲裁增加「标量+向量同拍双发」能力，并补齐 `vdata_operation` 的跨行 store（消除 WIP 告警）——这是一个有价值的二次开发课题。
