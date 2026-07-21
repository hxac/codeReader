# VMU 存储单元与三路仲裁

## 1. 本讲目标

本讲进入向量数据通路的「访存岔路」——向量存储单元 VMU（Vector Memory Unit）。学完本讲你应该能够：

- 说出 VMU 在顶层 `vector_top` 中的位置：它是从 vRRM 分叉、结果绕回 vIS 的访存旁路。
- 读懂 `vmu.sv` 如何用一组组合译码（`is_load` / `is_store` / `is_toepl` / `is_reconf`）把一条 `memory_remapped_v_instr` 分派到 load / store / tile-prefetch 三个子引擎。
- 解释 VMU 如何用一个深度为 3 的 `fifo_duth` 跟踪在途引擎，并据此对**唯一的缓存请求端口**和**唯一的写回端口**做仲裁。
- 说明 prefetch 引擎为何能「偷」空闲周期，以及 `is_older` 寄存器如何决定 load/toepl 写回优先级。
- 理解 `reconfigure` 指令为何要求三个引擎同时就绪、并同时下发。

本讲只讲 `vmu.sv` 这一层**仲裁与分派逻辑**，三个子引擎的内部实现（地址生成、双行 scratchpad、投机预取状态机）分别留给 u3-l2 / u3-l3 / u3-l4。

## 2. 前置知识

本讲默认你已经掌握 u2-l1（`vector_top` 顶层数据通路）的内容。这里回顾几个关键点，并补充本讲需要的新概念。

**回顾：VMU 在数据通路中的位置。** 向量数据通路主路是 vRRM（寄存器重映射）→ vIS（计分板发射）→ vEX（执行）。访存指令在 vRRM 处按 `fu === MEM_FU` 判定后，被改写成 `memory_remapped_v_instr` 走一条岔路进入 VMU；VMU 的 load 结果再**绕回 vIS**，像 vEX 的写回一样更新 VRF 并清除计分板。store 指令则只写缓存、不写回 VRF。

**ready/valid 握手与反压。** 各级之间用 `valid`（我有数据）+ `ready`（我能收）握手；当 `ready` 拉低时上游必须停住，这叫反压（back-pressure）。VMU 对上游 vRRM 暴露 `ready_o`，对下游缓存暴露 `mem_req_valid_o`。

**acquire-release 与 ticket（回顾 u2-l3/u2-l5）。** 访存指令在 vIS 处会按 `lock` 位**锁定**目的/源寄存器（acquire），等 VMU 完成后通过 `unlock_*` 接口按 `ticket` **解锁**（release）。`ticket` 是跨「计算路」与「访存路」的序号，用来消除两条独立数据流之间的生产者-消费者歧义。VMU 的 `unlock` 接口就是这条 release 通路的出口。

**仲裁（arbitration）。** 当多个请求方争夺同一个单端口资源时，需要一个仲裁器决定「这一拍把资源给谁」。VMU 里有两处单端口资源需要仲裁：① 对外的缓存请求端口（`mem_req_o`），② 对 vIS 的写回端口（`wrtbck_*`）。

**FIFO 作为顺序跟踪器。** `fifo_duth` 是一个通用的环形 FIFO（见 u2-l2）。VMU 把它当作「在途指令顺序表」用：每开始一条指令就 `push` 一个标识它属于哪个引擎的 2 位编码，FIFO 的**队头** `pop_data` 始终是**最老的那条在途指令**。用 FIFO 而不是简单寄存器，是因为三个引擎可以同时各跑一条指令，最多 3 条在途，正好对应 `DEPTH=3`。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [rtl/vector/vmu.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv) | **本讲主角**。VMU 顶层：例化三个子引擎、做引擎分派、用 FIFO 跟踪在途、对缓存请求端口与写回端口做仲裁、复用 unlock 信号。 |
| [rtl/vector/vmacros.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv) | 提供 `LD_BIT`（load/store 区分位）、存储操作码段定义（`MEM_OP_RANGE_*`、`MEM_SZ_RANGE_*`）。 |
| [rtl/vector/vstructs.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vstructs.sv) | 定义 VMU 的输入结构体 `memory_remapped_v_instr`、缓存请求/响应结构体 `vector_mem_req` / `vector_mem_resp`。 |
| [rtl/shared/fifo_duth.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/fifo_duth.sv) | 通用环形 FIFO，VMU 用它（`DW=2, DEPTH=3`）跟踪在途引擎顺序。 |

> 本讲引用的所有代码都来自上述文件，行号基于当前 HEAD `8ded0f4`。三个子引擎（`vmu_ld_eng` / `vmu_st_eng` / `vmu_tp_eng`）只在 `vmu.sv` 里作为黑盒例化，其内部细节不在本讲展开。

## 4. 核心概念与源码讲解

