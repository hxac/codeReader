# VMU 存储引擎（vmu_st_eng）

## 1. 本讲目标

本讲打开 VMU 三引擎中的**存储引擎（store engine，`vmu_st_eng.sv`）**黑盒。读完本讲，你应当能够：

- 说清一条 `vsw/vsh/vsb` 指令在存储引擎里从「读寄存器 → 打包数据 → 发访存请求 → 解锁源寄存器」的完整生命历程。
- 解释存储引擎为何**只有读端口、没有写回端口**，以及它为何配了**两个读端口**（数据 + 索引偏移）。
- 掌握按元素宽度（8/16/32 位）的**多元素打包**与 unit-strided「打包请求」机制。
- 透彻理解存储引擎的**解锁时机**：为什么它在「跨物理寄存器边界（`start_new_loop`）」时解锁，而 load 引擎在「写回完成（`writeback_complete`）」时解锁。
- 回答核心实践问题：**为什么存储引擎不需要 load 引擎那套双行 scratchpad？**

本讲严格承接 [u3-l1](u3-l1-vmu-memory-unit-and-arbitration.md)（VMU 三路分派与仲裁）与 [u3-l2](u3-l2-vmu-load-engine.md)（load 引擎与解耦执行）。u3-l1 已经说明 store 引擎「不写回 VRF」、unlock 端口被三引擎时分复用；u3-l2 已经讲清了 load 引擎的双行 scratchpad 与 `writeback_complete` 解锁。本讲的任务就是沿着同一条主线，把存储引擎与 load 引擎的**对称差异**讲透。

## 2. 前置知识

在进入源码前，先用三段话补齐本讲需要的基础概念。已熟悉的读者可跳过。

- **向量 store 指令在做什么。** 一条 `vsw v4, #2560` 的意思是：把向量寄存器 `v4` 里的 `vl` 个 32 位元素，按某种地址规律写到内存里，起始地址是立即数 `#2560`。存储引擎的工作就是「把寄存器里的数搬到内存」。它**消费**源寄存器（读出数据），但**不生产**任何向量寄存器的新值——这正是它与 load 引擎的根本对称差异：load 把内存的数写进寄存器（生产），store 把寄存器的数写进内存（消费）。

- **三种寻址模式与两种元素粒度。** 本项目把访存指令的 `microop` 字段切成三段（见 [u1-l4](u1-l4-shared-types-and-macros.md) 与 `vmacros.sv`）：高位 `microop[6]` 是 `LD_BIT`（1=load、0=store）；中段 `microop[5:4]` 是寻址模式 `MEM_OP`；低段 `microop[3:2]` 是元素宽度 `MEM_SZ`。寻址模式有三种：`OP_UNIT_STRIDED`（连续地址，单位步长）、`OP_STRIDED`（固定步长）、`OP_INDEXED`（变址，每个元素的偏移来自另一个向量寄存器）。元素宽度有 `SZ_8`（字节）、`SZ_16`（半字）、`SZ_32`（字，32 位）。

- **解耦执行里的 lock/unlock。** 回顾 [u2-l3](u2-l3-vrrm-register-remap.md)：vRRM 给访存指令盖了 lock 位，store 的 lock 编码是 `2'b01`，其中 `lock[0]=1` 表示「源寄存器正在被访存消费」。源寄存器一旦被 store 锁住，后续要写它的指令就得在计分板里等（参见 [u2-l5](u2-l5-vis-scoreboard-and-hazards.md) 的 `locked` 位）。store 引擎读完成之后，必须通过 unlock 接口告诉 vIS「我读完了，源可以释放了」——这就是 acquire-release 语义里的 **release**（详见 [u4-l1](u4-l1-decoupled-execution-acquire-release.md)）。

## 3. 本讲源码地图

本讲只聚焦一个文件，但会拿另外三个文件做对照：

