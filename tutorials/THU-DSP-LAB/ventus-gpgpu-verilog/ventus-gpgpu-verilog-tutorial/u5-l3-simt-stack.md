# SIMT 栈与分支发散

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 **warp 内分支发散（divergence）** 是什么问题，以及为什么 GPU 必须用 **活跃掩码（active mask）** 来管理它。
- 读懂 `simt_stack.v`：它如何把一条 `VBEQ/VBNE` 向量分支拆成「先执行哪一路、把另一路压栈」的判定逻辑，并用一个 `thread_masks` 寄存器实时维护每个 warp 当前的活跃线程。
- 读懂 `branch_join_stack.v`：它如何用「每次发散压两项」的栈结构记录延迟路径与重汇合点，并在执行到汇合点时弹栈恢复线程。
- 串起 `decodeUnit → issue → vALU → simt_stack → branch_back → warp_scheduler` 这条完整链路，理解 `JOIN` 指令如何让发散的 warp 重新汇合。

本讲承接 [u3-l4（issue 与 scoreboard）](u3-l4-issue-and-scoreboard.md)：那里讲的记分板防的是「数据冒险」，本讲讲的 SIMT 栈防的是「控制冒险」——warp 内的线程走了不同的分支方向。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个关键概念。

### 2.1 SIMT 与 warp

Ventus 是 SIMT（Single Instruction, Multiple Threads）架构。一条向量指令会广播给一个 **warp** 内的 `NUM_THREAD` 条线程（也叫 lane），它们在同一拍执行同一条指令、只是各自处理不同的数据（回顾 [u4-l2 向量 ALU](u4-l2-vector-alu.md) 的 lane 阵列）。`NUM_THREAD` 在 [define.v:11](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L11) 中定义，默认为 4（仅用于快速仿真）。

### 2.2 活跃掩码 active mask

既然一个 warp 的线程「必须执行同一条指令」，那如果遇到 `if (cond) { A } else { B }`，而 warp 内一部分线程 cond 为真、一部分为假怎么办？硬件没法让它们真的分头跑，于是采用 **掩码串行化（predication / masking）** 的折中：

- 用一个 `NUM_THREAD` 位宽的向量表示哪些线程「活跃」。某位为 1，对应 lane 才真正写回结果；为 0 的 lane 虽然也被迫执行指令，但其结果被丢弃。
- 走 `if` 分支体时，把 cond 为真的线程置 1、其余置 0；走 `else` 分支体时反过来。这样两条分支体各执行一遍，结果正确，代价是浪费了被屏蔽的 lane。

### 2.3 分支发散与重汇合

当 warp 内线程走向不同分支时，称为 **分支发散（divergence）**。最坏情况下要把分支体执行两遍。为了让发散的线程最终 **重新汇合（reconvergence）** 回同一控制流，GPU 用一个 **SIMT 栈**：

- 发散时，把「暂时不执行的另一路」连同它的跳转目标和恢复掩码压栈。
- 当前这一路执行到 **重汇合点（reconvergence PC）** 时，弹栈，切换到刚才压栈的另一路继续执行。
- 两路都跑完、到达同一个汇合点后，全部线程重新活跃。

Ventus 的重汇合点不是硬件自动算出来的，而是由编译器用 `SETRPC` 指令写进一个叫 `rpc` 的 CSR 里（回顾 [u5-l2 CSR 与分支](u5-l2-csr-and-branch.md)），通常指向 `JOIN` 指令所在地址。

> 名词速查：**diverge**（发散，走向不同分支）、**reconverge**（重汇合，回到同一控制流）、**TOS**（Top Of Stack，栈顶）、**rpc**（reconvergence PC，重汇合点寄存器）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/gpgpu_top/sm/pipeline/simt_stack/simt_stack.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/simt_stack/simt_stack.v) | SIMT 栈顶层。判定是否发散、决定先走哪一路、维护每 warp 的活跃掩码、产出对取指单元的跳转控制。 |
| [src/gpgpu_top/sm/pipeline/simt_stack/branch_join_stack.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/simt_stack/branch_join_stack.v) | 单个 warp 的栈存储。保存延迟路径的跳转 PC、恢复掩码与重汇合 PC，负责压栈/弹栈。 |
| [src/define/define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v) | 规模参数（`NUM_THREAD`、`DEPTH_THREAD`）、`VBEQ/VBNE/JOIN` 指令位模式、`CSR_RPC` 地址。 |
| src/gpgpu_top/sm/pipeline/pipe.v | 把 `simt_stack` 例化进流水线，并把它的 `out_mask` 与执行掩码相与、把 `complete` 信号回送记分板。 |
| src/gpgpu_top/sm/pipeline/valu/valu.v | vALU 计算每 lane 的比较结果，产出 `if_mask`（分支条件掩码）送 SIMT 栈。 |
| src/gpgpu_top/sm/pipeline/issue.v | 把 `VBEQ/VBNE/JOIN` 路由到 SIMT 栈（并按需同时送 vALU）。 |
| src/gpgpu_top/sm/pipeline/branch_back.v | 把 SIMT 栈的跳转请求（向量分支路径）仲裁后送回 `warp_scheduler` 改 PC。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先建立「活跃掩码」概念（4.1），再精读 `simt_stack` 顶层的分流与掩码维护（4.2），然后深入 `branch_join_stack` 的栈结构（4.3），最后把全链路串起来（4.4）。