### 4.1 引擎分派：把一条指令归类到 load / store / toeplitz

#### 4.1.1 概念说明

VMU 收到的并不是「vld / vst」这样的助记符，而是已经由 vRRM 改写好的 `memory_remapped_v_instr` 结构体，其中关键的是一个 7 位的 `microop` 字段。VMU 的第一件事就是**纯组合地**把这个 `microop` 翻译成「该交给哪个引擎」的 4 个布尔信号：

- `is_load`：交给 load 引擎 `vmu_ld_eng`（从缓存取数、写回 VRF）。
- `is_store`：交给 store 引擎 `vmu_st_eng`（从 VRF 读数、写缓存，不写回）。
- `is_toepl`：交给 tile-prefetch 引擎 `vmu_tp_eng`（为卷积做 2D 分块预取，也写回 VRF）。
- `is_reconf`：重配指令，不是一个常规访存，而是要求三个引擎**同时**清空并复位。

这四个信号互斥（一条指令只走一个引擎），是后续 `push` 与 `ready_o` 判定的基础。

#### 4.1.2 核心流程

分派的关键是 7 位 `microop` 的位段约定（来自 `vmacros.sv`）：

| 位段 | 宏 | 含义 |
|------|----|----|
| bit 6 | `LD_BIT` | 1 = load 类，0 = store 类 |
| bits[5:4] | `MEM_OP_RANGE` | 寻址模式（unit-strided / strided / indexed） |
| bits[3:2] | `MEM_SZ_RANGE` | 元素宽度（8 / 16 / 32 位） |

把 `reconfigure` 位也纳入后，四路的判定逻辑可写成：

\[
\text{is\_toepl} = \neg\text{reconfigure} \land (\text{microop} = 7'b1110011 \lor \text{microop} = 7'b1010011)
\]

\[
\text{is\_load} = \neg\text{reconfigure} \land \text{microop}[6] \land \neg\text{is\_toepl}
\]

\[
\text{is\_store} = \neg\text{reconfigure} \land \neg\text{microop}[6] \land \neg\text{is\_toepl}
\]

注意三点：

1. **`is_toepl` 先判**：两条自定义指令编码（`vtplcfg` / `vtpl`）虽然 bit 6 为 1（看起来像 load），但被优先识别为 toeplitz，再用 `& ~is_toepl` 把 load/store 排除掉，避免误归类。
2. **bit 6 决定 load vs store**：在非 toeplitz 的常规访存里，`LD_BIT` 一刀切开 load 与 store。
3. **`reconfigure` 优先级最高**：只要 `reconfigure=1`，前三个信号全部为 0，指令走第四条「同时下发三引擎」的特殊路径。

#### 4.1.3 源码精读

四路译码就这四行组合逻辑，见 [rtl/vector/vmu.sv:180-183](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L180-L183)：

