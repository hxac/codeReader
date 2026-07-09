# 总线与握手的跨域同步及约束

## 1. 本讲目标

本讲承接 [u3-l1（CDC 基础：电平、脉冲、计数器的同步）](u3-l1-resync-basics.md)，把视野从「单比特信号」推进到「多比特总线」。学完本讲，你应当能够：

- 说清楚为什么一个相关多比特向量（状态字、控制寄存器、地址）**不能**像单比特那样，给每一位各挂一条 `async_reg` 同步链就完事——即「比特一致性（bit coherency）」问题。
- 读懂 `resync_twophase`（自由运行版）与 `resync_twophase_handshake`（带 ready/valid 握手版），解释「一个单比特电平在两域间往返翻转」如何保证整字在被采样时已经稳定。
- 区分三类时序约束的用途——`set_false_path`、`set_max_delay -datapath_only`、`set_bus_skew`——并对照 `resync_level.tcl` / `resync_counter.tcl` 解释它们为何落在同步链的第一级寄存器上。
- 在 testbench 中验证多比特数据跨域后「不丢、不乱」，并理解项目配套 `.tcl` 作用域约束的必要性。

## 2. 前置知识

本讲默认你已掌握 u3-l1 的内容，这里只做最简回顾并引出新问题。

**亚稳态与同步链（来自 u3-l1）。** 单比特信号跨异步时钟域时，目的寄存器可能在数据翻转瞬间采样而陷入亚稳态。亚稳态无法消除，只能靠「两级 `async_reg` 寄存器 + 布局到同一 slice + `dont_touch`」把 MTBF 拉到可接受。这一招对单比特足够：一位最终要么变 0、要么变 1，功能都能接受。

**本讲的新问题：多比特的「比特一致性」。** 假设要把一个 16 位计数器值 `0x5A5A → 0x5A5B` 跨域。若给这 16 位每位各搭一条独立的两级同步链，各位走线延迟不同，目的域同一拍就会采到「部分位已翻转、部分位还没翻」的拼接值——可能是 `0x5A5A`（全旧）、`0x5A5B`（全新），也可能是 `0x5A5F`、`0x5B5B` 之类**新旧混杂的垃圾值**。这种错误不亚稳、不报错，却让数据间歇性错乱，极难定位。

u3-l1 讲过，`resync_counter` 用**格雷码**化解它：相邻值只差 1 位，偏移最多造成「上一拍/这一拍」两种合法结果。但很多数据天然不是格雷码（各位会同时跳变），格雷码帮不上忙——这时就要用本讲的**两相握手（two-phase handshake）**：先用源时钟把整字锁进寄存器，等它肯定稳定后，再发一个「可以采样了」的单比特令牌过去，目的域看到令牌才采样。

> 名词速查
> - **比特一致性（bit coherency）**：采样到的所有比特来自同一个源时钟拍的快照，不存在「半新半旧」。
> - **两相握手 / 翻转握手（toggle handshake）**：用「电平跳变」而非「电平高低」表示一次事件，每次翻转 = 一次事务，对 CDC 延迟不敏感。
> - **`-datapath_only`**：`set_max_delay` 选项，表示只约束数据路径延迟、忽略两异步时钟间的偏斜（偏斜本就无意义）。
> - **`set_bus_skew`**：约束「一个总线各比特之间的相对偏斜」而非绝对延迟，专为多比特 CDC 设计。

## 3. 本讲源码地图

| 文件 | 作用 | 进哪个工程 |
| --- | --- | --- |
| [modules/resync/src/resync_twophase.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase.vhd) | 自由运行的两相 CDC，保证多比特向量跨域的比特一致性 | 综合 + 仿真 |
| [modules/resync/src/resync_twophase_handshake.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase_handshake.vhd) | 带 AXI-Stream 式 ready/valid 握手的版本，支持背压 | 综合 + 仿真 |
| [modules/resync/scoped_constraints/resync_level.tcl](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_level.tcl) | 单比特电平同步链的约束（本讲约束部分的锚点） | 仅综合 |
| [modules/resync/scoped_constraints/resync_counter.tcl](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_counter.tcl) | 格雷计数器多比特同步的约束（`set_bus_skew` 范例） | 仅综合 |
| [modules/resync/scoped_constraints/resync_twophase.tcl](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_twophase.tcl) | `resync_twophase` 的作用域约束（数据通路 + 两条 level 通路） | 仅综合 |
| [modules/resync/test/tb_resync_twophase_handshake.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_twophase_handshake.vhd) | 用 BFM 验证「跨域无丢失」的 testbench | 仅仿真 |