| 文件 | 作用 | 本讲如何使用 |
| --- | --- | --- |
| `rtl/vector/vmu_st_eng.sv` | **存储引擎本体**，本讲主角 | 逐段精读，全部最小模块都落在这里 |
| `rtl/vector/vmu_ld_eng.sv` | load 引擎，u3-l2 已讲 | 拿来做对称对照（scratchpad、写回、解锁） |
| `rtl/vector/vmu.sv` | VMU 顶层，u3-l1 已讲 | 确认 store 引擎在顶层怎么被例化、unlock 怎么被仲裁复用 |
| `rtl/vector/vmacros.sv` + `vstructs.sv` | 宏定义与结构体，u1-l4 已讲 | 提供 `SZ_*`/`OP_*`/`LD_BIT` 宏与 `memory_remapped_v_instr` 结构体 |

## 4. 核心概念与源码讲解

按规格，本讲拆成三个最小模块：**①数据收集与读端口 ②元素打包 ③解锁时机**。三者顺着 store 指令的数据流向自然展开。

### 4.1 数据收集与读端口

#### 4.1.1 概念说明

存储引擎要做的第一件事，是**把要写进内存的数据从向量寄存器堆（VRF）里读出来**。这里有两个关键认知：

1. **store 是 VRF 的纯读者**。它只读不写——没有任何 `wrtbck_*` 输出端口。这一点和 load 引擎截然相反：load 引擎有一整套写回端口（`wrtbck_req_o/wrtbck_grant_i/wrtbck_en_o/wrtbck_reg_o/wrtbck_data_o/wrtbck_ticket_o`）。这个不对称会贯穿本讲始终，并最终解释「为什么 store 不需要双行 scratchpad」。

2. **store 需要两个读端口**。第一个端口读「要存的数据本身」（`src1`，比如 `vsw v4, #2560` 里的 `v4`）；第二个端口只在 `OP_INDEXED` 模式下用，读「每个元素的变址偏移」（`src2`）。所以引擎对外暴露 `rd_addr_1_o/rd_data_1_i` 和 `rd_addr_2_o/rd_data_2_i` 两组读端口。

每个读端口都附带两条来自 vIS 计分板的反馈：`rd_pending_i`（「这个源寄存器还有元素没算完吗？」）和 `rd_ticket_i`（「当前生产者的 ticket 是几？」）。store 必须等数据真的就绪了才能发请求，否则会把未定义的值写进内存。

#### 4.1.2 核心流程

存储引擎读端口的工作流程可以概括为：

```
1. 接收 push_store（来自 vmu.sv 的分派），锁存 src1_r / src2_r / ticket_r / vl / microop ...
2. 把 rd_addr_1_o 指向 src1_r（数据），把 rd_addr_2_o 指向 src2_r（索引）
3. 检查 data_ready   = ~rd_pending_1_i              # 数据寄存器就绪？
4. 检查 addr_ready   = indexed ? (~rd_pending_2_i & ticket 匹配) : 1   # 索引就绪？
5. request_ready     = data_ready & addr_ready & pending_elem[当前指针]
6. data_ready & addr_ready 同时成立后才允许 assert req_en_o
```

注意 `addr_ready` 的分支：只有在 `OP_INDEXED` 时才需要查 `src2` 的 pending 与 ticket；unit-strided 和 strided 的地址由 base+stride 推算，不读 `src2`，所以 `addr_ready=1`（恒成立）。

#### 4.1.3 源码精读

先看端口列表——这是「store 只有读、没有写回」最直接的证据：