```systemverilog
assign is_load   = ~instr_in.reconfigure & instr_in.microop[`LD_BIT]  & ~is_toepl;
assign is_store  = ~instr_in.reconfigure & ~instr_in.microop[`LD_BIT] & ~is_toepl;
assign is_toepl  = ~instr_in.reconfigure & (instr_in.microop === 7'b1110011 | instr_in.microop === 7'b1010011);
assign is_reconf =  instr_in.reconfigure;
```

译码结果驱动**分派 push** 信号：每个引擎有一个 `push_*`，普通指令按类别送入对应引擎；重配指令则要求三个引擎**同时**接收（见下一节的 `is_reconf` 分支）。见 [rtl/vector/vmu.sv:185-195](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L185-L195)：

```systemverilog
always_comb begin
    if(is_reconf) begin //reconfiguration must happen simultaneously
        push_load  = valid_in & load_ready & store_ready & toepl_ready;
        push_store = valid_in & load_ready & store_ready & toepl_ready;
        push_toepl = valid_in & load_ready & store_ready & toepl_ready;
    end else begin
        push_load  = valid_in & is_load  & load_ready  & fifo_ready;
        push_store = valid_in & is_store & store_ready & fifo_ready;
        push_toepl = valid_in & is_toepl & toepl_ready & fifo_ready;
    end
end
```

注意普通分支里 `push_*` 同时要求「目标引擎 ready」**和**「跟踪 FIFO ready」（`fifo_ready`）。后者是分派的第二道闸门：当在途指令已经达到 FIFO 容量（3 条）时，`fifo_ready` 拉低，VMU 就不再接收新指令——这是 VMU 自己的反压机制。

三个子引擎以**完全相同的参数模板**例化为黑盒，分别接收自己的 `push_*` 与共享的 `instr_in`，并把各自的 `ready_o` 反馈给分派逻辑。load 引擎见 [rtl/vector/vmu.sv:197-254](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L197-L254)，store 引擎见 [rtl/vector/vmu.sv:255-298](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L255-L298)，toeplitz 引擎见 [rtl/vector/vmu.sv:299-348](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L299-L348)。每个引擎都向外暴露四类接口：① 缓存请求（`req_*`），② 写回（`wrtbck_*`，store 没有），③ 解锁（`unlock_*`），④ 状态（`is_busy_o`）。

VMU 是否就绪接收上游指令，由 `ready_o` 表达，它按指令类别挑对应引擎的 ready，再 `& fifo_ready`；重配指令则要求三个引擎**全部** ready（即全部排空）。见 [rtl/vector/vmu.sv:134-137](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L134-L137)：

```systemverilog
assign ready_o = valid_in & is_load  ? (load_ready  & fifo_ready) :
                 valid_in & is_store ? (store_ready & fifo_ready) :
                 valid_in & is_toepl ? (toepl_ready & fifo_ready) :
                                       (load_ready & store_ready & toepl_ready & fifo_ready);
```

最后 `else` 分支（三个 `is_*` 全 0）正是 `is_reconf` 的情况——它强制要求三个引擎都已 idle，保证重配发生在「完全排空」之后。

#### 4.1.4 代码实践

**实践目标**：亲手把一条 `vld`（向量 load，unit-strided，32 位）走过分派逻辑，确认它被正确归类为 `is_load`，并看清 `push_load` / `ready_o` 的成立条件。

**操作步骤（源码阅读型）**：

1. 打开 [rtl/vector/vmacros.sv:13](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmacros.sv#L13) 确认 `` `LD_BIT `` 是 6，即 `microop` 的最高位。
2. 假设 `vld` 的 `microop` 满足：bit6=1（load）、bits[5:4]=`OP_UNIT_STRIDED`(00)、bits[3:2]=`SZ_32`(00)，且 `reconfigure=0`。
3. 在 [rtl/vector/vmu.sv:180-183](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L180-L183) 逐一代入：`is_toepl` = 0（microop 不等于那两个魔数），`is_load` = 1，`is_store` = 0，`is_reconf` = 0。
4. 跳到 [rtl/vector/vmu.sv:191](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L191)：`push_load = valid_in & is_load & load_ready & fifo_ready`，其余两个 push 为 0。

**需要观察的现象 / 预期结果**：当 `load_ready=1`（load 引擎空闲）且 `fifo_ready=1`（在途未满 3）时，这条 `vld` 被推入 load 引擎；同时 `ready_o`（[vmu.sv:134](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L134)）也为 1，上游 vRRM 会在同一拍撤掉这条指令。若 `fifo_ready=0`，则 `push_load` 与 `ready_o` 同时为 0，VMU 反压上游。

> 上述 `vld` 的具体 microop 位组合未在本讲精确给出（依赖 bits[1:0]），完整编码请对照 `sim_generator.py` 的映射表确认——若本地有 QuestaSim，可用 `display` 打印 `instr_in.microop` 实测验证。

#### 4.1.5 小练习与答案

**练习 1**：一条 `vsd`（向量 store）的 `microop[6]` 应该是 0 还是 1？它会被分派到哪个引擎？

> **答案**：`vsd` 是 store，`microop[6] = LD_BIT = 0`，代入后 `is_store=1`，分派到 `vmu_st_eng`。

**练习 2**：为什么 `is_toepl` 必须在 `is_load`/`is_store` 之前判定，并用 `& ~is_toepl` 排除？

> **答案**：因为两条 toeplitz 自定义指令（`7'b1110011` / `7'b1010011`）的 bit 6 为 1，若不先排除，会被 bit 6 误判成 load。先判 `is_toepl` 再用 `~is_toepl` 把它们从 load/store 里剔除，才能保证互斥分类正确。

**练习 3**：`reconfigure` 指令的 `push_load`/`push_store`/`push_toepl` 三者有何特殊之处？

> **答案**：三者完全相同且同时为 1（当三引擎都 ready 时），表示重配要**同时**下发给三个引擎，而不是选一个；同时 `ready_o` 也要求三个引擎都已排空。

---

### 4.2 三路仲裁：用一个 FIFO 跟踪在途并对缓存端口仲裁

#### 4.2.1 概念说明

三个引擎虽然并行工作，但它们**共享唯一一条对外的缓存请求端口**（`mem_req_o`，每拍只能发出一个请求）。于是需要一个仲裁器决定「这一拍把缓存端口给哪个引擎」。VMU 的设计选择是：**按程序顺序**仲裁——最老的那条在途指令先发请求。

为了知道谁最老，VMU 用一个深度为 3 的 `fifo_duth` 当「在途顺序表」：每开始一条指令就 `push` 一个 2 位编码指明它属于哪个引擎，FIFO 先进先出的特性保证**队头永远是最老的在途指令**。深度选 3 是因为最多只有三个引擎、最多 3 条指令同时在途。

注意一个特例：toeplitz 是**预取**引擎，它放的是「将来要用」的数据，时序不敏感。因此设计给了它一条「偷空闲周期」的特权——当这一拍 load/store 都没有获得缓存授权时，缓存空闲，预取就可以插队发请求，哪怕它不是队头。

#### 4.2.2 核心流程

**在途编码**：`push_data` 用 2 位 one-hot-ish 编码引擎身份（见下文源码）。FIFO 容量约束为：

\[
\text{在途指令数} \le \text{DEPTH} = 3
\]

**缓存请求授权（grant）三路**：

- `ld_grant`：队头是 load（`pop_data==01`）且 load 引擎在请求且缓存就绪。
- `st_grant`：队头是 store（`pop_data==10`）且 store 引擎在请求且缓存就绪。
- `tp_grant`：队头是 toeplitz（`pop_data==11`）且在请求 **或** ——特殊地——本拍 load/store 都没拿到授权（缓存空闲）且预取在请求。

队头出队（`pop`）发生在「队头指令对应的引擎已经完成（变 idle）」时，这样最老指令离开后，下一条自然升为队头。

**与写回端口的关系**：写回端口的仲裁（load vs toeplitz，store 无写回）用另一套 `wb_grant`，见 4.3 节。两个端口各自独立仲裁。

#### 4.2.3 源码精读

先看在途 FIFO 的实例化与 push/pop 编码，见 [rtl/vector/vmu.sv:390-422](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L390-L422)：

```systemverilog
assign load_starts  =  push_load & ~push_store & ~push_toepl;
assign store_starts = ~push_load &  push_store & ~push_toepl;
assign toepl_starts = ~push_load & ~push_store &  push_toepl;
...
assign push_data = load_starts  ? 2'b01 :
                   store_starts ? 2'b10 : 2'b11;
assign push = load_starts | store_starts | toepl_starts;
// keep active instructions in vMU
fifo_duth #(
    .DW   (2),
    .DEPTH(3)
) fifo_duth ( ... );
```

编码约定：`2'b01`=load、`2'b10`=store、`2'b11`=toeplitz。注意 `load_starts`/`store_starts`/`toepl_starts` 都带「其余两个为 0」的约束，所以它们三者在**同拍至多一个为真**——这正是普通指令互斥的自然结果。而重配指令三个 `push_*` 同拍全 1，三个 `*_starts` 反而**全为 0**，于是 `push=0`：重配指令**不入队**（它不是常规访存，不参与在途跟踪，只需各引擎同步复位）。

`fifo_duth` 本体（[rtl/shared/fifo_duth.sv](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/shared/fifo_duth.sv)）用 one-hot 指针 + `status_cnt` 判空满：`valid = ~status_cnt[0]`（非空）、`ready = ~status_cnt[DEPTH]`（非满）。VMU 把 `ready` 接到 `fifo_ready`，把 `valid` 用作 grant 与 pop 的前提（队列里有在途指令才谈得上授权/出队）。

缓存请求端口的三路 grant 见 [rtl/vector/vmu.sv:424-428](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L424-L428)：

```systemverilog
assign ld_grant =  valid & cache_ready_i & pop_data == 2'b01 & ld_request;
assign st_grant =  valid & cache_ready_i & pop_data == 2'b10 & st_request;
// prefetcher gets access to idle cycles as well
assign tp_grant = (valid & cache_ready_i & pop_data == 2'b11 & tp_request) |
                  (~ld_grant & ~st_grant & cache_ready_i & tp_request);
```

前两路严格遵守 FIFO 顺序：只有队头是 load/store 且该引擎在请求时才授权。第三路（toeplitz）除了「队头是 toeplitz」的正常路径外，多了一个 `(~ld_grant & ~st_grant & ... & tp_request)`——这就是注释里说的「prefetcher gets access to idle cycles as well」：只要本拍 load/store 都没拿到缓存（缓存将空闲），且预取有请求，预取就**越过队头**占用这一拍。这正是预取引擎利用访存空闲周期加速、又不阻塞真正 load/store 的策略。

对外请求信号最终由这三路 grant 选通，并用 mux 把对应引擎的 `req_addr/microop/size/ticket/data` 接到 `mem_req_o`，见 [rtl/vector/vmu.sv:156-172](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L156-L172)。其中 `mem_req_o.data` 固定接 `store_req_data`（只有 store 需要写数据，load/toeplitz 的请求里 data 字段无意义）。

出队 `pop` 的条件是「队头指令对应的引擎已经完成」，见 [rtl/vector/vmu.sv:399-432](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L399-L432)：

```systemverilog
assign load_ends  = ~is_busy[0];
assign store_ends = ~is_busy[1];
assign toepl_ends = ~is_busy[2];
...
assign pop = (valid & pop_data == 2'b01 & load_ends)  |
             (valid & pop_data == 2'b10 & store_ends) |
             (valid & pop_data == 2'b11 & toepl_ends);
```

`is_busy[*]` 是各引擎汇报的忙碌状态（`is_busy[0]`=load、`[1]`=store、`[2]`=toeplitz），`load_ends = ~is_busy[0]` 即 load 引擎空闲。当队头是 load 且 load 引擎恰好空闲，说明最老的那条 load 已走完全程，出队让位给下一条。整个 VMU 的 idle 也由这三个 bit 汇聚，见 [rtl/vector/vmu.sv:139](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L139)：

```systemverilog
assign vmu_idle_o = ~|is_busy;
```

即「三个引擎都空闲」时 VMU 才向顶层报 idle。

#### 4.2.4 代码实践

**实践目标**：理解预取引擎的「偷空闲周期」路径在什么条件下生效。

**操作步骤（源码阅读型）**：

1. 假设当前 FIFO 队头是一条 store（`pop_data==2'b10`），store 引擎正在请求（`st_request=1`），同时 toeplitz 引擎也有预取请求（`tp_request=1`），缓存就绪（`cache_ready_i=1`）。
2. 在 [rtl/vector/vmu.sv:424-428](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L424-L428) 代入：`ld_grant=0`、`st_grant=1`、`tp_grant` 的第二项因 `~ld_grant & ~st_grant` 为假而失效，故 `tp_grant=0`。
3. 现在改假设：队头仍是 store，但这一拍 store 引擎**没有**请求（`st_request=0`），预取仍有请求。
4. 重新代入：`st_grant=0`，于是 `tp_grant` 第二项 `~ld_grant & ~st_grant & cache_ready_i & tp_request = 1`，预取获得缓存端口。

**需要观察的现象 / 预期结果**：只要 load/store 本拍未占用缓存，预取就能补位发请求；一旦 load 或 store 要用，预取立刻让位。这就是「预取不阻塞真访存、却榨干空闲周期」的双赢效果。**待本地验证**：可在仿真里跑带 `vtpl` 的示例，观察 `mem_req_valid_o` 在 load stall 间隙是否被预取请求填满。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `fifo_duth` 的深度选 3？

> **答案**：因为 VMU 内部只有 load/store/toeplitz 三个引擎，同一时刻最多 3 条指令在途（每引擎至多 1 条），深度 3 刚好覆盖最大在途数，再多也是浪费。

**练习 2**：假设 FIFO 里依次 push 了 load（01）、store（10）两条，load 还在跑、store 已完成。`pop` 这一拍会发生吗？

> **答案**：不会。`pop` 要求**队头**对应的引擎完成。队头是 load（`pop_data==01`），需 `load_ends`（`~is_busy[0]`）才出队。store 虽然先完成，但它不在队头，不能越过 load 出队——这正是 FIFO 维持程序顺序的意义。

**练习 3**：去掉 `tp_grant` 的第二项（空闲偷周期）会怎样？

> **答案**：预取只能严格按 FIFO 顺序、等自己升到队头才能发请求，访存空闲周期被浪费，卷积类负载的预取加速效果会明显下降；但正确性不变，因为 load/store 仍然按序访问。

---

### 4.3 写回优先级与 unlock 信号复用

#### 4.3.1 概念说明

除了缓存请求端口，VMU 还有两个**单端口**输出需要仲裁：

1. **写回端口**（`wrtbck_*`）：load 和 toeplitz 都要把数据写回 VRF，但 vIS 只给了一个写回端口，每拍只能写一个。store 不写回 VRF，不参与。
2. **unlock 端口**（`unlock_*`）：三个引擎完成后都要向 vIS 发 unlock（释放锁定的寄存器），但 unlock 接口也是一组单端口。

因为端口只有一个、引擎有三个，VMU 用**多路 mux + 优先级编码**的方式复用：把三个引擎的同类输出接到一个 mux，用一个选择信号决定这一拍把端口给谁。

写回端口的关键是**优先级**：当 load 和 toeplitz 同拍都请求写回时，给谁？设计选择「**给更老的那条**」。理由是写回顺序应尽量贴合程序顺序——更老的指令更早被解锁，计分板和后续依赖它的指令才能更早推进，也避免「新指令先于老指令完成」带来的资源释放次序混乱。为此 VMU 维护一个 2 位寄存器 `is_older`，专门记录「当前在途的 load 与 toeplitz 中谁更老」。

#### 4.3.2 核心流程

**`is_older` 寄存器（2 位 one-hot）**：

- `2'b01`：在途的 load 比 toeplitz 老（load 优先写回）。
- `2'b10`：在途的 toeplitz 比 load 老（toeplitz 优先写回）。

更新时机：每当有**新的 load 或 toeplitz 开始**（`new_ld_starts = push_load | push_toepl`）时刷新。刷新规则：「新来的」与「已在跑的另一个」比年龄——若对方引擎正忙，则对方更老（它先来）；否则新来的就是唯一/最老的。

**写回授权 `wb_grant`（2 位）**：

- 若「老指令正在请求写回」（`high_priority_grant` 非零），则 `wb_grant` = 老指令的请求；
- 否则 `wb_grant` = 普通请求（`wb_request`，谁请求给谁）。

`wb_grant[0]` 直接当作 load-vs-toeplitz 的 mux 选择：为 1 选 load 的写回数据，为 0 选 toeplitz 的。

**unlock 复用**：unlock 的 mux 用一个简单的优先级——toeplitz（`wb_grant[1]`）> load（`load_unlock_en`）> store。哪个引擎的 `unlock_en` 有效，就把它的 `reg_a/reg_b/ticket` 接到对外 `unlock_*` 端口。

#### 4.3.3 源码精读

先看 `is_older` 的更新与 `wb_grant` 仲裁，见 [rtl/vector/vmu.sv:354-384](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L354-L384)：

```systemverilog
logic [1:0] is_older;
...
//Requests by the oldest instruction are considered high priority
assign wb_grant = ({2{ any_high_priority_grant}} & high_priority_grant) |
                  ({2{~any_high_priority_grant}} & wb_request          );

assign any_high_priority_grant = |high_priority_grant;
assign high_priority_grant     = is_older & wb_request;

assign new_ld_starts = push_load | push_toepl;
always_ff @(posedge clk or negedge rst_n) begin
    if(~rst_n) begin
        is_older <= '0;
    end else if(new_ld_starts) begin
        is_older <= nxt_is_older;
    end
end

always_comb begin
    if(new_ld_starts) begin //new load started
        nxt_is_older = is_busy[2] ? 2'b10 : //the old toepl is the oldest
                                    2'b01;  //the new load is the oldest
    end else begin //new toepl started (or nothing started)
        nxt_is_older = is_busy[0] ? 2'b01 : //the old load is the oldest
                                    2'b10;  //the new toepl is the oldest
    end
end
```

解读：`high_priority_grant = is_older & wb_request`——只有「被标记为更老」**并且**「正在请求写回」的引擎才获高优先级。若存在高优先级请求，`wb_grant` 取它；否则谁请求就给谁（`wb_request`）。`wb_request[0]` 来自 load 引擎、`wb_request[1]` 来自 toeplitz 引擎（分别见 [vmu.sv:220](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L220) 与 [vmu.sv:317](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L317) 的 `wrtbck_req_o`）。

`is_older` 的 `always_comb` 里有两支对称逻辑：新 load 进来时，若 toeplitz 引擎正忙（`is_busy[2]`），说明 toeplitz 先到、更老，置 `2'b10`；否则新 load 自己最老，置 `2'b01`。新 toeplitz 进来时对称处理。`is_older` 只在 `new_ld_starts`（load 或 toeplitz 开始）时刷新，store 不参与（它不写回）。

写回端口的 mux 由 `wb_grant[0]` 选通 load 还是 toeplitz 的写回数据，见 [rtl/vector/vmu.sv:174-177](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L174-L177)：

```systemverilog
assign wrtbck_en_o     = wb_grant[0] ? ld_wb_en     : tp_wb_en;
assign wrtbck_reg_o    = wb_grant[0] ? ld_wb_reg    : tp_wb_reg;
assign wrtbck_data_o   = wb_grant[0] ? ld_wb_data   : tp_wb_data;
assign wrtbck_ticket_o = wb_grant[0] ? ld_wb_ticket : tp_wb_ticket;
```

再看 unlock 信号的复用，见 [rtl/vector/vmu.sv:142-154](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L142-L154)：

```systemverilog
assign unlock_en_o     = load_unlock_en | store_unlock_en | toepl_unlock_en;

assign unlock_reg_a_o  = wb_grant[1]    ? toepl_unlock_reg_a  :
                         load_unlock_en ? load_unlock_reg_a   :
                                          store_unlock_reg_a;
assign unlock_reg_b_o  = wb_grant[1]    ? toepl_unlock_reg_b  :
                         load_unlock_en ? load_unlock_reg_b   :
                                          store_unlock_reg_b;
assign unlock_ticket_o = wb_grant[1]    ? toepl_unlock_ticket :
                         load_unlock_en ? load_unlock_ticket  :
                                          store_unlock_ticket;
```

`unlock_en_o` 是三者的或（任意引擎要解锁就拉高），而 `reg_a/reg_b/ticket` 用一个三级优先 mux 选源：toeplitz（当 `wb_grant[1]`，即 toeplitz 正在被写回授权）> load（`load_unlock_en`）> store（兜底）。这意味着 unlock 与写回是**协同**的：当 toeplitz 拿到写回授权时，它的 unlock 也一并优先送出；否则看 load 是否要解锁；store 只在两者都没动作时才占用 unlock 端口。这样一组 unlock 端口被三个引擎时分复用，无需为每个引擎单独拉线到 vIS。

> 关于 `unlock_reg_a` / `unlock_reg_b` 两个寄存器号：回顾 u2-l3，lock 位有两位——`lock[0]` 表示源寄存器被访存消费、`lock[1]` 表示目的寄存器由访存产生。因此一次 unlock 可能需要同时释放「源」和「目的」两个寄存器，这就是 `unlock_reg_a`/`unlock_reg_b` 两个出口的由来。

#### 4.3.4 代码实践

**实践目标**：跟踪一条 `vld` 从进入 VMU 到写回 + 解锁的全过程，并验证 `is_older` 在 load/toeplitz 并存时如何影响写回先后。

**操作步骤（源码阅读型）**：

1. **分派**：`vld` 经 4.1 节判定 `is_load=1`，在 `load_ready & fifo_ready` 时 `push_load=1`，被推入 load 引擎；同时 FIFO `push_data=2'b01`，FIFO 队尾加入一条 load 记录。
2. **发请求**：当这条 load 升到 FIFO 队头（`pop_data==2'b01`）且 load 引擎发出 `ld_request` 且 `cache_ready_i` 时，`ld_grant=1`，[vmu.sv:157-170](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L157-L170) 把 `load_req_addr/...` 接到 `mem_req_o`，向缓存发请求。
3. **收响应**：缓存返回 `mem_resp_valid_i`，load 引擎（通过 `resp_*` 接口）把数据收进双行 scratchpad（内部细节见 u3-l2）。
4. **写回**：load 引擎拉起 `wb_request[0]` 请求写回。若此时 toeplitz 也请求写回，则查 `is_older`：若 `is_older==2'b01`（load 更老），`high_priority_grant=2'b01`，`wb_grant[0]=1`，[vmu.sv:174](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L174) 选 load 的 `ld_wb_*` 写回 VRF。
5. **解锁**：load 引擎拉起 `load_unlock_en`，[vmu.sv:142-154](https://github.com/ic-lab-duth/RISC-V-Vector/blob/8ded0f4036bcb34868c2d15475883d03bff37328/rtl/vector/vmu.sv#L142-L154) 把 `load_unlock_reg_a/reg_b/ticket` 接到对外 `unlock_*`，vIS 据此按 ticket 清除对应寄存器的 locked 位（release）。
6. **出队**：load 引擎完成后 `is_busy[0]→0`，`load_ends=1`，且队头仍是该 load，于是 `pop=1`，FIFO 出队，VMU 朝 idle 靠近一步。

**需要观察的现象 / 预期结果**：写回与解锁在时间上紧邻（同一条 load 的最后阶段），且当 load/toeplitz 同拍争写回时，**更老者先**。**待本地验证**：可在 `vmu.sv` 临时给 `wb_grant` 与 `is_older` 加 `$display`，跑带 `vld`+`vtpl` 的示例，观察两者同时请求写回时 `is_older` 是否真的决定了 `wb_grant[0]` 的取值。

#### 4.3.5 小练习与答案

**练习 1**：`is_older` 为什么只在 `push_load | push_toepl` 时更新，而不考虑 store？

> **答案**：因为 `is_older` 服务的对象是**写回端口**的仲裁，而 store 不写回 VRF、不参与写回授权。所以只需比较 load 与 toeplitz 的年龄，store 无关。

**练习 2**：当 load 与 toeplitz 同拍都请求写回、且 `is_older==2'b01`（load 更老）时，`wb_grant` 等于什么？这一拍谁写回？

> **答案**：`high_priority_grant = is_older & wb_request = 2'b01 & 2'b?1`，非零，故 `wb_grant = high_priority_grant = 2'b01`。`wb_grant[0]=1`，这一拍 load 的写回数据被选中写回 VRF；toeplitz 必须等下一拍。

**练习 3**：unlock 端口的 mux 优先级是 toeplitz > load > store。如果某拍 load 和 store 同时要解锁（toeplitz 没动作），谁先？

> **答案**：`wb_grant[1]` 为 0（toeplitz 未获写回授权），`load_unlock_en` 为 1，所以选 load 的 `unlock_reg_*`；store 的解锁被推迟。注意这并不丢信息——store 引擎会保持 `store_unlock_en=1` 直到下一拍它轮到为止。

---

## 5. 综合实践

**任务**：用一个时序场景把「分派 → FIFO 跟踪 → 缓存仲裁 → 写回/unlock 复用」四件事串起来，画出波形草图。

**场景**：假设上游连续送来三条指令：① `vld`（load）、② `vtpl`（toeplitz 预取）、③ `vsd`（store）。三个引擎初始都空闲，FIFO 为空，缓存每个周期都 ready。

**要求**：

1. 在草稿纸上画出 FIFO 内容随拍变化的过程（每拍 push 了什么、pop_data 是什么、何时 pop）。
2. 标出每条指令第一次拿到 `*_grant` 的拍，特别指出 `vtpl` 是否可能**先于** `vld` 拿到缓存端口（提示：看 `tp_grant` 第二项与 `pop_data` 的关系——注意 `tp_grant` 的空闲偷周期路径**不要求** `pop_data==11`，但要求 `~ld_grant & ~st_grant`；当队头是 load 且 load 还没准备好请求时，预取能否补位？）。
3. 指出当 `vld` 与 `vtpl` 同时请求写回时，`is_older` 的初值与第一次更新后的值分别是什么（提示：`vld` 先 push，此时 `is_busy[2]=0`，`is_older` 更新为 `2'b01`；随后 `vtpl` push 时 `is_busy[0]=1`，`is_older` 更新为 `2'b01`——即 load 始终更老）。
4. 用一句话总结：VMU 用「FIFO 保请求顺序 + `is_older` 保写回顺序 + mux 复用 unlock」三者如何在不增加端口的前提下支持三引擎并行。

**预期结果（要点）**：

- 三条指令依次 push 进 FIFO，编码依次为 `01`、`11`、`10`，队列最深达 3。
- 请求授权严格按队头顺序（load 先、toepl 次、store 后），除非某拍 load/store 都无请求、预取才借空闲周期插队。
- 写回端口在 load 与 toeplitz 之争中始终优先 load（`is_older==2'b01`）。
- 整条链路结束后 `is_busy` 全 0，`vmu_idle_o=1`，FIFO 被清空。

> 这是一个**源码阅读 + 推理型**实践，无需运行仿真即可完成；若想验证，可在 `vmu.sv` 关键信号（`push_data`、`pop_data`、`ld_grant/st_grant/tp_grant`、`wb_grant`、`is_older`、`unlock_en_o`）上临时加 `$display`，用 `vector_simulator/examples` 下的示例跑 QuestaSim 对照。

## 6. 本讲小结

- VMU 是从 vRRM 分叉的访存旁路：用 `is_load`/`is_store`/`is_toepl`/`is_reconf` 四路**纯组合译码**把 `memory_remapped_v_instr` 分派到三个并行子引擎，译码的核心是 `microop[6]`（`LD_BIT`）区分 load/store，两条 toeplitz 自定义编码优先识别。
- 一个深度为 3 的 `fifo_duth` 当「在途顺序表」，`push_data` 用 `01/10/11` 编码引擎身份，队头 `pop_data` 始终是最老在途指令，从而把唯一的**缓存请求端口**按程序顺序仲裁给三个引擎。
- toeplitz 预取引擎有一条特权路径：当本拍 load/store 都未获缓存授权时，它可越过队头占用空闲周期，榨干访存带宽又不阻塞真访存。
- 唯一的**写回端口**在 load 与 toeplitz 间仲裁，由 2 位寄存器 `is_older` 标记谁更老，更老者优先写回；store 不写回 VRF。
- 唯一的 **unlock 端口**被三引擎时分复用，mux 优先级为 toeplitz > load > store，配合写回授权协同释放被 lock 的寄存器（acquire-release 的 release 出口）。
- `reconfigure` 是特殊指令：要求三个引擎全部排空（`ready_o` 的 else 分支），三路 `push_*` 同拍全 1 同时下发，且不入 FIFO。

## 7. 下一步学习建议

本讲只读了 `vmu.sv` 的**仲裁层**，把三个子引擎当黑盒。接下来建议：

- **u3-l2 VMU 加载引擎**：打开 `vmu_ld_eng.sv`，看 load 引擎如何生成三种寻址模式的地址、用双行 scratchpad 实现取数与写回的解耦，以及它如何产出本讲里反复出现的 `ld_request`/`wb_request[0]`/`load_unlock_en`。
- **u3-l3 VMU 存储引擎**：打开 `vmu_st_eng.sv`，理解 store 为何不需要写回端口、如何按元素宽度打包数据，以及它的解锁时机与 load 的差异。
- **u3-l4 VMU 分块预取引擎**：打开 `vmu_tp_eng.sv`，看本讲提到的「空闲偷周期」背后那个 IDLE/ACTIVE/PREFETCH 状态机如何做 2D 地址计算与投机预取验证。
- 之后可回到 **u4-l1 解耦执行与 acquire-release 语义**，把本讲的 unlock 出口与 vIS 的 lock 入口拼成完整的跨数据通路 acquire-release 闭环。
