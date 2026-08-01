# u4-l5 SFU 特殊功能单元

## 1. 本讲目标

本讲聚焦 Ventus SM 流水线中最后一类执行单元——**SFU（Special Function Unit，特殊功能单元）**。学完后你应该能够：

- 说清楚 SFU 在整条流水线里扮演的「慢车道」角色：它专门承接那些**周期数高、面积大**的运算（整数除法/取余、浮点除法/开方），把它们从快速 ALU/FPU 通路里剥离出来。
- 理解规模参数 `NUM_SFU = NUM_THREAD/4` 如何决定 SFU 的「折叠」结构：物理运算核数只有线程数的 1/4，靠**分组迭代**若干拍覆盖整个 warp。
- 读懂 `int_div` 模块基于**符号归一化 + 迭代商选择（SRT 风格）**的多周期状态机，以及它对除零、溢出、被除数小于除数等边界情况的处理。
- 理解一条 SFU 指令如何从 `issue` 被路由、在 SFU 内部跑完多拍、再把结果经 `writeback` 写回，并在此期间靠 `scoreboard` 的忙位保持依赖指令阻塞。

## 2. 前置知识

在进入本讲前，请确保你已经理解以下概念（它们在前置讲义中已建立，这里只做最简回顾）：

- **SIMT 与 lane 级并行**：一条向量指令会被广播给整个 warp，在 `NUM_THREAD` 个 lane 上并行执行（见 u4-l2 vALU、u4-l4 vFPU）。vALU/vFPU 是「全并行」的——每个 lane 配一个运算核。
- **指令译码控制字 `ctrlSignals`**：`decodeUnit` 把 32 位指令查表成一个打包控制位串，其中的 `fp` 位、`sfu` 位（`ctrlSignals[8]`）、`alu_fn` 功能码会随指令一路送到执行单元（见 u3-l3）。
- **发射 `issue` 是纯组合路由器**：它按 `tc → sfu → fp → ... → 默认 sALU` 的优先级，把一条指令的握手接通到唯一一个执行单元（见 u3-l4）。
- **记分板 `scoreboard` 的忙位机制**：每个 warp 一份位图，指令进入采集器时把目的寄存器置「忙」，写回时清「忙」；只要源/目的操作数对应位为忙，`delay_o` 就拉高，暂停该 warp（见 u3-l4、u4-l1）。

本讲要回答的核心问题是：**当一类运算「快不起来」时，硬件如何既不拖累快速通路，又能正确地把结果送回去？** 答案就是 SFU 的「独立慢车道 + 折叠迭代 + 记分板阻塞」三件套。

> 补充一个名词：**SRT 除法**。它是一类「每拍猜一位商、同时更新部分余数」的迭代除法算法，通过把被除数和除数预先归一化（对齐到固定范围），使得每拍的商位可以用简单的区间比较（而非全比较）快速选定。本讲的 `int_div` 不是教科书上的恢复余数法，而是带预归一化的迭代商选择，思想与 SRT 同源，下文统称「SRT 风格」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/define/define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v) | 定义 `NUM_SFU`（SFU 物理核数）与 SFU 专属的 `FN_DIV/FN_REM/FN_DIVU/FN_REMU/FN_FDIV/FN_FSQRT/FN_EXP` 功能码 |
| [src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v) | SFU 顶层：输入 FIFO 缓冲、分组迭代 FSM、例化 `NUM_SFU` 个 `int_div` 与 `div_sqrt_top_mvp`、结果仲裁与写回出口 |
| [src/gpgpu_top/sm/pipeline/sfu_v2/int_div.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/int_div.v) | 整数除法核：五状态机（IDLE→PRE→COMPUTE→RECOVERY→FINISH），处理有/无符号除、取余、除零与溢出 |
| [src/gpgpu_top/sm/pipeline/sfu_v2/float_div_mvp/div_sqrt_top_mvp.sv](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/float_div_mvp/div_sqrt_top_mvp.sv) | 浮点除法/开方核，源自 **pulp-platform（ETH Zürich / University of Bologna）**，Solderpad 许可，SFU 直接例化复用 |
| [src/gpgpu_top/sm/pipeline/decodeUnit.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v) | 译码表：`DIV/DIVU/REM/REMU` 及其向量形式 `VDIV_*/VREM_*`、`FDIV_S/FSQRT_S`、`VFDIV_*/VFSQRT_V`、`VFEXP_V` 都把 `sfu` 位置 1 |
| [src/gpgpu_top/sm/pipeline/issue.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v) | 发射路由：`sfu` 位为真时把握手接通到 SFU |
| [src/gpgpu_top/sm/pipeline/scoreboard.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/scoreboard.v) | 记分板：忙位贯穿 SFU 的长延迟，写回前阻塞依赖指令 |
| [src/gpgpu_top/sm/pipeline/pipe.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v) | 例化 `sfu_exe` 并把其 `out_x/out_v` 接入 `writeback` 的标量/向量仲裁 |

## 4. 核心概念与源码讲解

### 4.1 SFU 的定位、NUM_SFU 与「折叠」结构