[rtl/vector/vmu_st_eng.sv:19-52](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_st_eng.sv#L19-L52) 中文说明：存储引擎的端口只有输入指令、**两组 RF 读端口**（`rd_addr_1_o` 数据、`rd_addr_2_o` 索引）、unlock 端口、访存请求端口与同步端口。对比 [rtl/vector/vmu_ld_eng.sv:18-66](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L18-L66)，load 引擎额外多出一整段 `RF write Interface` 与 `RF Writeback Probing Interface`——store 把这一整段都省了。

读地址直接由锁存的源寄存器号驱动：

[rtl/vector/vmu_st_eng.sv:166-167](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_st_eng.sv#L166-L167) 中文说明：`rd_addr_1_o = src1_r`（读要存的数据寄存器），`rd_addr_2_o = src2_r`（读变址索引寄存器）。这两个地址会随着硬件循环展开（`start_new_loop`）逐个物理寄存器递增（见 4.3 节）。

数据就绪与地址就绪的判定：

[rtl/vector/vmu_st_eng.sv:156-158](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_st_eng.sv#L156-L158) 中文说明：`request_ready` 必须同时满足数据就绪、地址就绪、当前指针元素仍待存。`data_ready` 就是源寄存器不 pending；`addr_ready` 仅 indexed 模式查 `src2` 的 pending 与 ticket（当前 ticket 或上一轮 `last_ticket_src2_r` 都算命中），其余模式恒为 1。

在顶层 `vmu.sv` 里，这两个读端口连到 VRF 的第 1、2 号读口（load 引擎占用第 0 号）：

[rtl/vector/vmu.sv:271-280](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L271-L280) 中文说明：store 引擎例化时，`rd_addr_1_o/rd_data_1_i/rd_pending_1_i/rd_ticket_1_i` 连数据读口，`rd_addr_2_o/rd_data_2_i/rd_pending_2_i/rd_ticket_2_i` 连索引读口，**注意例化里根本没有连任何 `wrtbck_*` 端口**——顶层 [rtl/vector/vmu.sv:174-177](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L174-L177) 的写回 mux 也只在 load（`wb_grant[0]`）与 toeplitz（`wb_grant[1]`）之间二选一，store 完全不参与写回。

#### 4.1.4 代码实践

**实践目标**：亲手确认「store 是纯读者、且需要两个读端口」这件事。

**操作步骤**：

1. 打开 `rtl/vector/vmu_st_eng.sv` 的端口声明（19–52 行），数一数有几个 `output ... wrtbck_*`（答案应为 0）。
2. 打开 `rtl/vector/vmu_ld_eng.sv` 的端口声明（18–66 行），数一数 load 有几个 `wrtbck_*` 端口。
3. 在 `vector_simulator/examples/saxpy/instrs.csv` 里找到 `vsw, v4, #2560` 这一行。在 `sim_generator.py` 第 97 行确认 `vsw` 的 microop = `0000000`。

**需要观察的现象 / 预期结果**：

- `vsw` 的 7 位 microop `0000000` 拆解：`bit[6]=0`（`LD_BIT=0`，store）、`bit[5:4]=00`（`OP_UNIT_STRIDED`，连续地址）、`bit[3:2]=00`（`SZ_32`，32 位字）。所以这条指令走 unit-strided、字宽、**不读 `src2`**（`addr_ready` 恒为 1），只通过 `rd_addr_1_o` 读 `v4`。
- store 引擎端口数明显少于 load 引擎——少掉的就是写回相关的全部信号。

> 待本地验证：如果你有 QuestaSim 环境，可以在 `vsw` 执行期间把 `rd_addr_1_o` 加到波形，确认它指向 `v4` 的物理寄存器号，并且整个执行期间 `wrtbck_en_o`（在 `vmu.sv` 顶层）始终不会因为 store 而拉高。

#### 4.1.5 小练习与答案

**练习 1**：`vsw` 和 `vssw`（strided store）在「读几个寄存器」上有区别吗？
**答案**：没有。两者都只读数据寄存器 `src1`（`rd_addr_1_o`）。`vssw` 的步长来自立即数/`src2` 但地址用 `base + stride` 推算（见 4.2 节），并不需要逐元素读 `src2`，所以 `addr_ready` 仍恒为 1。只有 `vsxw`（indexed store）才会真正用到 `rd_addr_2_o` 读变址向量。

**练习 2**：为什么 indexed 模式下 `addr_ready` 还要额外比较 `rd_ticket_2_i === last_ticket_src2_r`，而不只比 `ticket_r`？
**答案**：变址向量 `src2` 可能由更早一轮展开（上一张 ticket）的生产者写出。比较当前 `ticket_r` 与「上一轮 ticket」`last_ticket_src2_r` 两种情况，是为了在跨展开轮次时也能正确识别「这个版本的 src2 已经就绪」，避免误等。

### 4.2 元素打包

#### 4.2.1 概念说明

数据读出来之后，要按「请求总线宽度」组织成一次访存请求。这里有一个核心权衡：

- **unit-strided 模式可以「打包」**：因为元素地址连续，缓存一次能收 `REQ_DATA_WIDTH` 位（默认 256 位 = 32 字节）。对 32 位元素，一次最多塞 8 个；对 8 位元素，一次最多塞 32 个。但引擎每拍最多从 `VECTOR_LANES` 个 lane 拿数据，所以实际打包上限是
  \[
  \text{MAX\_SERVED\_COUNT} = \min(\text{VECTOR\_LANES},\ \text{REQ\_DATA\_WIDTH}/\text{DATA\_WIDTH})
  \]
  默认参数下 = min(8, 256/32) = 8。

- **strided 与 indexed 不能打包**：因为元素地址不连续，一个请求只能写一个元素，`el_served_count = 1`。

「按 size 选位宽」指的是：同样是把 `VECTOR_LANES` 个 32 位 lane 元素塞进请求包，`SZ_8` 只取每元素的最低 8 位、`SZ_16` 取低 16 位、`SZ_32` 取全部 32 位，再把它们**紧凑拼接**到 `req_data_o` 里。

#### 4.2.2 核心流程

```
multi_valid = (memory_op == OP_UNIT_STRIDED)              # 是否可打包

if (!multi_valid):                       # strided / indexed：单元素
    data_selected[0+:32] = rd_data_1_i[当前元素]          # 只放一个 32 位
    el_served_count      = 1
else:                                    # unit-strided：打包
    el_served_count = min(本寄存器剩余待存元素, MAX_SERVED_COUNT)
    for i in 0..el_served_count-1:
        按 size 取第 i 个 lane 元素的低 8/16/32 位
        拼到 data_selected 的第 i 个宽度槽位

req_size_o = el_served_count × (SZ_8?1 : SZ_16?2 : 4)      # 这次请求写多少字节
```

请求字节数 `req_size_o` 与元素个数 `el_served_count` 的换算（以字节为单位）：

\[
\text{req\_size\_o} = \text{el\_served\_count} \times \begin{cases} 1 & \text{SZ\_8} \\ 2 & \text{SZ\_16} \\ 4 & \text{SZ\_32} \end{cases}
\]

#### 4.2.3 源码精读

先看打包上限与每拍服务元素数的计算：

[rtl/vector/vmu_st_eng.sv:54-58](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_st_eng.sv#L54-L58) 中文说明：`MAX_MEM_SERVED_LIMIT = REQ_DATA_WIDTH/DATA_WIDTH`（请求宽度能容纳几个元素），`MAX_SERVED_COUNT = min(VECTOR_LANES, MAX_MEM_SERVED_LIMIT)`——打包上限取「lane 数」与「请求宽度能塞的元素数」的较小值。注意这里 store 用的是 `VECTOR_LANES`，而 load 引擎 [rtl/vector/vmu_ld_eng.sv:72-73](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L72-L73) 用的是 `VECTOR_REGISTERS`——默认参数下两者都化简为 8，但概念来源不同。

每拍服务元素数 `el_served_count` 的取值：

[rtl/vector/vmu_st_eng.sv:258-266](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_st_eng.sv#L258-L266) 中文说明：非 unit-strided 模式恒为 1（一次写一个元素）；unit-strided 模式取「本物理寄存器剩余待存元素」与 `MAX_SERVED_COUNT` 的较小值——尾部不足一组时只打包剩余几个。

核心的**按 size 打包**逻辑：

[rtl/vector/vmu_st_eng.sv:231-245](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_st_eng.sv#L231-L245) 中文说明：非打包模式只把单个 32 位元素放到低位；打包模式遍历 `MAX_SERVED_COUNT` 个槽位，按 `SZ_8/SZ_16/SZ_32` 分别从每个 lane 元素的 32 位槽里取低 8/16/32 位，紧凑拼到 `data_selected`。`data_selected_v`（[第 228 行](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_st_eng.sv#L228)）先把整个 lane 向量右移，让当前指针元素落到 bit0，再从这里开始按槽位取样。

请求字节数与 microop 的发出：

[rtl/vector/vmu_st_eng.sv:142-153](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_st_eng.sv#L142-L153) 中文说明：`req_size_o` 按 size 把 `el_served_count` 换算成字节数（×1/×2/×4）；`req_data_o = data_selected`、`req_addr_o = current_addr`、`req_microop_o` 当前固定为 `5'b11111`（代码注释 `REVISIT will change based on instruction` 标明这是占位值，后续会按指令细分）。打包好的数据经顶层 [rtl/vector/vmu.sv:172](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L172) 直接送到 `mem_req_o.data`——注意顶层这里**只有 store 才填 `data` 字段**，load/toeplitz 的请求不带数据。

地址生成（三种模式）：

[rtl/vector/vmu_st_eng.sv:177-195](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_st_eng.sv#L177-L195) 中文说明：`OP_UNIT_STRIDED` 地址按已服务字节数连续累加；`OP_STRIDED` 每次加一个定长 `stride`；`OP_INDEXED` 用 `base + rd_data_2_i[当前元素]`（变址偏移来自 `src2` 的第二读口）。这段逻辑与 load 引擎 [rtl/vector/vmu_ld_eng.sv:225-243](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L225-L243) 几乎逐字相同，因为地址计算与方向（load/store）无关。

#### 4.2.4 代码实践

**实践目标**：在脑中（或纸上）画出 `SZ_8` 打包 8 个字节时 `req_data_o` 的位布局。

**操作步骤**：

1. 假设 `v4` 的 8 个 lane 元素分别是 `0x0A0B0C0D, 0x..., ...`（每个 32 位）。
2. 走一遍 [第 236–243 行](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_st_eng.sv#L236-L243) 的 `SZ_8` 分支：对每个 `i`，`data_selected[i*8 +: 8] = data_selected_v[i*32 +: 8]`。
3. 计算 `req_size_o`（`SZ_8` 分支：`el_served_count << 0` = 8 字节）。

**需要观察的现象 / 预期结果**：

- `req_data_o` 的 bit[7:0] = 第 0 个 lane 元素的低 8 位（`0x0D`），bit[15:8] = 第 1 个 lane 元素的低 8 位，…… bit[63:56] = 第 7 个 lane 元素的低 8 位。高 192 位为 0。
- `req_size_o = 8`（字节）。也就是说，一条 `vsb` 在 unit-strided 下，一拍就把 8 个字节紧凑成一次 8 字节的内存写。

> 待本地验证：可在仿真里对 `vsw/vsh/vsb` 三种宽度各跑一次，比较 `req_size_o`（应分别为 32/16/8 字节量级）与 `req_data_o` 的有效位宽。

#### 4.2.5 小练习与答案

**练习 1**：同样是 unit-strided、`VECTOR_LANES=8`、`SZ_32`，一条 `vl=8` 的 `vsw` 需要发几次访存请求？
**答案**：1 次。`el_served_count = min(8, MAX_SERVED_COUNT=8) = 8`，8 个 32 位元素正好打包成 256 位 = 32 字节一次写完，`req_size_o = 8×4 = 32`。

**练习 2**：换成 `OP_STRIDED`、`vl=8`，需要几次？
**答案**：8 次。strided 模式 `el_served_count` 恒为 1，每个元素地址不连续，必须逐个发请求。

### 4.3 解锁时机

#### 4.3.1 概念说明

这是本讲最关键、也是与 load 引擎差异最大的一块。

回顾 lock/unlock 的语义：vRRM 给 store 的源寄存器盖了 `lock[0]=1`（「源被访存消费」），后续想写这个源寄存器的指令会被 vIS 卡住。store 引擎读完成之后，要通过 `unlock_*` 接口释放它。问题来了：**store 在什么时刻才算「读完」、可以解锁？**

- **store 的答案：在「跨物理寄存器边界」时解锁。** store 把源寄存器的数据读出来、打包发往缓存，发完之后这个源寄存器对 store 来说就没用了，可以释放。所以引擎在**开始处理下一个物理寄存器**（`start_new_loop`）或**整条指令结束**（`current_finished`）时拉高 `unlock_en_o`。

- **load 的答案（对照）：在「写回完成」时解锁。** load 是往**目的**寄存器写数据，目的寄存器在被锁期间不能被读（消费者在等）。只有当数据真的写回了 VRF（`writeback_complete`），目的寄存器才算有效，才能解锁。

一句话总结差异：**store 解锁的是它消费完的「源」，解锁时机 = 源被读完的边界；load 解锁的是它生产出的「目的」，解锁时机 = 目的被写回完成。** 前者跟随读流推进，后者跟随写回流完成。

#### 4.3.2 核心流程

store 的硬件循环展开（expansion loop）会按 `VECTOR_LANES` 把一条长 `vl` 的指令拆成若干个物理寄存器（参见 [u3-l2](u3-l2-vmu-load-engine.md) 的 expansion loop，两者结构相同）。设一条指令被展开成 N 个物理源寄存器，则解锁序列是：

```
N=1:  current_finished  →  解锁寄存器 0
N>1:  start_new_loop（第 0 个寄存器读完） → 解锁寄存器 0
      start_new_loop（第 1 个寄存器读完） → 解锁寄存器 1
      ...
      current_finished（最后一个读完）   → 解锁寄存器 N-1
```

关键细节：`unlock_reg_a_o = src1_r`、`unlock_reg_b_o = src2_r`，而 `src1_r/src2_r` 在 `start_new_loop` **同一拍**自增（指向下一个物理寄存器）。因此解锁用的是**自增前**的值——正好是被刚刚读完的那个寄存器。每个物理源寄存器恰好被解锁一次。

#### 4.3.3 源码精读

解锁信号的定义（本讲最核心的一行）：

[rtl/vector/vmu_st_eng.sv:159-163](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_st_eng.sv#L159-L163) 中文说明：`unlock_en_o = start_new_loop | current_finished`——store 在「进入下一个物理寄存器」或「整条指令完成」时解锁；解锁目标是 `src1_r`（数据寄存器）和 `src2_r`（索引寄存器），ticket 是本指令的 `ticket_r`。**对照** load 引擎 [rtl/vector/vmu_ld_eng.sv:191-194](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L191-L194)：`unlock_en_o = writeback_complete`，且解锁目标 `unlock_reg_a_o` 选的是 `row_0_ready ? row_0_rdst : row_1_rdst`（**目的**寄存器，并且要等写回 grant）。

`start_new_loop` 的触发条件：

[rtl/vector/vmu_st_eng.sv:137-139](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_st_eng.sv#L137-L139) 中文说明：在未完成展开（`~expansion_finished`）且本拍成功完成一次访存事务（`new_transaction_en`）的前提下，若当前物理寄存器的剩余元素已服务完（指针不在 0 且下一元素已不 pending），或仍有一整组（`loop_remaining_elements >= MAX_SERVED_COUNT`）要继续，就启动新一轮循环——即「当前物理寄存器读完了，进下一个」。

`src1_r/src2_r` 的自增时机，配合上面的解锁：

[rtl/vector/vmu_st_eng.sv:320-334](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_st_eng.sv#L320-L334) 中文说明：`start_new_loop` 当拍把 `current_exp_loop_r` 加 1、`src1_r <= src1_r + 1`、`src2_r <= src2_r + 1`。由于寄存器在下一拍才更新，而 `unlock_reg_a_o` 用的是当拍（自增前）的 `src1_r`，所以解锁的恰好是刚读完的那个物理寄存器。整条指令结束时由 `current_finished`（[第 125 行](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_st_eng.sv#L125)）解锁最后一个寄存器。

最后看 unlock 在顶层怎么被三引擎时分复用：

[rtl/vector/vmu.sv:142-154](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L142-L154) 中文说明：`unlock_en_o` 是三引擎 unlock 的或；mux 优先级为 toeplitz（`wb_grant[1]`）> load（`load_unlock_en`）> store。store 的解锁优先级最低，但这不影响正确性——store 一旦发完请求，源数据早已被缓存端口接收，晚一拍解锁只是让后续指令稍晚一点能写这个源寄存器。

#### 4.3.4 代码实践

**实践目标**：预测一条长 store 的解锁序列，理解「按寄存器边界解锁」。

**操作步骤**：

1. 设想一条 `vsw`，`vl=20`、`VECTOR_LANES=8`。
2. 计算展开成几个物理源寄存器：\(\lceil 20/8 \rceil = 3\)（元素 0–7、8–15、16–19）。
3. 用 [第 129–131 行](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_st_eng.sv#L129-L131) 的 `expansion_finished` 判定：`vl_reached` 在 `((current_exp_loop_r+1) << 3) >= 20` 时成立，即 `current_exp_loop_r = 2`（`3<<3=24>=20`）。
4. 列出解锁时刻：`exp_loop=0` 读完 → `start_new_loop` 解锁寄存器 0；`exp_loop=1` 读完 → `start_new_loop` 解锁寄存器 1；`exp_loop=2`（最后一个，触发 `current_finished`）解锁寄存器 2。

**需要观察的现象 / 预期结果**：3 个物理源寄存器各被解锁恰好一次。其中前 2 个由 `start_new_loop` 解锁，最后一个由 `current_finished` 解锁。整个过程中 store 引擎**从不**等任何 VRF 写回 grant——因为它根本不写回。

> 待本地验证：在波形里跟踪 `store_unlock_en` 与 `src1_r`，确认每次 unlock 拍 `src1_r` 的值是「即将被自增前」的物理寄存器号，并且 unlock 次数 = 展开的物理寄存器数。

#### 4.3.5 小练习与答案

**练习 1**：store 引擎的 `unlock_en_o` 与 load 引擎的 `unlock_en_o` 触发条件分别是什么？为什么不同？
**答案**：store = `start_new_loop | current_finished`（源读完即解锁）；load = `writeback_complete`（目的写回完成才解锁）。因为 store 消费源、读完即释放；load 生产目的、必须等数据真正落到 VRF 才能让消费者读到有效值。

**练习 2**：如果 store 在「最后一个物理寄存器」也用 `start_new_loop` 解锁会怎样？
**答案**：会漏解锁。最后一个寄存器读完后，`expansion_finished` 已为真，`start_new_loop` 的 `~expansion_finished` 条件不成立，不会再触发。所以必须由 `current_finished` 兜底解锁最后一个寄存器，否则该源寄存器会永远被锁住，后续依赖它的指令死锁。

## 5. 综合实践

本讲的核心实践任务：**比较 store 引擎与 load 引擎在 scratchpad 与写回上的差异，解释为何 store 不需要双行 scratchpad。** 请完成下面三步。

### 第一步：建立对照表

阅读 `rtl/vector/vmu_st_eng.sv` 与 `rtl/vector/vmu_ld_eng.sv`，填写下表（参考答案见后）：

| 维度 | load 引擎（`vmu_ld_eng`） | store 引擎（`vmu_st_eng`） |
| --- | --- | --- |
| 是否有写回端口（`wrtbck_*`） | ? | ? |
| 是否有 scratchpad 缓冲寄存器 | ? | ? |
| 数据流方向 | ? | ? |
| `unlock_en_o` 触发条件 | ? | ? |
| 解锁目标寄存器 | ? | ? |
| 是否需要等 VRF 写回 grant | ? | ? |
| 读端口数量 | ? | ? |

### 第二步：画出两条数据流

分别画 load 与 store 的「外部交互时序图」：

- **load**：缓存返回数据（`resp_valid_i`）→ 写进 `scratchpad` → 等写回 grant（`wrtbck_grant_i`）→ 写回 VRF（`wrtbck_*`）→ `writeback_complete` → unlock。
- **store**：读 VRF（`rd_data_1_i`）→ 组合打包（`data_selected`）→ 同拍发请求（`req_en_o & grant_i`）→ `start_new_loop`/`current_finished` → unlock。

### 第三步：回答核心问题

**为什么 store 不需要双行 scratchpad？** 提示：从「是否存在两个需要被解耦的时间域」入手。load 的「缓存响应到达」与「VRF 写回授权」是两个独立的时间点，二者之间需要缓冲数据，于是用双行 scratchpad 让「取下一批」与「写回当前批」并行（详见 [u3-l2](u3-l2-vmu-load-engine.md)）。store 呢？

### 参考答案

对照表：

| 维度 | load 引擎 | store 引擎 |
| --- | --- | --- |
| 写回端口 | **有**（`wrtbck_req_o/grant_i/en_o/reg_o/data_o/ticket_o` + probing） | **无** |
| scratchpad | **有**，且是**双行** `logic [1:0][VECTOR_LANES-1:0][DATA_WIDTH-1:0]` | **无**（[第 63–81 行](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_st_eng.sv#L63-L81) 的寄存器列表里没有任何数据缓冲） |
| 数据流方向 | 内存 → VRF（生产目的） | VRF → 内存（消费源） |
| `unlock_en_o` | `writeback_complete`（[ld 第 191 行](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_ld_eng.sv#L191)） | `start_new_loop \| current_finished`（[st 第 160 行](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_st_eng.sv#L160)） |
| 解锁目标 | **目的**寄存器 `rdst`（`row_0/1_rdst`） | **源**寄存器 `src1_r`（数据）/ `src2_r`（索引） |
| 是否等 VRF 写回 grant | 是（`wrtbck_grant_i`） | 否（写回与 store 无关） |
| 读端口数量 | 1（`rd_addr_o`，仅 indexed 读索引） | **2**（数据 `rd_addr_1_o` + 索引 `rd_addr_2_o`） |

**核心解释**：store 不需要双行 scratchpad，因为它的数据通路里**没有第二个需要被解耦的消费者**。load 必须把缓存「推过来」的数据先存起来（缓存不会重发），再排队等 VRF 写回授权，于是出现「响应到达」与「写回授权」两个时间域，需要双行缓冲让取数流与写回流并行。而 store 的数据从 VRF 读出后，**在同一拍**经组合逻辑打包成 `req_data_o`，一旦缓存给 grant（`new_transaction_en`），数据就被缓存端口取走——读出的数据立刻被消费，不需要暂存到下一拍，更不需要为「等待另一个慢消费者」而缓存。store 唯一的状态跟踪是 `pending_elem` 这个「还有哪些元素没存」的待办位图（[第 302–318 行](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu_st_eng.sv#L302-L318)），它记录的是「进度」而非「缓存的数据」。简言之：**load 要缓冲是因为它有一个会延迟的下游（VRF 写回）；store 没有下游，所以没有缓冲。**

## 6. 本讲小结

- store 引擎是 VRF 的**纯读者**：只有 `rd_addr_1_o`（数据）和 `rd_addr_2_o`（indexed 的变址偏移）两个读端口，**没有任何写回端口**。
- 数据就绪判定 `data_ready = ~rd_pending_1_i`；indexed 模式额外要求 `addr_ready` 校验 `src2` 的 pending 与 ticket。
- **按 size 打包**：unit-strided 模式每拍最多把 `MAX_SERVED_COUNT = min(LANES, REQ_DATA_WIDTH/DATA_WIDTH)` 个元素按 8/16/32 位紧凑拼进 `req_data_o`；strided/indexed 一次只写一个元素。
- 请求字节数 `req_size_o = el_served_count × {1,2,4}`，打包好的数据经顶层 `mem_req_o.data` 直送缓存（顶层里只有 store 填这个字段）。
- **解锁时机**：`unlock_en_o = start_new_loop | current_finished`，在跨物理寄存器边界或整条指令结束时解锁**源**寄存器；与 load 的 `unlock_en_o = writeback_complete`（解锁**目的**）形成鲜明对比。
- store **不需要双行 scratchpad**，因为它的数据同拍被缓存消费，没有需要解耦的延迟下游；进度只需一个 `pending_elem` 待办位图即可。

## 7. 下一步学习建议

- 接下来读 [u3-l4](u3-l4-vmu-tile-prefetch-engine.md)（VMU 分块预取/Toeplitz 引擎），它是 VMU 三引擎里最后、也最特殊的一个：带显式状态机和投机预取，且会「偷」load/store 的空闲周期（本讲的 `can_be_inteleaved_o` 信号就是为这种偷闲机制服务的）。
- 若想把解锁语义上升到体系结构层面，建议跳到 [u4-l1](u4-l1-decoupled-execution-acquire-release.md)（解耦执行与 acquire-release 语义），把本讲的 store unlock 与 load 的 lock、vIS 的计分板串成完整的 acquire-release 闭环。
- 想动手验证本讲结论的读者，可回到 [u1-l5](u1-l5-running-the-simulator.md) 跑 `saxpy` 示例（其中就含一条 `vsw`），在波形里对照观察 `req_data_o`、`req_size_o` 与 `store_unlock_en`。