### 4.1 分支发散与活跃掩码

#### 4.1.1 概念说明

SIMT 栈最核心的产出，是一个随执行进度动态变化的 **每 warp 活跃掩码** `thread_masks`。流水线任何一条向量指令在真正执行前，都要拿这个掩码去「裁剪」自己的执行掩码：只有既被指令本身选中、又在 `thread_masks` 里为 1 的 lane 才会真正生效。

而掩码的「分叉」来源，是向量分支指令（`VBEQ/VBNE`）在 vALU 里算出的逐 lane 比较结果。vALU 把这个结果打包成一个叫 `if_mask` 的掩码送给 SIMT 栈，由 SIMT 栈据此把 warp 的活跃线程切成两组。

#### 4.1.2 核心流程

```
VBEQ/VBNE 指令
      │  (逐 lane 比较 vrs1 与 vrs2)
      ▼
   vALU.alu_cmp[i]            ← 第 i 个 lane 比较是否成立
      │  取反
      ▼
   if_mask = ~alu_cmp          ← 送入 simt_stack 的「分支条件掩码」
      │
      ▼
simt_stack.thread_masks[wid]  ← 当前 warp 的活跃掩码（被更新）
      │
      ▼
issue_in_mask = active_mask & thread_masks   ← 真正用于执行的掩码
```

关键直觉：vALU 的比较结果是「逐 lane」的，它告诉我们 warp 内哪些线程条件成立、哪些不成立；SIMT 栈据此决定哪些线程现在活跃、哪些被推迟。

#### 4.1.3 源码精读

**（1）vALU 产出 if_mask。** 每个 lane 的 `alu` 算出比较位 `alu_cmp[i]`，再取反得到 `if_mask`：