> 提醒（承接 [u1-l2](u1-l2-repo-and-module-layout.md)）：`scoped_constraints/*.tcl` 不是 VHDL、不参与编译，只在综合期由 `read_xdc -ref <实体名>` 应用；`test/tb_*.vhd` 只进仿真工程。

## 4. 核心概念与源码讲解

### 4.1 为什么多比特总线不能直接用寄存器链同步

#### 4.1.1 概念说明

「总线跨域」的本质，是让目的域**一次性、原子地**采样到一整组相关联的比特，而不是采到一堆新旧混杂的中间态。hdl-modules 给出两条互补路线：

1. **单调 ±1 的计数器类数据**（如 FIFO 读写指针）：用**格雷码**。相邻值只差 1 位，偏移最多造成「上一拍/这一拍」两种合法结果。代表实体 `resync_counter`（u3-l1 已讲）。
2. **任意跳变的相关向量**（控制/状态寄存器、配置位组）：格雷码失效，改用**两相握手**。先用源时钟把整字锁进寄存器，待其稳定后再发单比特「许可」令牌，目的域见令牌才采样。令牌是单比特，可安全走 `async_reg` 链。

一句话对比：**格雷码靠「每次只变 1 位」化解偏移；两相握手靠「数据先稳定、后给许可」化解偏移。**

#### 4.1.2 核心流程

两相握手的关键，是在源域与目的域之间让一个**单比特 level 令牌来回翻转（toggle）**，每完成一次往返搬运一个字：

```text
源域(clk_in)                         目的域(clk_out)
-----------                          ---------------
1. 把 data 锁进 data_in_sampled  ─┐
2. 翻转 request level ──────────┼──> [async_reg x2] ──> 3. 看到 level 跳变
                                   │                       4. 采样 data_in_sampled -> data_out
                                   │                       5. 翻转 acknowledge level
6. 看到 level 跳变(ack 回来) <──┤── [async_reg x2] <────┘
7. 锁入新 data，回到第 2 步 ──────┘
```

要点：

- **数据走并行通路**（多位一起过），但**只在令牌到达、即「许可」生效时才被采样**。
- 令牌（request / acknowledge）是单比特，各经一条两级 `async_reg` 链跨域。
- 令牌要穿两级同步链（至少 2 个目的时钟周期），等目的域采到令牌时，源域早已把数据稳定保持了若干拍——这就保证了比特一致性。

为什么用「跳变」而非「高低」？跨域有延迟，一个「高脉冲」可能恰好在两个目的沿之间被错过；而「跳变」只要最终到达，目的域迟早会在某一拍发现「与记录不符」并处理，**事件绝不会丢**。

#### 4.1.3 源码精读

`resync_twophase` 的头注释把这套动机说得很直白：与 `resync_slv_level` 不同，它**内建了保证比特一致性的机制**——一个电平在输入/输出间往返，每次往返翻转一次，两侧各自在跳变时采样：