#### 4.1.1 概念说明

vALU 和 vFPU 追求的是「一个 warp 一拍出结果」，因此给每个 lane 都配一个运算核（`generate for` 复制 `NUM_THREAD` 份）。但**除法和开方是迭代算法**——一次 32 位除法往往要迭代十几甚至几十拍，且单个除法器的面积远大于一个加减法器。如果仍按「每 lane 一个核」的全并行方式实现，面积代价不可接受。

于是硬件做了一个工程权衡：把所有「慢运算」集中到一个独立单元 SFU，并且**只配少量物理核，靠多拍迭代分批处理一个 warp 的所有 lane**。这就是本讲的核心思想——**折叠（folding）**：用时间换面积。这和 vALU 顶层用 `ALU_NOT_FOLD` 宏在「全并行」与「折叠省面积」之间切换（见 u4-l2）是同一种思路，只不过 SFU 默认就是折叠的。

控制 SFU 物理核数的开关就是 `NUM_SFU`：

[src/define/define.v:37](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L37) —— `NUM_SFU = NUM_THREAD >> 2`，即线程数的 1/4。

那么「一个 warp 有 `NUM_THREAD` 个 lane，SFU 只有 `NUM_SFU` 个核，如何覆盖全部 lane？」答案是分成 `NUM_GRP` 组，每组 `NUM_SFU` 个 lane，迭代 `NUM_GRP` 拍：

\[ \text{NUM\_GRP} = \frac{\text{NUM\_THREAD}}{\text{NUM\_SFU}} = \frac{\text{NUM\_THREAD}}{\text{NUM\_THREAD}/4} = 4 \]

也就是说，**无论 `NUM_THREAD` 取多少，SFU 恒为 4 组迭代**；变的只是每组并行处理的 lane 数（= `NUM_SFU`）。下表给出两种典型配置：

| `NUM_THREAD` | `NUM_SFU`（物理核数） | `NUM_GRP`（迭代组数） | 含义 |
| --- | --- | --- | --- |
| 4（默认仿真） | 1 | 4 | 1 个除法器，4 拍串行处理 4 个 lane |
| 32（综合） | 8 | 4 | 8 个除法器并行，共 4 组 |

#### 4.1.2 核心流程

一条向量 SFU 指令（如 `VDIV_VV`）在 SFU 内部的处理节奏：

1. **整组进入**：warp 的 `NUM_THREAD` 个 lane 的操作数与掩码一起进入 SFU 输入缓冲。
2. **按组迭代**：每拍用 `fixed_pri_arb` 在 4 个组里选出当前要处理的一组（`i_cnt`），把该组的 `NUM_SFU` 个 lane 操作数切片送进 `NUM_SFU` 个物理除法器。
3. **核内多拍**：每个 `int_div`/`div_sqrt_top_mvp` 核自身还要迭代若干拍才出结果。
4. **回填与推进**：该组结果写回 `out_data` 对应位置，掩码推进到下一组（`next_mask`），直到所有活跃组处理完。
5. **整 warp 完成**：进入 `S_FINISH`，把 `out_data` 经结果 FIFO 送往写回。

注意：步骤 2 的「组间迭代」与步骤 3 的「核内迭代」是**两层嵌套的迭代**——外层把 lane 分组，内层在每个核里算完一次除法。

#### 4.1.3 源码精读

`NUM_GRP` 与 FSM 状态定义在 sfu_exe 顶部：