```verilog
assign if_mask[i] = ~alu_cmp[i];
```
见 [valu.v:101](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu.v#L101)。这里 `alu` 是 [u4-l2](u4-l2-vector-alu.md) 讲过的单 lane 运算核，对 `VBEQ` 用 `FN_SEQ`（相等则置位）、对 `VBNE` 用 `FN_SNE`。取反后，`if_mask` 表示「比较 **不** 成立的 lane」。随后 `{wid, if_mask}` 经一个 `stream_fifo` 削峰送出（[valu.v:233-234](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu.v#L233-L234)）。

> 说明：`if_mask` 取的是「不成立」那组，这只是命名习惯，不影响发散机制——SIMT 栈只关心「把活跃线程切成两组」，至于哪组叫 if、哪组叫 else 是对称的。下文 4.2 会看到它如何被用到。

**（2）流水线用 out_mask 裁剪执行掩码。** `simt_stack` 维护的 `thread_masks` 经 `out_mask_o` 输出，在 `pipe.v` 里与操作数采集器送来的 `active_mask` 相与：

```verilog
assign issue_in_mask = operand_collector_out_isvec ?
                       (operand_collector_out_active_mask & simt_stack_out_mask) :
                       operand_collector_out_active_mask;
```
见 [pipe.v:878](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L878)。也就是说：**向量指令** 的最终执行掩码 = 指令自带掩码 ∩ SIMT 栈当前活跃掩码；标量指令不受 SIMT 栈约束。这正是分支发散「屏蔽部分 lane」的硬件落点。

#### 4.1.4 代码实践

**实践目标：** 确认「活跃掩码」如何影响一条向量指令真正写回哪些 lane。

**操作步骤：**

1. 打开 [pipe.v:878](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L878)，确认 `issue_in_mask` 的来源。
2. 在 `simt_stack.v` 找到 `out_mask_o` 的赋值（[simt_stack.v:255](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/simt_stack/simt_stack.v#L255)），看清它读的是 `thread_masks[input_wid_i]` 这一段。
3. 顺着 `input_wid_i` 在 [pipe.v:1895](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1895) 看到它接的是 `operand_collector_out_wid`——即「即将执行的那条指令所属的 warp」。

**需要观察的现象：** `out_mask_o` 是按 `input_wid_i` 选出的对应 warp 的掩码；不同 warp 有各自独立的掩码段，互不干扰。

**预期结果：** 你能解释「为什么标量指令不受 SIMT 栈影响、而向量指令必须与 `out_mask` 相与」——因为只有向量指令才会发生 warp 内分支发散。

#### 4.1.5 小练习与答案

**练习 1：** 假设 `NUM_THREAD=4`，某 warp 当前 `thread_masks=4'b1010`（线程 1、3 活跃），执行一条向量加法 `VADD_VV`，指令自带 `active_mask=4'b1111`。真正写回结果的 lane 是哪几个？

**答案：** `issue_in_mask = 1111 & 1010 = 1010`，只有线程 1、3 写回；线程 0、2 虽参与运算但结果丢弃。

**练习 2：** 为什么标量指令（`isvec=0`）不需要与 `out_mask` 相与？

**答案：** 标量指令只作用于一个标量寄存器，不涉及 warp 内多 lane 的并行，不存在「warp 内部分支发散」问题，因此直接用 `active_mask` 即可。

---

### 4.2 simt_stack 顶层：分流判定与 thread_masks 维护

#### 4.2.1 概念说明

`simt_stack` 是整个机制的协调者，但自身不存「延迟路径」的细节——那交给 4.3 的 `branch_join_stack`。`simt_stack` 负责三件事：

1. **判定是否真的发散**：如果所有活跃线程条件都一致（全走同一路），那根本不用压栈，直接当普通分支处理。
2. **决定先走哪一路**：若真发散了，选线程数较少的那一路先执行，以减少被浪费的 lane（一种简单的启发式优化）。
3. **维护 `thread_masks`**：一个 `NUM_WARP × NUM_THREAD` 位的寄存器组，记录每个 warp 当前的活跃掩码，并随分支/汇合事件实时更新。

#### 4.2.2 核心流程

`simt_stack` 同时接收两路输入：来自 issue 的 **分支控制信号** `branch_ctl`（含 wid、分支目标 PC、当前执行 PC、进入时的掩码、以及 opcode=0 分支/1 汇合）和来自 vALU 的 **条件掩码** `if_mask`。两者按 wid 配对后触发处理。

```
                  branch_ctl (opcode=0 分支)
                        │  +  if_mask (vALU 条件掩码)
                        ▼
        ┌──────────── 计算两组掩码 ────────────┐
        │  if_mask' = if_mask & mask_init     │  ← 条件不成立的活跃线程
        │  else_mask = (~if_mask') & mask_init│  ← 条件成立的活跃线程
        └──────────────────┬──────────────────┘
                           ▼
              div_occur? (两组都非空?)
              ┌─────── 是 ───────┐───── 否 ─────┐
              ▼                                   ▼
    take_if = (if_cnt < else_cnt) ?        按普通分支处理：
              选线程少的一路先执行           直接跳/不跳，不压栈
              │
              ├─ 更新 thread_masks ← 选中那一路的掩码
              ├─ 把另一路 (掩码+跳转PC+重汇合PC) 压入 branch_join_stack
              └─ 产出 fetch_ctl (是否跳转、跳到哪)
```

判定先走哪一路的依据是「数 1 的个数」比较：\( \text{take\_if} = (\text{if\_cnt} < \text{else\_cnt}) \)，即哪组线程少就先执行哪组，让被浪费的 lane 数最小化。

#### 4.2.3 源码精读

**（1）计算两组掩码与是否发散。** [simt_stack.v:172-178](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/simt_stack/simt_stack.v#L172-L178)：

```verilog
assign if_mask  = if_mask_mask_i & branch_ctl_buf_mask_init_o;   // 条件不成立组
assign else_mask = (~if_mask) & branch_ctl_buf_mask_init_o;       // 条件成立组
assign take_if   = ((if_cnt < else_cnt) && div_occur) || if_only;
assign if_only   = (else_mask == 'h0);   // 只有条件不成立组 → 不发散
assign else_only = (if_mask  == 'h0);   // 只有条件成立组 → 不发散
assign div_occur = !(if_only || else_only); // 两组都非空才算真发散
```

`branch_ctl_buf_mask_init_o` 是「进入这条分支指令时 warp 的活跃掩码」（从 issue 经 `issue_simtExeData_mask_init_o` 传来，见 [issue.v:434](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L434)）。两组人数用 `pop_cnt` 算出（[simt_stack.v:320-334](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/simt_stack/simt_stack.v#L320-L334)）。

**（2）决定要压栈的「另一路」数据。** [simt_stack.v:180-189](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/simt_stack/simt_stack.v#L180-L189)：

```verilog
if(take_if) begin                       // 先走条件不成立组(数值较少)
  pushdata_new_mask = else_mask;        //   把条件成立组压栈推迟
  pushdata_jump_pc  = branch_ctl_buf_pc_branch_o;   // 它将来跳到分支目标
end else begin                          // 先走条件成立组
  pushdata_new_mask = if_mask;          //   把条件不成立组压栈推迟
  pushdata_jump_pc  = branch_ctl_buf_pc_execute_o + 3'h4; // 它将来顺序执行
end
```

注意被压栈的总是「另一路」——现在不执行的那组，连同它将来恢复时要跳转的目标 PC。

**（3）产出对取指的跳转控制 fetch_ctl。** [simt_stack.v:130-146](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/simt_stack/simt_stack.v#L130-L146)：分两种情况——`JOIN`（opcode=1）时按栈顶决定是否跳；`VBEQ/VBNE`（opcode=0）时，若先走「条件成立组」（`!take_if`）或全成立（`else_only`），则跳到分支目标，否则顺序执行不跳。

**（4）维护 thread_masks 寄存器。** [simt_stack.v:237-253](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/simt_stack/simt_stack.v#L237-L253)，三个更新来源：

```verilog
if(!rst_n) thread_masks <= 全1;                                   // 复位：所有线程活跃
else if(发散发生) thread_masks[wid] <= take_if ? if_mask : else_mask;  // 切到选中那一路
else if(JOIN且确实跳) thread_masks[wid] <= bj_mask[wid];           // 汇合：恢复栈顶掩码
else if(栈空)        thread_masks[wid] <= 全1;                     // 全部汇合完成
```

这就是 4.1 里 `out_mask_o` 读的那个寄存器（[simt_stack.v:255](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/simt_stack/simt_stack.v#L255)）。它用「位段切片」`thread_masks[(NUM_THREAD*(wid+1)-1)-:NUM_THREAD]` 按 wid 索引，等价于一个「每 warp 一项」的掩码数组。

**（5）complete 信号回送记分板。** [simt_stack.v:148](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/simt_stack/simt_stack.v#L148) 的 `complete_valid_o`，在 [pipe.v:1278](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1278) 被接进 `scoreb_br_ctrl[wid]`——即 SIMT 分支处理完成后，触发记分板释放该 warp 的分支占用（与 `branch_back` 跳转完成、`warp_control` 完成并列三个释放源之一）。这正是本讲与 [u3-l4 scoreboard](u3-l4-issue-and-scoreboard.md) 的衔接点。

#### 4.2.4 代码实践

**实践目标：** 走查一次「1 比 3」的发散，体会 `take_if` 如何选少数派先执行。

**操作步骤：** 设 `NUM_THREAD=4`，warp 进入时 `mask_init=4'b1111`。一条 `VBEQ` 比较 `vrs1==vrs2`，结果线程 0/1/2 相等、线程 3 不等。

1. 推算 `alu_cmp`：相等的位置（线程 0/1/2）为 1 → `alu_cmp=4'b0111`。
2. 推算 `if_mask`（取反，[valu.v:101](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu.v#L101)）：`if_mask=4'b1000`（线程 3）。
3. 在 [simt_stack.v:172-175](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/simt_stack/simt_stack.v#L172-L175) 推算：`if_mask'=1000`、`else_mask=0111`，`if_cnt=1`、`else_cnt=3`。
4. 推算 `take_if`：\( 1 < 3 \) 且发散 → `take_if=1`，即先执行线程 3 这少数派。

**需要观察的现象：** `take_if=1` 时 `thread_masks` 被设成 `if_mask=1000`（只有线程 3 活跃），而压栈推迟的是 `else_mask=0111`（三线程相等组），它将来跳到 `pc_branch`。

**预期结果：** 少数派（1 个线程）先跑，多数派（3 个线程）被压栈——这样当前这一路只浪费 3 个 lane，而不是反过来浪费 1 个后还要……总之把即时浪费降到最低。

**待本地验证：** 上述掩码推演可手工完成；若要在仿真中观察，需在 `simt_stack.v` 的 `take_if`、`pushdata_new_mask` 处加临时打印，并用含发散分支的测试用例（如 `tc_*` 中带 `VBEQ` 的 kernel）跑 VCS 查看 `test.fsdb`。

#### 4.2.5 小练习与答案

**练习 1：** 若 `if_cnt == else_cnt`（两组人数相等），`take_if` 取何值？先执行哪一组？

**答案：** `take_if=((if_cnt<else_cnt)&&div_occur)||if_only`，相等时 `if_cnt<else_cnt` 为假，且非 `if_only`，故 `take_if=0`，先执行 `else_mask`（条件成立组）。

**练习 2：** `div_occur=0`（不发散）时，`push` 信号（见下节 [simt_stack.v:193](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/simt_stack/simt_stack.v#L193)）会如何？

**答案：** `push` 条件里含 `div_occur`，不发散时 `push=0`，不压栈——因为所有线程走同一路，无需记录延迟路径。

---

### 4.3 branch_join_stack：双压栈与重汇合

#### 4.3.1 概念说明

`simt_stack` 用 `generate for` 给 **每个 warp** 例化了一个独立的 `branch_join_stack`（[simt_stack.v:191-214](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/simt_stack/simt_stack.v#L191-L214)）。这是真正存放「延迟路径」信息的地方。

它用了一个精巧的 **「每次发散压两项」** 设计：一次发散往栈里压两个相邻表项——一个记「延迟的那一路」（将来切过去执行），一个记「重汇合点」（将来全部线程恢复）。这样 `JOIN` 时只要比较「当前 PC 是否等于栈顶的重汇合 PC」就能判断该不该弹栈，逻辑非常规整。

#### 4.3.2 核心流程

栈里每个表项存三个字段：

| 字段 | 含义 |
| --- | --- |
| `rpc`（reconverge pc） | 重汇合点 PC（来自 `rpc` CSR） |
| `jpc`（jump pc） | 这一路恢复时要跳转到的目标 PC |
| `nmk`（new mask） | 这一路恢复时的活跃掩码 |

压栈/弹栈用 `rd_ptr`（读指针，指向当前栈顶 TOS）和 `wr_ptr`（写指针）管理：

```
push（一次发散）:
   写 wr_ptr   项 ← {rpc, jpc=rpc,      nmk=当前完整掩码}   // 重汇合项
   写 wr_ptr+1 项 ← {rpc, jpc=延迟路PC,  nmk=延迟路掩码}      // 延迟路径项
   rd_ptr <= wr_ptr+1            // 栈顶指向延迟路径项
   wr_ptr <= wr_ptr+2            // 写指针前进两项

pop（执行到重汇合点 & JOIN到来）:
   当前PC == 栈顶rpc ?  → 是则 jump_o=1
   跳到 栈顶jpc，掩码换成 栈顶nmk
   rd_ptr <= rd_ptr-1
   wr_ptr <= wr_ptr-1
```

「当前 PC 是否等于栈顶 rpc」这一比较是关键：它把「什么时候该切换到延迟路/什么时候该最终汇合」完全交给控制流自然推进——执行到汇合点就触发，不需要额外计数器。

#### 4.3.3 源码精读

**（1）栈存储与三项打包。** 为省面积，三项各用一个扁平的大位宽寄存器实现（注释里保留了等价的数组写法）：

```verilog
reg [((`DEPTH_THREAD+22)*32)-1:0]         stack_mem_rpc, stack_mem_jpc;
reg [((`DEPTH_THREAD+22)*`NUM_THREAD)-1:0] stack_mem_nmk;
```
见 [branch_join_stack.v:42-43](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/simt_stack/branch_join_stack.v#L42-L43)。深度 `DEPTH_THREAD+22`（`DEPTH_THREAD=$clog2(NUM_THREAD)`，见 [define.v:45](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L45)）。`NUM_THREAD=4` 时为 24 项，每次发散占 2 项，故约支持 12 级嵌套发散。

**（2）何时弹栈：比较栈顶 rpc 与当前 PC。** [branch_join_stack.v:52-54](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/simt_stack/branch_join_stack.v#L52-L54)：

```verilog
assign is_pop      = (stack_mem_rpc[(32*(rd_ptr+1)-1)-:32] == pc_execute_i);
assign jump_o      = stack_empty_o ? 1'h0 : (is_pop && pop_i);
```
即「TOS 的重汇合 PC == 正在执行的 PC」且来了 `JOIN`（`pop_i`）时，才真正跳。`stack_empty_o = (wr_ptr==0)`（[第58行](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/simt_stack/branch_join_stack.v#L58)）。

**（3）压栈一次写两项。** [branch_join_stack.v:103-119](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/simt_stack/branch_join_stack.v#L103-L119) 的核心：

```verilog
else if(push_i) begin
  // 写 wr_ptr+1（延迟路径项）：跳到延迟路PC，掩码为延迟路掩码
  // 写 wr_ptr   （重汇合项）：跳到rpc，    掩码为当前完整掩码
  stack_mem_rpc[...wr_ptr+1 与 wr_ptr 两段...] <= {pushdata_recon_pc_i, pushdata_recon_pc_i};
  stack_mem_jpc[...wr_ptr+1 与 wr_ptr 两段...] <= {pushdata_jump_pc_i,  pushdata_recon_pc_i};
  stack_mem_nmk[...wr_ptr+1 与 wr_ptr 两段...] <= {pushdata_new_mask_i, thread_mask_i};
end
```

注意两项的 `rpc` 都填同一个重汇合点；区别在 `jpc`/`nmk`：`wr_ptr+1` 项是延迟路径（`jump_pc`、`new_mask`），`wr_ptr` 项是重汇合恢复点（`rpc`、当前完整 `thread_mask`，用于最终汇合时恢复全部线程）。

**（4）指针更新。** [branch_join_stack.v:60-77](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/simt_stack/branch_join_stack.v#L60-L77)：`push` 时 `rd_ptr←wr_ptr+1`、`wr_ptr←wr_ptr+2`（前进两项）；`jump_o` 时两者各减 1。配合上节 [simt_stack.v:244-248](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/simt_stack/simt_stack.v#L244-L248)：弹到栈空时 `thread_masks` 恢复全 1，标志该层发散彻底汇合。

**（5）输出恢复信息。** [branch_join_stack.v:121-122](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/simt_stack/branch_join_stack.v#L121-L122) 把栈顶 `jpc`、`nmk` 输出为 `new_pc_o`、`new_mask_o`，供 `simt_stack` 在 `JOIN` 时（[simt_stack.v:131-134](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/simt_stack/simt_stack.v#L131-L134)）决定跳转目标与新掩码。

#### 4.3.4 代码实践

**实践目标：** 用 4.2.4 的「1 比 3」例子，把栈的内容变化画出来。

**操作步骤（承接 4.2.4，先走线程 3）：** 设重汇合点 `rpc=0x40`（`JOIN` 地址），分支目标 `pc_branch=0x20`，分支指令在 `pc_execute=0x10`。

1. **发散压栈（push）：** 按上节写入两项——
   - `wr_ptr+1` 项（延迟路径）：`rpc=0x40, jpc=0x20, nmk=0111`（线程 0/1/2，将来跳到 0x20）。
   - `wr_ptr` 项（重汇合）：`rpc=0x40, jpc=0x40, nmk=1111`（最终汇合，全线程）。
   - 压栈后 `rd_ptr` 指向延迟路径项，`thread_masks=1000`（线程 3 顺序执行 0x14、0x18…）。
2. **线程 3 跑到 JOIN（PC=0x40）：** `is_pop = (栈顶 rpc 0x40 == 0x40)=1`，`JOIN` 触发 `jump_o` → 跳到 `jpc=0x20`，`thread_masks←0111`。指针各减 1，栈顶变成重汇合项。
3. **线程 0/1/2 从 0x20 执行分支体，再到 JOIN（PC=0x40）：** 再次 `is_pop=1`，跳到 `jpc=0x40`（rpc），`thread_masks←1111`。指针再减，`wr_ptr=0` → 栈空 → 全线程恢复活跃。

**需要观察的现象：** 每次发散产生两次「PC 到达 0x40」的弹栈：第一次切换到延迟路径，第二次恢复全部线程。

**预期结果：** 你能解释「为什么压两项而非一项」——一项管「切换到延迟路」，另一项管「最终全线程汇合」，二者共享同一个 rpc 判据，状态机因此无需额外计数器。

**待本地验证：** 指针与栈内容的时序需在波形中确认；可在 `branch_join_stack.v` 的 `rd_ptr/wr_ptr` 与 `stack_mem_*` 上观察一次完整发散—汇合周期。

#### 4.3.5 小练习与答案

**练习 1：** 为什么两项的 `rpc` 必须填同一个值？

**答案：** 弹栈判据是「当前 PC == 栈顶 rpc」。延迟路径项与重汇合项都应在「执行到重汇合点」时触发，故两者 rpc 相同，都用编译器设定的同一个 `rpc`。

**练习 2：** 若程序里嵌套了两层 `if`（外层未汇合就又发散），栈会怎样？

**答案：** 第二次发散再压两项，`wr_ptr` 再前进 2。只要总嵌套深度不超过 `(DEPTH_THREAD+22)/2` 级，栈就能正确处理；超出则会溢出（本设计未做溢出保护，需编译器保证嵌套深度）。

---

### 4.4 全链路协作：VBEQ/VBNE/JOIN 的译码、发射与跳转

#### 4.4.1 概念说明

把前三节拼起来：一条 `VBEQ` 从译码到最终改 PC，要经过四个角色的接力。理解这条链路，才能在波形里定位 SIMT 栈的输入输出。

#### 4.4.2 核心流程

```
decodeUnit:  译码出 simt_stack=1, simt_stack_op=0(分支)/1(汇合)
   │
   ▼ (经 ibuffer → ibuffer2issue)
issue:       若 simt_stack_op=0(VBEQ/VBNE): 同时送 vALU(算比较) + simt_stack(branch_ctl)
             若 simt_stack_op=1(JOIN):      只送 simt_stack(无需算比较)
   │
   ├──► vALU ──if_mask──► simt_stack.if_mask
   │
   └──► simt_stack.branch_ctl (wid, pc_branch, pc_execute, mask_init, opcode)
                            │  + pc_reconv (来自 rpc CSR, 即 csrfile_simt_rpc)
                            ▼
                   simt_stack 判定 → thread_masks 更新 + 压/弹栈
                            │
                            ▼ fetch_ctl (jump? new_pc?)
                   branch_back (向量分支路径 v_*, 与标量分支 s_* 仲裁)
                            │
                            ▼
                   warp_scheduler 改 PC (取指重定向)
```

`JOIN` 不需要 vALU，因为它不比较寄存器，只负责「执行到汇合点时弹栈恢复」。

#### 4.4.3 源码精读

**（1）译码产出两个控制位。** [decodeUnit.v:605-606](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L605-L606)：`ctrlSignals[36]→simt_stack`（是否与 SIMT 栈相关）、`[35]→simt_stack_op`（0=分支、1=汇合）。三条指令的编码（[decodeUnit.v:577-583](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L577-L583)）：

| 指令 | 位模式（[define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v)） | ALU 功能 | simt_stack | simt_stack_op |
| --- | --- | --- | --- | --- |
| `VBEQ`（[L762](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L762)） | 向量分支，相等跳转 | `FN_SEQ` | 1 | 0（分支） |
| `VBNE`（[L767](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L767)） | 向量分支，不等跳转 | `FN_SNE` | 1 | 0（分支） |
| `JOIN`（[L673](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L673)） | 重汇合 | `FN_ADD`（占位） | 1 | 1（汇合） |

**（2）issue 的差异化路由。** [issue.v:540-561](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L540-L561)：当 `simt_stack=1` 时——`op=0`（`VBEQ/VBNE`）需 vALU 与 simt_stack **同时就绪**才发射（valid 三者与起来）；`op=1`（`JOIN`）只发给 simt_stack。同时 [issue.v:431-435](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L431-L435) 把 `pc_branch`（来自立即数 `in3`）、`pc_execute`（当前 PC）、`wid`、`opcode`、`mask_init` 打包成 `branch_ctl`。

**（3）pc_reconv 来自 rpc CSR。** [pipe.v:1892-1893](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1892-L1893) 把 `pc_reconv_i` 接到 `csrfile_simt_rpc`——即 `rpc` CSR（地址 `0x80c`，[define.v:1234](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L1234)）。它由 `SETRPC` 指令预设（回顾 [u5-l2](u5-l2-csr-and-branch.md) 的 `custom_signal_0` 旁路），通常指向紧跟分支体后的 `JOIN` 地址。

**（4）跳转经 branch_back 回取指。** `simt_stack` 的 `fetch_ctl_*` 接到 `branch_back` 的向量分支输入 `v_*`（[pipe.v:2063-2066](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L2063-L2066)）。`branch_back` 用「标量分支优先」仲裁（[branch_back.v:37-59](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/branch_back.v#L37-L59)：`s_valid_i` 优先于 `v_valid_i`），把胜出者的 `jump/new_pc` 送 `warp_scheduler` 重定向取指。

#### 4.4.4 代码实践

**实践目标：** 在源码里走完 `VBEQ` 从译码到改 PC 的完整链路，标注每个信号的传递点。

**操作步骤：**

1. 在 [decodeUnit.v:578](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L578) 确认 `VBEQ` 译出 `simt_stack=1, simt_stack_op=0`，ALU 用 `FN_SEQ`。
2. 在 [issue.v:542-547](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L542-L547) 确认 `op=0` 时 vALU 与 simt_stack 同时被点亮。
3. 在 [pipe.v:1880-1890](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1880-L1890) 确认 `branch_ctl_*` 与 `if_mask_*` 两组输入接到 `simt_stack`，且 `pc_reconv` 来自 `rpc` CSR。
4. 在 [pipe.v:1901-1905](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1901-L1905) 确认 `fetch_ctl_*` 经 `branch_back` 的 `v_*` 送出。

**需要观察的现象：** `VBEQ` 的「比较」走 vALU、「控制」走 simt_stack，两者并行；`JOIN` 则完全不经过 vALU。

**预期结果：** 你能画出一张包含 5 个节点（decodeUnit→issue→{vALU, simt_stack}→branch_back→warp_scheduler）的信号流图，并指出 `rpc` CSR 是重汇合点的唯一来源。

**待本地验证：** 链路时序建议在含 `VBEQ/JOIN` 的 kernel 仿真波形中，按 wid 跟踪 `issue_out_SIMT_*`、`valu_out2simt_if_mask`、`simt_stack_fetch_ctl_jump`、`branch_back_out_*` 这几组信号的一次完整跳变。

#### 4.4.5 小练习与答案

**练习 1：** `JOIN` 指令为什么不需要读 `vrs1/vrs2`（译码里 `A1_X/A2_X`）？

**答案：** `JOIN` 只做弹栈恢复，不比较寄存器，因此 `issue` 不点亮 vALU、译码把源操作数选择设为 `A1_X/A2_X`（无效）。

**练习 2：** 如果编译器忘了在分支体后放 `JOIN`（或 `rpc` 没指向正确地址），会发生什么？

**答案：** `is_pop` 依赖「当前 PC == 栈顶 rpc」。若执行流到不了正确的 rpc，栈永远不会弹空，`thread_masks` 无法恢复全 1，warp 会一直带着部分活跃掩码执行，导致后续指令行为错误。所以 `SETRPC`/`JOIN` 的配合是正确性的硬约束。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成一次「发散—汇合」的全流程推演与定位。

**任务：** 设 `NUM_THREAD=4`，给出如下伪程序（地址为示意）：

```
0x10: SETRPC  x?, 0x40      ; 设 rpc=0x40 (JOIN 地址)
0x14: VBEQ    v1, v2, 0x20  ; 若 v1==v2 跳 0x20；假设线程0/1相等、线程2/3不等
0x18: <then 体: 仅不等线程执行>
0x20: <else 体: 仅相等线程执行>
0x40: JOIN                  ; 重汇合
```

请完成：

1. **掩码推演：** 算出 `alu_cmp`、`if_mask`、`if_mask'`、`else_mask`、`if_cnt`、`else_cnt`、`take_if`，确定先执行哪一组、`thread_masks` 设成什么、压栈推迟哪一组及其 `jump_pc`。
2. **栈内容推演：** 写出压栈后两个表项的 `{rpc, jpc, nmk}`，标出 `rd_ptr/wr_ptr`。
3. **汇合推演：** 描述线程到达 `0x40` 时的两次弹栈分别跳到哪、掩码恢复成什么，最终 `thread_masks` 如何回到全 1。
4. **源码定位：** 在 [simt_stack.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/simt_stack/simt_stack.v) 与 [branch_join_stack.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/simt_stack/branch_join_stack.v) 中，分别指出承载步骤 1、2、3 逻辑的代码行号。

**参考答案要点：**

1. `alu_cmp=0011`（线程0/1相等）、`if_mask=~0011=1100`、`if_mask'=1100 & 1111=1100`、`else_mask=0011`；`if_cnt=2, else_cnt=2`；`take_if=((2<2)&&1)||0=0` → 先执行 `else_mask=0011`（相等线程，跳到 `0x20`）；`thread_masks←0011`；压栈推迟 `if_mask'=1100`，`jump_pc=pc_execute+4=0x18`。
2. 两项：延迟路径项 `{rpc=0x40, jpc=0x18, nmk=1100}`；重汇合项 `{rpc=0x40, jpc=0x40, nmk=1111}`；`rd_ptr` 指向延迟路径项，`wr_ptr` 前进 2。
3. 相等线程从 `0x20` 执行到 `0x40`：第一次弹栈 → 跳 `0x18`、`thread_masks←1100`（不等线程跑 then 体）；它们再到 `0x40`：第二次弹栈 → 跳 `0x40`、`thread_masks←1111`，`wr_ptr=0` 栈空，全线程恢复。
4. 步骤1 见 [simt_stack.v:172-189](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/simt_stack/simt_stack.v#L172-L189)；步骤2 见 [branch_join_stack.v:103-119](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/simt_stack/branch_join_stack.v#L103-L119)；步骤3 见 [branch_join_stack.v:52-77](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/simt_stack/branch_join_stack.v#L52-L77) 与 [simt_stack.v:244-249](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/simt_stack/simt_stack.v#L244-L249)。

## 6. 本讲小结

- **分支发散** 是 SIMT 的固有代价：warp 内线程条件不同时，硬件用 **活跃掩码** 屏蔽部分 lane，把分支体执行多遍。最终执行掩码 = 指令掩码 ∩ `thread_masks`（[pipe.v:878](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L878)）。
- **vALU 产出 `if_mask=~alu_cmp`**（[valu.v:101](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/valu/valu.v#L101)），把活跃线程切成两组；`simt_stack` 据此判定 `div_occur` 与 `take_if`（少数派优先），更新 `thread_masks` 并把另一路压栈。
- **`branch_join_stack` 用「每次发散压两项」** 的栈：一项记延迟路径，一项记重汇合恢复点，两者共享同一个 `rpc`，靠「当前 PC == 栈顶 rpc」触发弹栈，无需额外计数器。
- **重汇合点来自 `rpc` CSR**（`SETRPC` 预设），`JOIN` 指令在执行到 rpc 时弹栈恢复，是正确性的硬约束。
- **全链路** 为 `decodeUnit → issue → {vALU, simt_stack} → branch_back → warp_scheduler`；`VBEQ/VBNE` 同时驱动 vALU 与 simt_stack，`JOIN` 只驱动 simt_stack。
- `complete_valid_o` 回送记分板（[pipe.v:1278](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1278)），释放 SIMT 分支对记分板的占用，衔接 [u3-l4](u3-l4-issue-and-scoreboard.md)。

## 7. 下一步学习建议

- **继续精读执行与控制单元：** 本讲是 [u5 访存与控制](u5-l1-lsu.md) 单元的分支/控制部分。若还未读 [u5-l4 张量核](u5-l4-tensor-core.md)，可接着了解另一种高性能执行单元。
- **回顾数据冒险侧：** 把本讲的「控制冒险」与 [u3-l4 scoreboard](u3-l4-issue-and-scoreboard.md) 的「数据冒险」对照，理解 SM 流水线如何同时处理两类冒险。
- **深入存储侧：** 分支发散会放大访存请求的掩码效应，建议接着学习 [u6-1 dcache 与 MSHR](u6-l1-dcache-and-mshr.md)，看 LSU 如何在部分 lane 活跃时处理向量 load/store。
- **二次开发视角：** 若你打算扩展指令集（[u8-l4](u8-l4-isa-extension.md)），注意任何新的 warp 内条件控制流指令都必须正确接入 `simt_stack` 的 `branch_ctl`/`if_mask` 接口，否则会破坏发散/汇合语义。