[modules/resync/src/resync_twophase.vhd:9-21](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase.vhd#L9-L21) —— 说明本实体「保证比特一致性」，机制是「电平在输入与输出之间往返旋转、每次往返翻转一次，两侧各自在跳变时采样」。

它还划定了适用边界：适合**慢变**的相关数据（计数器、状态字），**不能**处理脉冲：

[modules/resync/src/resync_twophase.vhd:28-40](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase.vhd#L28-L40) —— 适合「比特相关」的慢变数据；脉冲会被漏掉；并且与 `resync_level` 不同，本实体输入**可以**用 LUT 驱动（因为数据先被锁存、不在 CDC 通路上）。

#### 4.1.4 代码实践

**目标**：在纸上把「跳变即事件」走一遍，建立直觉（源码阅读型，无需运行）。

1. 假设 `req`、`ack` 初值都为 `0`，源域要发两笔数据。
2. 写出每一步 `req`、`ack` 的值：发第 1 笔 → `req` 翻成 `1` → 目的域采样 → `ack` 翻成 `1` → 源域发第 2 笔 → `req` 翻成 `0` → ……
3. 观察：`req`/`ack` 在 `0/1` 间反复跳变，每跳一次代表一笔事务，与「高有效脉冲」不同，它对到达时刻不敏感。

**预期结果**：电平序列呈 `0→1→0→1…`，每个跳变对应一笔数据。

#### 4.1.5 小练习与答案

**练习 1**：如果把 16 位状态寄存器每位各接一条 `resync_level` 跨域，为什么会出错？
**答案**：各链布线延迟不同，目的域同一拍会采到「新旧拼接」的值，破坏比特一致性。`resync_level` 只解决单比特亚稳态，不保证多比特同时采样；状态寄存器各位可能同时跳变，也不满足格雷码「只变 1 位」，故必须用两相握手。

**练习 2**：两相握手为什么用「电平跳变」而非「高电平脉冲」表示事务？
**答案**：跨域同步链有延迟，高脉冲可能被错过；而跳变只要最终传到，目的域迟早会发现「与记录不符」并处理，事件不会被丢弃。

---

### 4.2 resync_twophase：自由运行的两相 CDC

#### 4.2.1 概念说明

`resync_twophase` 是上面原理的最小实现，**自由运行（free-running）**：只要上电就不停地把输入端采样、往输出端送，**不管输入数据有没有变化**。对「状态字/计数快照」正合适——随时想读都能拿到一个比特一致的、新鲜的快照，重复送同一个值没有副作用。代价是它抓不住脉冲。

#### 4.2.2 核心流程

实体接口极简——两套时钟 + 一条数据总线 + 一个上电默认值，没有 ready/valid：

[modules/resync/src/resync_twophase.vhd:92-106](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase.vhd#L92-L106) —— `width` 定位宽，`default_value` 给出上电初期、第一笔真实数据到达前的输出值。

内部有三条并行通路：一条**数据通路**（`data_in_sampled → data_out_int`，多位），两条**level 往返通路**（request、acknowledge，各单比特，经 `async_reg` 链跨域）。最坏情况输入到输出延迟约为：

\[
T_{\text{latency}} \approx 4\,T_{\text{clk\_in}} + 4\,T_{\text{clk\_out}}
\]

这也决定了它的采样周期（吞吐的倒数），所以**只适合慢变信号**。资源方面 LUT 恒为 3，FF 随位宽线性增长：

[modules/resync/src/resync_twophase.vhd:43-73](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase.vhd#L43-L73) —— 给出延迟公式 `4*Tin + 4*Tout`、LUT 恒为 3、FF 以 `2*width` 速率增长，并与 `resync_counter`、浅 `asynchronous_fifo` 做了面积/延迟对比。

#### 4.2.3 源码精读

信号与属性声明：数据寄存器与电平寄存器都被 `dont_touch` 钉死，防综合工具吸收/移动逻辑；进同步链的电平寄存器再叠加 `async_reg`，强制布局到同一 slice 以优化 MTBF（属性来自 u2-l2 讲过的 `attribute_pkg`）：

[modules/resync/src/resync_twophase.vhd:108-129](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase.vhd#L108-L129) —— 声明 `data_in_sampled/data_out_int` 两级数据寄存器并加 `dont_touch`；声明输入侧 `input_level_m1/input_level/input_level_not_p1`、输出侧 `output_level_m1/output_level/output_level_p1` 三段电平，对进同步链的 4 个寄存器打 `async_reg`。

输入侧进程（`clk_in`）：检测电平回环来锁存数据，维护「取反 + 移位进同步链」的电平往返：

[modules/resync/src/resync_twophase.vhd:134-147](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase.vhd#L134-L147) —— `if input_level = input_level_not_p1` 时锁存 `data_in_sampled`；`input_level_not_p1 <= not input_level`（取反准备发往输出）；`input_level <= input_level_m1; input_level_m1 <= output_level_p1` 把后向电平移入同步链。

输出侧进程（`clk_out`）对称：检测跳变来采样数据，维护前向电平：

[modules/resync/src/resync_twophase.vhd:151-165](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase.vhd#L151-L165) —— `if output_level /= output_level_p1`（检测到跳变）时把 `data_in_sampled` 采进 `data_out_int`；`output_level_p1 <= output_level`；`output_level <= output_level_m1; output_level_m1 <= input_level_not_p1` 把前向电平移入同步链。

> 关键理解：`input_level_not_p1` 是「`not input_level` 寄存一拍」的值，所以采样条件 `input_level = input_level_not_p1` 本质是「检测 `input_level` 刚发生过跳变」——每次后向电平到达，输入侧就锁一笔新数据并把取反后的电平送回。电平就这样在两域间反复跳变，每往返一次各自采样一笔。
>
> **比特一致性从何而来**：源域把数据锁进 `data_in_sampled` 后，request level 要穿两级 `async_reg`（至少 2 个 `clk_out` 周期）才到目的域；这期间源域不会改 `data_in_sampled`（要等 acknowledge 回来才改）。所以当目的域终于看到 level 跳变、决定采样时，`data_in_sampled` 已稳定多拍，所有位都已结束翻转——采样到的必然是一个完整一致的值。

#### 4.2.4 代码实践

**目标**：在源码里把「两条 level 链 + 一条数据链」找出来，并与资源回归数对上（源码阅读型，无需运行）。

1. 在 [resync_twophase.vhd:117-129](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase.vhd#L117-L129) 中分别列出属于「前向链」「后向链」「数据通路」的信号。
2. 数一下打 `async_reg` 的寄存器（应为 4 个：输入/输出各两级）。
3. 打开 [module_resync.py:230-234](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/module_resync.py#L230-L234)，确认 `resync_twophase` 的 netlist 资源断言是 `lut=3, ff=2*width+6`，与头注释「LUT 恒 3、FF 随 `2*width` 增长」一致。

**预期结果**：4 个 `async_reg`、3 个 `dont_touch`；`width=8` 时 FF=22、`width=32` 时 FF=70。

#### 4.2.5 小练习与答案

**练习 1**：`data_in_sampled` 和 `data_out_int` 为什么都要加 `dont_touch`？
**答案**：它们是并行数据通路的两端，综合工具若吸收/合并/挪动它们，可能改变数据相对 level 令牌的时序关系，破坏「数据先稳定后采样」的前提。`dont_touch` 把它们钉死，让 4.4 节的约束能精确作用于它们之间的路径。

**练习 2**：头注释为什么说这个实体「抓不住脉冲」？
**答案**：它自由运行、按自己的节拍（令牌往返周期）采样源数据；源端短脉冲很可能落在两次采样之间而被略过。传脉冲要用 `resync_pulse`（u3-l1）。

---

### 4.3 resync_twophase_handshake：带 ready/valid 握手的版本

#### 4.3.1 概念说明

`resync_twophase` 没有「数据有效」概念，源端无法表达「这拍是新的、务必送过去」，目的端也无法背压。`resync_twophase_handshake` 在同一套两相机制上叠加 **AXI-Stream 式 ready/valid 握手**（见 [u2-l1](u2-l1-handshake-convention.md)）：输入侧 `input_valid/input_ready`，结果侧 `result_valid/result_ready`。既能「按需发送」，也支持背压，代价是多花几个 LUT/FF。

#### 4.3.2 核心流程

实体把数据握手明确出来：

[modules/resync/src/resync_twophase_handshake.vhd:56-71](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase_handshake.vhd#L56-L71) —— 输入侧 `input_clk/input_ready/input_valid/input_data`，结果侧 `result_clk/result_ready/result_valid/result_data`；只有一个 generic `data_width`。

握手语义建立在与 4.2 相同的「电平跳变」之上，但用 `xor` 把跳变翻译成 ready：

- **`input_ready = input_level xor input_level_p1`**：二者不同时，表示「上一笔已被结果侧确认、可接收新数据」。
- 一次成功的输入握手（`input_valid and input_ready`）翻转 `input_level_p1`，这一翻转作为「有新数据」的请求过同步链到结果侧。
- 结果侧检测到电平跳变、且当前没有未取走的结果时，置 `result_valid`、采样数据，并**立即翻转反馈电平**——输入侧据此重新变 ready。
- 消费者通过 `result_ready/result_valid` 取走数据，取走后翻转 `result_level_handshake`，确保在下一笔新数据到来前不重复置 valid。

延迟与吞吐（头注释给出）：

\[
T_{\text{latency}} \le T_{\text{input\_clk}} + 3\,T_{\text{result\_clk}}
\]

\[
T_{\text{sampling\_period}} \approx 3\,T_{\text{input\_clk}} + 3\,T_{\text{result\_clk}}
\]

[modules/resync/src/resync_twophase_handshake.vhd:30-46](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase_handshake.vhd#L30-L46) —— 给出延迟与采样周期公式，并指出「结果侧背压时，输入仍可在上一笔结果送出前采样下一笔」从而提升吞吐。

#### 4.3.3 源码精读

注意电平信号初值被故意设成不对称，以便上电就产生第一个 `input_ready`：

[modules/resync/src/resync_twophase_handshake.vhd:84-98](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase_handshake.vhd#L84-L98) —— `input_level_m1/input_level/result_level_feedback` 初值为 `'1'`，其余为 `'0'`；注释明说这是「为了触发第一个 `input_ready` 事件」。结果 `input_level='1'`、`input_level_p1='0'`，`xor` 出 `input_ready='1'`，开机即可接收。

输入侧进程：只要 ready 就先把数据锁存（无论 valid），valid 时才翻转请求电平：

[modules/resync/src/resync_twophase_handshake.vhd:102-126](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase_handshake.vhd#L102-L126) —— `if input_ready` 锁 `input_data_sampled`；`if input_valid` 翻转 `input_level_p1`（注释：若此时 ready 成立，这一赋值既拉低 ready、又向结果侧声明新数据）；最后把反馈电平移入同步链。第 [126 行](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase_handshake.vhd#L126) `input_ready <= input_level xor input_level_p1` 用异或把跳变翻译成 ready。

结果侧进程：检测「请求电平相对上次握手发生了跳变」且当前无积压 → 置 valid、采样、翻转反馈；消费者取走 → 清 valid、翻转握手电平：

[modules/resync/src/resync_twophase_handshake.vhd:130-161](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase_handshake.vhd#L130-L161) —— `(result_level xor result_level_handshake) and not result_valid` 时置 `result_valid`、采 `result_data_int`、翻转 `result_level_feedback`（注释称这是背压机制，多花一个 FF、几个 LUT，却能在某些场景把吞吐翻倍）；`result_ready and result_valid` 时清 valid 并翻转 `result_level_handshake`。

> **背压优化**：反馈电平在「采样到新数据」时就翻转，**而不是**等「结果被取走」才翻转。于是输入侧不必等结果真的被消费就能开始送下一个字（相当于结果侧预存一个字），在某些场景把吞吐近似翻倍——这正是头注释 [L45-46](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase_handshake.vhd#L45-L46) 所说的吞吐收益。

资源对比（来自 netlist 回归）：`resync_twophase` 为 `lut=3, ff=2*width+6`；`resync_twophase_handshake` 为 `lut=5, ff=2*width+8`——握手逻辑多花 2 个 LUT、2 个 FF：

[module_resync.py:236-244](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/module_resync.py#L236-L244) —— `resync_twophase_handshake` 的 `lut=5, ff=2*width+8, logic=2` 断言。若不需要背压，把 `input_valid`/`result_ready` 恒接 `'1'` 即退化为 `resync_twophase`，但资源略高，故无需背压时直接用 `resync_twophase` 更省。

#### 4.3.4 代码实践

**目标**：运行项目自带的「无丢失」回归测试，亲眼看到跨域数据被逐拍核对。

testbench 把同一份随机数组同时压入输入队列与结果队列：输入侧 BFM 按它发，结果侧 BFM 自动核对收到的每一拍是否与队列一致——任何丢失或错码都会断言失败：

[modules/resync/test/tb_resync_twophase_handshake.vhd:113-130](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_twophase_handshake.vhd#L113-L130) —— `run_test` 生成随机数组，`copy` 后一份送输入、一份送结果核对队列，然后等到 `num_beats_checked` 达到 `num_beats`。

[modules/resync/test/tb_resync_twophase_handshake.vhd:177-209](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_twophase_handshake.vhd#L177-L209) —— 实例化 `bfm.axi_stream_master`（输入侧，带 stall）与 `bfm.axi_stream_slave`（结果侧，自动比对 `reference_data_queue`），DUT 即 `resync_twophase_handshake`。

操作步骤（承接 [u1-l3](u1-l3-toolchain-and-deps.md) 工具链）：

1. 配好 `PYTHONPATH` 指向仓库根目录，安装 `vunit-hdl` 预发布版。
2. 运行该 testbench（VUnit 标准过滤语法，**待本地验证**确切的 `--help` 选项）：
   ```bash
   python tools/simulate.py resync.tb_resync_twophase_handshake
   ```
3. 观察 [module_resync.py:100-125](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/module_resync.py#L100-L125) 已为该 tb 配置了 5 种时钟快慢关系 × `{8,16}` 位宽组合，全部应通过。

**需要观察的现象**：即使结果时钟比输入时钟快 20 倍或慢 20 倍、即使 BFM 施加随机 stall，`num_beats_checked` 都能稳步增长到 `num_beats` 而不报错——证明无丢失、无重复、比特一致。

**预期结果**：所有配置通过；`test_count_sampling_period` 还会打印实测采样周期与理论值 `3*Tin + 3*Tout` 的比值，应在 0.80～1.001 之间（见 [tb 第 133-161 行](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_twophase_handshake.vhd#L133-L161)）。若本地暂无法运行 Vivado/tsfpga，标记「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`input_ready <= input_level xor input_level_p1` 用异或，相比直接用电平值，有什么好处？
**答案**：异或检测的是「跳变」而非「绝对高低」，无论请求电平当前是 0 还是 1，只要相对上一拍变了，就被识别为一次事件——这正是两相握手对 CDC 延迟不敏感的根源。

**练习 2**：结果侧的 `not result_valid` 守卫有什么作用？
**答案**：它防止「上一笔结果还没被消费者取走」时用新数据覆盖 `result_data_int`，从而实现背压——结果占线时不会丢正在等待的数据。

**练习 3**：如果把 `input_valid` 和 `result_ready` 恒接 `'1'`，它会退化成什么？
**答案**：功能上等价于自由运行的 `resync_twophase`（见 [resync_twophase.vhd:76-82](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase.vhd#L76-L82)），但资源比专门实现的 `resync_twophase` 略高——所以无需背压时直接用 `resync_twophase` 更省。

---

### 4.4 配套约束：set_false_path / set_max_delay / set_bus_skew

#### 4.4.1 概念说明

任何 CDC 路径都横跨两个**异步**时钟，静态时序分析器（STA）无法用「单一时钟周期」去检查它——放任不管，工具要么报一堆假错误，要么把这条路径当普通路径去优化布局，反而破坏同步链。所以**每条 CDC 路径都必须显式约束**，告诉工具「按 CDC 规则处理」。项目用三种命令分工：

| 命令 | 作用 | 适用 |
| --- | --- | --- |
| `set_false_path` | 彻底切断检查，**不设延迟上限** | 找不到时钟时的兜底；延迟无所谓 |
| `set_max_delay -datapath_only` | 切断时钟关系，给数据延迟设上限（忽略两时钟偏斜） | 知道两个时钟时的首选；要确定性延迟 |
| `set_bus_skew` | 约束**总线各比特之间的相对偏斜**（非绝对延迟） | 多比特 CDC，配合格雷码 |

为什么优先 `max_delay` 而非 `false_path`？`false_path` 让延迟完全失控，信号可能慢悠悠走几十纳秒，采样时机不可预期；`max_delay` 给一个上界（取两时钟周期较小者），延迟确定、可分析。u3-l1 讲 `resync_level` 时提过「想要确定延迟就得开 `enable_input_register` 并接 `clk_in`」，根因就在这里——只有能找到 `clk_in`，约束才走 `max_delay` 分支。

#### 4.4.2 核心流程

约束脚本遵循统一的「找时钟 → 算周期 → 下约束」套路：

```text
1. get_clocks -quiet -of_objects [get_ports "clk_in"]   # 找源时钟，找不到返回空
2. 若找到 -> get_property PERIOD 取周期
   若找不到 -> 用安全默认值（2 ns，对应 500 MHz）
3. 对目的时钟同理，得到 clk_in_period、clk_out_period
4. min_period = min(两个周期)
5. 按路径类型下约束：
   - 单比特 level / 数据并行通路：set_max_delay -datapath_only -from <源> -to <目的> min_period
   - 两个时钟都找不到时：set_false_path -setup -hold -to <第一级同步寄存器>
   - 格雷码多比特：set_bus_skew -from <稳定源> -to <第一级同步寄存器> <源周期>
```

`-datapath_only` 几乎是 Xilinx CDC 约束的标配：它把时钟偏移、抖动和悲观余量从延迟计算里剔除。代价是「实际延迟可能略大于一个周期」，但两级 `async_reg` 已兜底亚稳态，这点余量损失可接受；某些派生时钟/IP 时钟场景下不加它约束会直接失败（`resync_pulse.tcl` 注释有详述）。

#### 4.4.3 源码精读

**`resync_level.tcl`** —— 本讲约束部分的锚点，逻辑只有两分支。先取两个时钟与第一级同步寄存器：

[modules/resync/scoped_constraints/resync_level.tcl:18-20](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_level.tcl#L18-L20) —— `get_clocks` 取 `clk_in/clk_out`，`get_cells data_in_p1_reg` 取同步链第一级寄存器。

两个时钟都在时，算 `min_period` 并下 `set_max_delay -datapath_only`，给出确定性延迟：

[modules/resync/scoped_constraints/resync_level.tcl:22-46](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_level.tcl#L22-L46) —— 取两时钟周期、算 `min`，`set_max_delay -datapath_only -from clk_in -to data_in_p1_reg min_period`。注释还解释了为何没用更「优雅」的 `get_timing_paths` 自动找驱动——因为在作用域脚本里该命令会报 critical warning。

找不到时钟时（如 `clk_in` 非端口时钟、或时钟尚未创建），退回 `set_false_path`：

[modules/resync/scoped_constraints/resync_level.tcl:47-54](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_level.tcl#L47-L54) —— 兜底 `set_false_path -setup -hold -to data_in_p1_reg`，并打印警告。

**`resync_counter.tcl`** —— 格雷码多比特的约束，多了 `set_bus_skew`。先取「源域稳定寄存器组」与「目的域第一级同步寄存器组」：

[modules/resync/scoped_constraints/resync_counter.tcl:18-19](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_counter.tcl#L18-L19) —— `counter_in_gray_reg*`（稳定）与 `counter_in_gray_p1_reg*`（第一级同步）。

`set_bus_skew` 限制字内各比特的相对偏斜，确保「采样时最多一个比特在翻转」：

[modules/resync/scoped_constraints/resync_counter.tcl:33-35](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_counter.tcl#L33-L35) —— `set_bus_skew -from stable_registers -to first_resync_registers clk_in_period`。

再下 `set_max_delay` 给延迟封顶，并用 `create_waiver` 压掉「多比特走 ASYNC_REG」的安全告警：

[modules/resync/scoped_constraints/resync_counter.tcl:49-64](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_counter.tcl#L49-L64) —— `set_max_delay -datapath_only ... min_period`；`create_waiver -id CDC-6` 说明「格雷码 + 正确约束下多比特同步是安全的」。

**`resync_twophase.tcl`**（`_handshake` 版除信号名外几乎逐行相同）—— 给两相实体的三类路径下约束。并行数据通路封顶 + 压掉 CDC-15 时钟使能告警：

[modules/resync/scoped_constraints/resync_twophase.tcl:41-57](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_twophase.tcl#L41-L57) —— 对 `data_in_sampled_reg* → data_out_int_reg*` 下 `set_max_delay`；`create_waiver -id CDC-15` 说明「时钟使能是本 CDC 概念的一部分，无需告警」。

两条 level 往返通路各下一条 `set_max_delay`：

[modules/resync/scoped_constraints/resync_twophase.tcl:59-66](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_twophase.tcl#L59-L66) —— `input_level_not_p1 → output_level_m1` 与 `output_level_p1 → input_level_m1` 两条单比特通路各一条 `set_max_delay`，注释说「与 `resync_level.tcl` 非常相似」。两相握手之所以**不需要** `bus_skew`，正因为不依赖「只变 1 位」，而是靠令牌保证整字稳定。

#### 4.4.4 代码实践

**目标**：对照 `resync_level.tcl`，讲清楚「为何必须对同步寄存器下 false_path / max_delay」（源码阅读型，无需运行）。

1. 读 [resync_level.tcl:18-54](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_level.tcl#L18-L54) 全文。
2. 回答：如果**不下**任何约束，`data_in_p1_reg` 这条跨域路径在 STA 里会怎样？（提示：两异步时钟没有公共周期，工具按各自周期检查，要么假失败、要么把同步链拆散重布局。）
3. 解释 `set_false_path` 与 `set_max_delay -datapath_only` 的取舍：何时用哪个？为什么后者更可取却仍要保留前者兜底？
4. 对照 [resync_counter.tcl:33-35](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_counter.tcl#L33-L35) 的 `set_bus_skew`，说明它解决的是 `resync_level.tcl` 不需考虑的「多比特一致性」问题。

**预期结果**：能得出类似结论——「CDC 路径必须脱离常规 STA 检查；知道两时钟时优先 `set_max_delay -datapath_only`（有界、确定延迟），找不到时退回 `set_false_path`（无界但至少不假错）；多比特还要 `set_bus_skew` 保证同字采样。」

#### 4.4.5 小练习与答案

**练习 1**：`set_max_delay` 为什么要带 `-datapath_only`？
**答案**：两时钟本就异步，其间偏斜无意义；`-datapath_only` 让工具只看数据路径延迟、忽略时钟偏斜，给出有物理意义的上界。两级 `async_reg` 已兜底亚稳态，故剔除偏斜余量既能让约束可解，又不影响可靠性。

**练习 2**：`resync_level.tcl` 为什么要在「找不到两个时钟」时退回 `set_false_path`？
**答案**：找不到时钟（如 `clk_in` 非端口时钟、或时钟此时尚未创建）时无法算 `min_period`、无法下 `set_max_delay`；`set_false_path` 至少把这条路径从普通 STA 中切出来，避免假错误，代价是失去延迟上界（延迟不确定）。

**练习 3**：`resync_counter.tcl` 用了 `set_bus_skew`，而 `resync_level.tcl` 没有，为什么？
**答案**：`resync_level` 只同步一个比特，没有「字内一致性」问题；`resync_counter` 同步多比特格雷字，必须限制各比特相对偏斜，才能保证采样时「最多一个比特在翻转」从而安全。

---

## 5. 综合实践

把本讲三条主线——**多比特一致性 → 两相握手实现 → CDC 约束**——串成一个完整小任务。

**场景**：一个工作在 `clk_in`（如 50 MHz）下的 16 位递增计数器，需要被 `clk_out`（如 200 MHz）域的逻辑安全读取，且要求每次读到的是一个**比特一致**的快照、绝不丢数。

**任务**：

1. **搭 testbench**：以 [tb_resync_twophase_handshake.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/test/tb_resync_twophase_handshake.vhd) 为模板，把输入侧 BFM 改成「发送一个递增的 `slv` 序列 `0,1,2,...,N-1`」（可仍借用 `stall_bfm_pkg` 施加随机 stall），结果侧核对「收到的每个值都比上一个正好大 1」。这一条比原 tb 的「随机数组比对」更严格，能直接暴露任何丢数或跳变。
2. **跑多种时钟关系**：参照 [module_resync.py:102-108](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/module_resync.py#L102-L108) 的五种快慢组合，至少跑「结果域远快」「结果域远慢」「同频」三种，确认递增关系始终成立。
3. **施加约束**：确认综合工程里通过 `read_xdc -ref resync_twophase_handshake .../resync_twophase_handshake.tcl` 加载约束（手动流程）或由 tsfpga 自动加载（见 [getting_started.rst:73-101](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/getting_started.rst#L73-L101)）。打开 `report_cdc`，确认对应路径只剩被 `create_waiver` 压掉的 CDC-15 提示，而**没有**未约束的裸 CDC 路径。
4. **对比**：把同一递增计数器改接到 `resync_twophase`（自由运行版），观察它**不等 ready** 就持续搬运的行为差异，并说明为何这种用法下无法施加背压。
5. **写一段说明**：解释如果忘记加载该 `.tcl`，`input_data_sampled_reg* → result_data_int_reg*` 这条并行数据通路会怎样（延迟无界、可能被工具重排，极端情况下采样到未稳定的数据）。

**预期结果**：递增序列在三种时钟关系下都严格 `+1` 传递；`report_cdc` 干净。若本地无 Vivado 环境，仿真部分可跑、综合/约束核对部分标记「待本地验证」。

## 6. 本讲小结

- 多比特总线跨域的核心风险不是亚稳态，而是**比特一致性**：各位到达时间不同，会让目的域采到新旧混杂的脏值。
- hdl-modules 给出两条互补解法：**格雷码**（每次只变 1 位，配 `set_bus_skew`，适合单调计数器）与**两相握手**（数据先稳定、令牌后许可，配数据通路 `set_max_delay`，适合任意相关向量）。
- `resync_twophase` 是自由运行版：一个 level 令牌在两域间往返翻转，每往返一次搬一个字，靠「令牌穿两级 `async_reg` 期间数据早已稳定」保证比特一致性；适合慢变信号，抓不住脉冲。
- `resync_twophase_handshake` 在其上加 ready/valid：`input_ready = input_level xor input_level_p1` 一行表达忙闲；结果侧采样后立即反馈（不等消费）以提升吞吐；资源多 2 LUT、2 FF。
- 每条 CDC 路径都必须配 scoped constraint 脱离常规 setup/hold 检查：能确定延迟时用 `set_max_delay -datapath_only`（有上界），找不到时钟时退化为 `set_false_path`（无上界），格雷码多比特额外用 `set_bus_skew` 限字内偏移；预期告警用 `create_waiver` 压掉。
- `dont_touch` 钉死数据寄存器、`async_reg` 把 level 同步链布局到同一 slice——属性与约束协同，才让「数据先稳定后采样」的时序前提在真实硅片上成立。

## 7. 下一步学习建议

- **向下游走**：[第 4 单元 FIFO 系列](u4-l1-synchronous-fifo.md)——异步 FIFO 内部正是用 `resync_counter` 同步格雷码读写指针，本讲的「多比特跨域 + bus_skew 约束」是其直接前置。
- **向 AXI 走**：第 5 单元的 `axi_read_cdc` / `axi_write_cdc` 会把 AXI 各通道拆成异步 FIFO 跨域，是本讲握手与 FIFO 的综合应用。
- **阅读变体**：[resync_twophase_lutram.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_twophase_lutram.vhd) 与同名 `.tcl`，看一种「用 LUTRAM 替代 FF 存数据」的面积优化变体。
- **深入约束**：[resync_pulse.tcl](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/scoped_constraints/resync_pulse.tcl) 头注释解释了「为何用这种笨办法找最小周期」「为何用 `-datapath_only`」的通用背景，本讲三份 `.tcl` 都引用了它；头注释里的 LinkedIn「Reliable CDC constraints」系列文章是最佳延伸阅读。
- **验证方法**：本讲实践用到的 `bfm.axi_stream_master/slave`，[u8-l1](u8-l1-bfm-simulation-models.md) 会系统讲解这些 BFM 如何驱动随机化握手验证。