[src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v:61-66](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v#L61-L66) —— `NUM_GRP = NUM_THREAD/NUM_SFU`（恒为 4），并定义三态 `S_IDLE/S_BUSY/S_FINISH`。

掩码按 `NUM_SFU` 位一组压缩成 `mask_grp`，用于组选择：

[src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v:138-142](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v#L138-L142) —— `mask_grp[i] = |mask[NUM_SFU*(i+1)-1 : NUM_SFU*i]`，即「第 i 组里只要有任意一个 lane 活跃，该组就需处理」。这一步把逐 lane 的掩码折叠成逐组的 4 位掩码。

#### 4.1.4 代码实践

- **实践目标**：直观感受 `NUM_THREAD` 改变时 SFU 物理核数的变化。
- **操作步骤**：
  1. 打开 [src/define/define.v:37](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L37)。
  2. 心算（或用 `$clog2` 表达式推算）`NUM_THREAD=4` 与 `NUM_THREAD=32` 两种情况下 `NUM_SFU` 的值。
  3. 在 sfu_exe.v 中确认 `NUM_GRP` 在两种配置下都等于 4。
- **需要观察的现象**：`NUM_THREAD` 翻倍时，`NUM_SFU`（物理除法器个数）随之翻倍，但迭代组数 `NUM_GRP` 不变。
- **预期结果**：`NUM_THREAD=4 → NUM_SFU=1`；`NUM_THREAD=32 → NUM_SFU=8`；两者 `NUM_GRP` 均为 4。
- **待本地验证**：若实际改参数综合，可在综合报告中对比「`int_div` 实例数」是否等于 `NUM_SFU`。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 SFU 选择「`NUM_THREAD/4` 个核 + 4 组迭代」，而不是像 vALU 那样「每 lane 一核」？
  - **答案**：除法器面积大、延迟高，全并行面积不可接受；用 1/4 的核数配合 4 拍迭代，用时间换面积，是面积/性能的折中。vALU 的加减法器小且单拍，才能承受全并行。
- **练习 2**：若把 `NUM_SFU` 改成 `NUM_THREAD/2`，`NUM_GRP` 会变成多少？对性能和面积分别有何影响？
  - **答案**：`NUM_GRP = NUM_THREAD/(NUM_THREAD/2) = 2`，组间迭代减半、吞吐提升，但物理除法器数量翻倍、面积增大。

---

### 4.2 sfu_exe：输入缓冲、分组迭代 FSM 与整数/浮点双通路

#### 4.2.1 概念说明

`sfu_exe` 是 SFU 的总调度器，它要做三件事：

1. **缓存输入**：把随握手进来的操作数与一堆控制信号（`wid/fp/isvec/alu_fn/reg_idxw/wvd/wxd/...`）打包存进一个深度为 1 的 FIFO，把上游 `issue` 与下游多拍运算**解耦**。
2. **分发到两类核**：用 `fp` 位区分整数运算（除/余，送 `int_div`）与浮点运算（除/开方，送 `div_sqrt_top_mvp`），两者**共用同一套数据切片与组迭代逻辑**，最后用一个 2 选 1 仲裁器选出有效结果。
3. **串接写回**：结果分标量（`out_x`）与向量（`out_v`）两路，各经一个结果 FIFO 送出。

关键设计：**整数核与浮点核是「同时例化、二选一使用」**。一条指令要么走整数通路、要么走浮点通路，由 `i_fp` 决定；未使用的那条通路保持 `valid=0`，仲裁器自然忽略它。

#### 4.2.2 核心流程

顶层三态 FSM（区别于 `int_div` 内部的五态机）：

```
S_IDLE ──收到指令──> S_BUSY ──所有活跃组算完──> S_FINISH ──结果被写回取走──> S_IDLE
                       │                                            ↑
                       └─────────────（组间迭代 + 核内多拍）──────────┘
```

- `S_IDLE`：输入 FIFO 有数据且握手成功 → `S_BUSY`，同时锁存 `mask`、置 `i_valid`。
- `S_BUSY`：每拍由 `fixed_pri_arb` 选出当前组 `i_cnt`，切片出该组 `NUM_SFU` 个 lane 的操作数送核；核返回结果（`sfu_out_fire`）后回填 `out_data` 并用 `next_mask` 推进掩码。向量指令要等「所有活跃组」处理完才进 `S_FINISH`；标量指令只需一组（`k==0`）。
- `S_FINISH`：把 `out_data` 经 `result_x/result_v` FIFO 发出，握手成功后回 `S_IDLE`。

#### 4.2.3 源码精读

输入缓冲 FIFO 与打包（注意位宽把三套操作数 + 掩码 + 全部控制信号都拼进来）：

[src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v:76-103](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v#L76-L103) —— 把 `in_in1/2/3`、`in_mask`、`wid/fp/reverse/isvec/alu_fn/reg_idxw/wvd/wxd/rm` 打包成 `data_buffer_enq_bits` 存入深度 1 的 `stream_fifo`，`w_ready_o` 直接作为对上游 `issue` 的 `in_ready_o`。

组选择（固定优先级仲裁 + one-hot 转二进制得到组号 `i_cnt`）：

[src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v:188-203](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v#L188-L203) —— `fixed_pri_arb` 在 4 位 `mask_grp` 里选最低有效组，`one2bin` 转成二进制 `i_cnt`，作为切片操作数与回填结果的位置索引。

按 `i_cnt` 切片出当前组的操作数（并处理 `reverse` 反向除法 `VFRDIV_VF`）：

[src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v:230-233](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v#L230-L233) —— 从 `NUM_THREAD` 个 lane 里切出第 `i_cnt` 组共 `NUM_SFU` 个 lane 的 `in1/in2`，`reverse` 时交换两源（把 `rs1/vs2` 的反向除法统一成 `vs2/rs1`）。

**核心连接块**（每实例化一个 SFU 物理核就同时挂一个 `int_div` 和一个 `div_sqrt_top_mvp`，并做整数/浮点二选一）：

[src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v:382-476](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v#L382-L476) —— `generate for(n=0;n<NUM_SFU;n++)` 循环内完成全部接线。其中几个关键赋值：

- [sfu_exe.v:388](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v#L388) `intdiv_sign_bit[n] = !i_alu_fn[1]`：用功能码第 1 位区分有/无符号。`FN_DIV(0)/FN_REM(1)` 的 `alu_fn[1]=0` → 有符号；`FN_DIVU(2)/FN_REMU(3)` 的 `alu_fn[1]=1` → 无符号。
- [sfu_exe.v:389](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v#L389) `intdiv_in_valid[n] = !i_fp && i_valid`：整数通路只在非浮点时激活。
- [sfu_exe.v:393-394](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v#L393-L394) `float_div_start = (alu_fn[2:0]==0)`、`float_sqrt_start = (alu_fn[2:0]!=0)`：浮点通路用功能码低 3 位区分除法（`FN_FDIV=0`）与开方（`FN_FSQRT=1` 等）。
- [sfu_exe.v:466](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v#L466) `arb_in_0[n] = i_alu_fn[0] ? intdiv_r[n] : intdiv_q[n]`：用功能码第 0 位区分取余（`FN_REM/REMU`，`alu_fn[0]=1` → 余数 `r`）与取商（`FN_DIV/DIVU`，`alu_fn[0]=0` → 商 `q`）。
- [sfu_exe.v:473](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v#L473) `arb_out[n] = arb_in_valid_0[n] ? arb_in_0[n] : arb_in_1[n]`：整数结果有效则取整数，否则取浮点。

于是功能码的每一位都被复用了：`[1]` 选符号、`[0]` 选商/余、`[2:0]` 选除/开方。这正是 u1-l3 所说「`FN_*` 编码按执行单元分命名空间、数值可复用」的具体体现——`FN_DIV=0` 与 `FN_FDIV=0` 同值却各得其所。

`int_div` 与 `div_sqrt_top_mvp` 的例化：

[src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v:435-447](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v#L435-L447) —— 例化 `int_div invdiv`，输入被除数 `a_i`、除数 `d_i`、符号位 `sign_bit`，输出商 `q_o` 与余数 `r_o`。

[src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v:449-464](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v#L449-L464) —— 例化 `div_sqrt_top_mvp flaot_div_sqrt`（注意作者拼写为 `flaot`），用 `Div_start_SI/Sqrt_start_SI` 选运算，`Format_sel_SI=2'b00` 固定 FP32，`Precision_ctl_SI=6'h00` 全精度，结果高 32 位丢弃（`unused_reasult`）、低 32 位为 `float_result`。

顶层 FSM 与掩码推进：

[src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v:486-533](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v#L486-L533) —— 三态机：`S_BUSY` 内向量指令靠 `next_mask` 是否归零判断是否全部组完成。

[src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v:536](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v#L536) —— `next_mask = mask & ({{(NUM_THREAD-NUM_SFU){1'b1}},{NUM_SFU{1'b0}}} << (i_cnt*NUM_SFU))`，即「把刚处理完的第 `i_cnt` 组对应的 `NUM_SFU` 个位清掉」，实现组间推进。

#### 4.2.4 代码实践

- **实践目标**：验证「功能码位复用」的译码逻辑。
- **操作步骤**：
  1. 对照 [src/define/define.v:545-551](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L545-L551) 写出四个整数功能码的二进制：`FN_DIV=6'd0`、`FN_REM=6'd1`、`FN_DIVU=6'd2`、`FN_REMU=6'd3`。
  2. 对每条指令，分别写出 `alu_fn[1]`（决定符号）和 `alu_fn[0]`（决定商/余）的值。
  3. 在 [sfu_exe.v:388 与 466](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v#L388-L466) 对照确认你的推断。
- **需要观察的现象**：`DIVU/REMU` 与 `DIV/REM` 唯一区别在 `alu_fn[1]`；`REM/REMU` 与 `DIV/DIVU` 唯一区别在 `alu_fn[0]`。
- **预期结果**：填出下表（答案见 4.2.5）。
- **待本地验证**：无。

| 指令 | `alu_fn` | `alu_fn[1]`→符号 | `alu_fn[0]`→商/余 |
| --- | --- | --- | --- |
| DIV | 0 | 有符号 | 商 |
| DIVU | 2 | ? | ? |
| REM | 1 | ? | ? |
| REMU | 3 | ? | ? |

#### 4.2.5 小练习与答案

- **练习 1**：补全上表。
  - **答案**：DIVU：无符号、商；REM：有符号、余；REMU：无符号、余。
- **练习 2**：`VFEXP_V`（`FN_EXP=6'd4`）也被译码为 `fp=Y`、`sfu=Y` 而进入 SFU（见 [decodeUnit.v:593](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L593)）。它的 `alu_fn[2:0]=3'b100≠0`，会落在 `float_sqrt_start` 分支。这意味着什么？
  - **答案**：从代码连线看，`VFEXP_V` 复用了浮点开方通路进入 `div_sqrt_top_mvp`。该浮点核源自 pulp-platform 的共享除法/开方单元，**是否真正实现指数运算依赖其内部迭代控制**，本讲不展开其内部状态机，**待本地验证**其实际指数结果是否正确。

---

### 4.3 int_div：SRT 风格迭代除法状态机

#### 4.3.1 概念说明

`int_div` 是 SFU 整数通路的核心，承担 `DIV/DIVU/REM/REMU`。它的算法可拆成四步：

1. **符号与归一化（PRE）**：先把操作数转成绝对值（无符号量），并记住最终商/余数的符号；再用 `find_first` 找到被除数和除数的最高有效位位置，把它们**左移对齐**到固定位置——这是 SRT 风格算法能「每拍简单猜一位商」的前提。
2. **迭代商选择（COMPUTE）**：每拍看部分余数的高两位，判定本位商 `+1/0/-1`（用 `q` 记正商位、`qn` 记负商位），同时把部分余数左移并加减归一化后的除数。
3. **余数恢复（RECOVERY）**：迭代结束后若部分余数为负，需加回一次除数做修正，并把商的正/负位合并成最终商。
4. **边界与符号处理（FINISH）**：处理除零（商全 1、余数=被除数）、有符号溢出（如 `INT_MIN / -1`）、被除数小于除数（商 0、余数=被除数），最后按记下的符号修正商和余数。

> 名词解释：**部分余数（partial remainder）**是除法迭代过程中不断被更新的「尚未除完的剩余量」，对应代码里的 `a_reg`；**商选择（quotient digit selection）**是每拍根据部分余数落在哪个区间来决定本位商，对应 `sel_pos/sel_neg`。

#### 4.3.2 核心流程

`int_div` 的五态机：

```
IDLE ──握手──> (除零/溢出?) ──是──> FINISH
                    │否
                    v
                  PRE ──(被除数<除数?)──是──> FINISH（商0）
                    │否
                    v
                  COMPUTE ──cnt减到0──> RECOVERY ──> FINISH ──握手取走──> IDLE
```

每态主要工作：

- **IDLE**：`in_ready_o=1` 等请求；握手时锁存符号、绝对值、除零/溢出标志。
- **PRE**：用 `find_first` 算出 `a_lez/d_lez`（前导零个数），进而算出迭代次数 `iter`、归一化后的 `a_norm/d_norm/d_neg_norm`。
- **COMPUTE**：循环 `iter` 次，每拍更新 `a_reg/q/qn`。
- **RECOVERY**：修正余数符号、合并正负商位、把余数右移回原始对齐。
- **FINISH**：按符号与边界条件输出最终 `q_o/r_o`，`out_valid_o=1` 等取走。

迭代次数 `iter` 由被除数与除数的前导零差决定：

\[ \text{iter} = \begin{cases} 0 & \text{if } a\_lez > d\_lez \\ d\_lez - a\_lez + 1 & \text{otherwise} \end{cases} \]

直观理解：被除数和除数对齐后，需要产生的商的位数等于它们的「有效位差 + 1」。

#### 4.3.3 源码精读

符号提取、绝对值与边界检测：

[src/gpgpu_top/sm/pipeline/sfu_v2/int_div.v:92-98](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/int_div.v#L92-L98) —— `a_sign = sign_bit & a_i[31]`（仅当 `sign_bit=1` 即有符号且最高位为 1 时取负）；`unsigned_a = a_sign ? (~a_i+1) : a_i` 取绝对值；`overflow` 检测 `INT_MIN / -1` 这种唯一的有符号溢出；`div_by_zero = (d_i==0)`。

预归一化（前导零 + 迭代次数 + 操作数左移对齐）：

[src/gpgpu_top/sm/pipeline/sfu_v2/int_div.v:142-161](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/int_div.v#L142-L161) —— 例化两个 `find_first`（复用自公共单元库，见 u8-l2）分别求 `a_lez/d_lez`；`iter` 按上式计算；`a_norm` 把被除数左移 `a_lez` 位对齐到最高位，`d_norm/d_neg_norm` 是除数及其负数（补码）左移后用于迭代加减。

迭代商选择（每拍的核心组合逻辑）：

[src/gpgpu_top/sm/pipeline/sfu_v2/int_div.v:189-192](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/int_div.v#L189-L192) —— `sel_pos = (a_reg[XLEN:XLEN-1]==2'b01)`、`sel_neg = (...==2'b10)`，即看部分余数最高两位：为 `01` 时本位商 `+1`（稍后减去除数 `a_shift + d_neg_norm`），为 `10` 时本位商 `-1`（加上除数 `a_shift + d_norm`），否则商 `0`（不变）。这正是 SRT 风格的「看高位区间选商位」。

[src/gpgpu_top/sm/pipeline/sfu_v2/int_div.v:305-310](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/int_div.v#L305-L310) —— `COMPUTE` 态每拍把 `sel_pos/sel_neg` 移入 `q/qn`（正商位与负商位分两寄存器存放），并更新 `a_reg <= a_next`、`cnt <= cnt_next`。

余数恢复与最终符号修正：

[src/gpgpu_top/sm/pipeline/sfu_v2/int_div.v:194-198](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/int_div.v#L194-L198) —— `rem_is_neg = a_reg[XLEN+1]`，若迭代结束部分余数为负则加回除数 `recovery_r`；`common_q = rem_is_neg ? (q + ~qn) : (q - qn)` 用「正商位减负商位」合并出无符号最终商（负商位的补偿）。

[src/gpgpu_top/sm/pipeline/sfu_v2/int_div.v:216-224](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/int_div.v#L216-L224) —— `signed_q = q_sign_reg ? (~common_q_reg + 1) : common_q_reg` 按商符号取补；`special_q` 处理除零（全 1）与溢出（`INT_MIN`）；`special_r` 在除零或商为 0 时把余数置为原被除数；最终 `q_o/r_o` 在边界与正常结果间二选一。

状态转移：

[src/gpgpu_top/sm/pipeline/sfu_v2/int_div.v:227-276](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/int_div.v#L227-L276) —— `IDLE` 握手后若除零/溢出直接跳 `FINISH`，否则进 `PRE`；`PRE` 中若被除数小于除数（商必为 0）也跳 `FINISH`；`COMPUTE` 按 `cnt_next` 减到 0 进 `RECOVERY`；`RECOVERY` 一拍后进 `FINISH`；`FINISH` 等结果被取走回 `IDLE`。

#### 4.3.4 代码实践

- **实践目标**：手动模拟一次小位宽迭代，理解 `sel_pos/sel_neg` 如何逐位生成商。
- **操作步骤**：
  1. 取一个简单例子：被除数 `a=12(1100)`、除数 `d=3(0011)`，预期商 `4(0100)`、余数 `0`。
  2. 归一化后 `d` 对齐到最高位；按 [int_div.v:189-192](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/int_div.v#L189-L192) 的规则，逐拍记录 `a_reg` 高两位与 `sel_pos/sel_neg`。
  3. 用 `common_q = q - qn` 合并正负商位，验证是否得到 `4`。
- **需要观察的现象**：每位商的判定只依赖部分余数最高两位，无需全宽比较——这正是 SRT 风格能高速迭代的原因。
- **预期结果**：合并后的商为 `4`、余数为 `0`。
- **待本地验证**：完整 32 位的逐拍波形需在 Verdi 中查看 `int_div` 实例的 `a_reg/q/qn/cnt` 信号确认（手算仅作定性理解）。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `int_div` 要把商的正位和负位分开放在 `q` 和 `qn` 两个寄存器，而不是每拍直接算出最终商？
  - **答案**：SRT 风格允许本位商为 `-1`，负商位无法直接写入「0/1」的商寄存器；先分别累计正/负商位，迭代结束后用 `q - qn`（或 `q + ~qn`）一次性合并补偿，逻辑更简洁、关键路径更短。
- **练习 2**：`INT_MIN(-2^31) / -1` 在 32 位有符号除法中会发生什么？代码如何处理？
  - **答案**：正确结果 `+2^31` 无法用 32 位有符号数表示，属于溢出。代码在 [int_div.v:97](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/int_div.v#L97) 用 `overflow` 检测该唯一情形，并在 [int_div.v:218](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/int_div.v#L218) 输出 `special_q = {1'b1,{31{1'b0}}}`（即 `INT_MIN`），与 RISC-V「溢出时商=被除数、余数=0」的语义一致。

---

### 4.4 与流水线衔接：issue 路由、写回与 scoreboard 阻塞

#### 4.4.1 概念说明

SFU 不是孤立存在的，它前后各有一个关键衔接点：

- **前端**：`decodeUnit` 给除法/开方类指令打上 `sfu=1` 标记，`issue` 据此把指令路由进 SFU。
- **后端**：SFU 结果经 `writeback` 写回寄存器堆；在结果写回之前，`scoreboard` 中该目的寄存器保持「忙」，任何依赖它的指令都会被 `delay_o` 暂停。

第二点是理解 SFU「慢」却不出错的关键：**SFU 的多拍延迟被记分板的忙位完整覆盖**。忙位在指令进入采集器时置 1，一直保持到 SFU 写回那拍才清 0——无论 SFU 跑了 5 拍还是 50 拍。

#### 4.4.2 核心流程

从译码到写回的完整链路：

```
decodeUnit：DIV/VDIV/FDIV/VFSQRT... → ctrlSignals[8](sfu)=1, fp=(浮点类), alu_fn=FN_*
   │
   v
issue：if(sfu) 把 valid/ready + 操作数 + 控制信号接通到 sfu_exe
   │
   v
sfu_exe：S_IDLE→S_BUSY(分组迭代 + int_div/div_sqrt 多拍)→S_FINISH
   │
   v
result_x / result_v FIFO → writeback 仲裁（标量6路 / 向量6路之一）
   │
   v
寄存器堆写回 + scoreboard 对应忙位清 0 → 依赖指令解除 delay
```

#### 4.4.3 源码精读

译码置 `sfu` 位（`ctrlSignals[8]`）：

[src/gpgpu_top/sm/pipeline/decodeUnit.v:348-351](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L348-L351) —— 标量 `DIV/DIVU/REM/REMU` 的译码表项，末段含 `Y`（即 `sfu` 位）。

[src/gpgpu_top/sm/pipeline/decodeUnit.v:466-473](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L466-L473) —— 向量 `VDIV_VV/VDIV_VX/VDIVU_*/VFDIV_VV/VFDIV_VF/VFRDIV_VF/VFSQRT_V` 等，`isvec=Y`、浮点类同时 `fp=Y`，且都置 `sfu=Y`。

[src/gpgpu_top/sm/pipeline/decodeUnit.v:626](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L626) —— `control_Signals_sfu_0_o = ctrlSignals_0[8]`，确认 `sfu` 就是控制字的第 8 位。

`issue` 路由到 SFU：

[src/gpgpu_top/sm/pipeline/issue.v:468-472](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L468-L472) —— `else if(inputBuf_warps_control_Signals_sfu) begin issue_out_SFU_valid_o = inputBuf_valid; inputBuf_ready = issue_out_SFU_ready_i; ... end`，把握手接通 SFU（优先级排在 TC 之后、vFPU 之前）。

`pipe.v` 例化 `sfu_exe` 并接入写回：

[src/gpgpu_top/sm/pipeline/pipe.v:1908](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1908) —— `sfu_exe sfu(...)`，输入接 `issue_out_SFU_*`，输出接 `sfu_out_x_*/sfu_out_v_*`。

[src/gpgpu_top/sm/pipeline/pipe.v:880-891](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L880-L891) —— 把 `sfu_out_x_*` 拼进标量写回 6 路仲裁 `{mul,sfu,csr,lsu,fpu,salu}`，把 `sfu_out_v_*` 拼进向量写回 6 路仲裁 `{tc,mul,sfu,lsu,fpu,valu}`。

`scoreboard` 忙位如何覆盖 SFU 延迟：

[src/gpgpu_top/sm/pipeline/scoreboard.v:84-93](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/scoreboard.v#L84-L93) —— 向量忙位：`if_fire && if_wvd` 时置 1（指令进入采集器），`wb_v_fire && wb_v_wvd` 时清 0（写回）。SFU 指令的写回要等到 `S_FINISH` 才发生，故忙位在此期间一直为 1。

[src/gpgpu_top/sm/pipeline/scoreboard.v:103](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/scoreboard.v#L103) —— 标量忙位置/清同理（注意写 `x0` 永不置忙）。

[src/gpgpu_top/sm/pipeline/scoreboard.v:185](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/scoreboard.v#L185) —— `delay_o = read_rs1|read_rs2|read_rs3|read_mask|read_wb|read_beep|read_opcol|read_fence`，其中 `read_rs*` / `read_wb` 读的就是上述忙位；只要后续指令的源或目的撞上 SFU 占用的忙位，该 warp 即被暂停，直到 SFU 写回。

#### 4.4.4 代码实践

- **实践目标**：把「SFU 多拍延迟 ↔ scoreboard 阻塞」串成一条可观测的调用链。
- **操作步骤**：
  1. 在 [pipe.v:1908](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L1908) 找到 `sfu` 的 `out_v_wb_wvd_rd_o` 输出，沿 [pipe.v:886-891](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L886-L891) 追到 `writeback` 的向量输入。
  2. 确认 `writeback` 的写回使能会反馈到 `scoreboard` 的 `wb_v_fire_i`（见 [scoreboard.v:89](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/scoreboard.v#L89)）。
  3. 在仿真中（如 `tc_vecadd` 框架）跑一条 `VDIV_VV`，用 Verdi 同时观察：SFU 的 `c_state`（应停留 `S_BUSY` 多拍）、`int_div` 的 `current_state`、目的向量寄存器在 `scoreboard` 中的忙位、以及依赖该寄存器的下一条指令的 `delay_o`。
- **需要观察的现象**：`int_div` 进入 `COMPUTE` 多拍期间，记分板对应忙位保持 1，依赖指令的 `delay_o` 保持高；直到 SFU 走到 `S_FINISH` 并写回，忙位才清 0，`delay_o` 随之释放。
- **预期结果**：依赖指令恰好在 SFU 写回后的下一拍得以发射，验证了「忙位完整覆盖 SFU 延迟」。
- **待本地验证**：上述波形现象需在本地 VCS+Verdi 环境中实际跑通 `make run-vcs-4w4t` 后确认（本讲不假定已运行）。

#### 4.4.5 小练习与答案

- **练习 1**：如果把 SFU 写回到 `writeback` 的连线断开（极端假设），scoreboard 的忙位会怎样？对流水线有什么影响？
  - **答案**：忙位永远不会被清 0（`wb_v_fire` 不发生），该目的寄存器永久「忙」，所有依赖它的指令（含同 warp 后续读该寄存器的指令）会被 `delay_o` 永久阻塞，流水线死锁。这反过来说明写回通路对正确性至关重要。
- **练习 2**：SFU 的多拍停顿会拖慢整个 warp，但 GPU 常说「用多 warp 切换隐藏延迟」。这套机制是怎么配合的？
  - **答案**：当 warp A 因 SFU 除法被 `delay_o` 暂停时，`warp_scheduler` 会切到其它就绪的 warp B/C 继续取指发射（见 u3-l1/u3-l4 的「漏斗」结构）。SFU 占用的是 warp A 的记分板忙位，不影响其它 warp；只要驻留 warp 数足够多，就能用其它 warp 的指令填满 SFU 迭代的空档，实现延迟隐藏。

---

## 5. 综合实践

**任务：追踪一条 `VDIV_VV` 指令从译码到写回的完整生命周期，并解释每处「慢」是如何被正确处理的。**

请按顺序完成：

1. **译码层**：在 [decodeUnit.v:466](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/decodeUnit.v#L466) 找到 `VDIV_VV` 表项，确认它设置了 `isvec=Y`、`alu_fn=FN_DIV`、`sfu(ctrlSignals[8])=Y`，并指出它**没有**置 `fp`（故走整数通路）。
2. **发射层**：在 [issue.v:468-472](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/issue.v#L468-L472) 说明 `sfu` 位如何让指令进入 SFU 而非 vALU。
3. **SFU 顶层**：在 [sfu_exe.v:382-476](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v#L382-L476) 解释：为什么 `VDIV_VV` 会激活 `intdiv_in_valid` 而不是 `float_in_valid`？为什么 `intdiv_sign_bit=1`（有符号）？为什么结果取 `intdiv_q` 而非 `intdiv_r`？（提示：分别对应 `!i_fp`、`!alu_fn[1]`、`!alu_fn[0]`。）
4. **迭代层**：在 [sfu_exe.v:536](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/sfu_exe.v#L536) 说明 `next_mask` 如何逐组推进，使得 `NUM_THREAD` 个 lane 在 `NUM_GRP=4` 组里全部得到处理。
5. **除法核**：在 [int_div.v:189-192](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/int_div.v#L189-L192) 说明每个核内部如何用 `sel_pos/sel_neg` 逐拍生成商位。
6. **阻塞与写回**：在 [scoreboard.v:84-93](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/scoreboard.v#L84-L93) 说明目的向量寄存器忙位何时置 1、何时清 0，并在 [pipe.v:886-891](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/pipe.v#L886-L891) 确认 SFU 结果经哪条写回路径清掉它。

**产出**：一张标注了「SFU 在 S_BUSY 停留期间，记分板忙位为 1、依赖指令被 delay」的时序示意草图（手绘即可），从而把「慢运算、折叠迭代、记分板阻塞」三者串联成一条完整的正确性证据链。

## 6. 本讲小结

- SFU 是 SM 流水线的「慢车道」，集中处理整数除法/取余（`DIV/DIVU/REM/REMU` 及向量形式）与浮点除法/开方（`FDIV_S/FSQRT_S/VFDIV_*/VFSQRT_V`），把它们从快速 ALU/FPU 通路剥离。
- 规模参数 `NUM_SFU = NUM_THREAD/4` 决定物理核数，`NUM_GRP = NUM_THREAD/NUM_SFU = 4` 决定组间迭代数——这是一种「1/4 核数 + 4 拍迭代」的折叠设计，用时间换面积。
- `sfu_exe` 用三态机 `S_IDLE/S_BUSY/S_FINISH` 串联输入缓冲、分组迭代与写回；整数核 `int_div` 与浮点核 `div_sqrt_top_mvp`（源自 pulp-platform）同时例化，由 `fp` 位二选一。
- 功能码位被高度复用：`alu_fn[1]` 选有/无符号、`alu_fn[0]` 选商/余、`alu_fn[2:0]` 选浮点除/开方——这是「`FN_*` 编码按执行单元分命名空间、数值可复用」的典范。
- `int_div` 是带预归一化的 SRT 风格迭代除法器：五态机 `IDLE→PRE→COMPUTE→RECOVERY→FINISH`，用 `q/qn` 分别累计正/负商位最后合并，并妥善处理除零、`INT_MIN/-1` 溢出、被除数小于除数等边界。
- SFU 的多拍延迟由 `scoreboard` 的忙位完整覆盖：进入采集器置忙、写回清忙，期间依赖指令被 `delay_o` 阻塞；同时其它 warp 仍可切换发射，实现延迟隐藏。

## 7. 下一步学习建议

- **横向对比三种「折叠 vs 全并行」**：回顾 u4-l2（vALU 的 `ALU_NOT_FOLD`）、u4-l3（vmul 的 `MUL_NOT_FOLD`）与本讲 SFU 的折叠，体会同一思想在不同执行单元中的落地差异。
- **进入访存与控制类执行单元**：下一讲 u5-l1 将讲解 LSU（向量访存），它同样是一个多拍、带 MSHR 的复杂单元，且其延迟也依赖 scoreboard 与多 warp 切换来隐藏，可与本讲对照阅读。
- **深入浮点核内部（选读）**：若你对 `div_sqrt_top_mvp` 的迭代开方算法感兴趣，可自行阅读 [src/gpgpu_top/sm/pipeline/sfu_v2/float_div_mvp/](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/pipeline/sfu_v2/float_div_mvp/div_sqrt_top_mvp.sv) 下的 `control_mvp.sv`、`norm_div_sqrt_mvp.sv`、`preprocess_mvp.sv`，理解 pulp-platform 共享除法/开方单元的归一化与迭代控制细节。
- **公共单元库复用**：本讲 `int_div` 用到的 `find_first`、`sfu_exe` 用到的 `fixed_pri_arb/one2bin/stream_fifo` 都来自 `src/common_cell`，将在 u8-l2 系统讲解，届时可回看本讲加深理解。
