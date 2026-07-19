# 形式化验证（SymbiYosys）

> 适用对象：已经学过 u3-l2（时钟域与 CDC）、u4-l4（Doppler 处理）、u11-l1（FPGA 回归与 cosim）的读者。
> 本讲带你从「仿真碰运气」走向「数学证明性质恒成立」。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清**形式化验证（formal verification）**和**仿真（simulation）**的本质差别：仿真检查「我想到的那些输入组合」，形式化检查「所有可能的输入组合」。
- 读懂 AERIS-10 FPGA 仓库 `9_Firmware/9_2_FPGA/formal/` 下的 `.sby` 配置文件与 `fv_*.v` 包装模块，理解两者如何分工。
- 区分三类形式化语句：`assert`（性质必须成立）、`assume`（环境约束）、`cover`（某状态必须可达）。
- 判断一个模块「值不值得做形式化验证」，并能看懂 `fv_radar_mode_controller.v` 里 6 条性质各自证明了什么。

## 2. 前置知识

### 2.1 仿真的根本局限

你在 u11-l1 见过 `run_regression.sh`：它用 iverilog 跑 `tb_fullchain_realdata.v`，喂入真实雷达数据，逐比特比对黄金值。这是**基于激励（stimulus-based）**的验证——测试者精心挑选若干输入向量，观察输出对不对。

问题在于：一个有 \(n\) 位状态的模块，状态空间上限是 \(2^n\)。一个有 18 位 timer（`radar_mode_controller.v` 里 `reg [17:0] timer`）的 FSM，光 timer 就有 \(2^{18}=262{,}144\) 个值，再乘上模式、计数器、输入组合，可达状态数轻易上亿。仿真只能划过其中几条轨迹，像手电筒照黑洞：

\[
\text{仿真覆盖率} \;=\; \frac{\text{被测过的状态数}}{\text{可达状态总数}} \;\ll\; 1
\]

仿真回答的是「**在这些输入下没出错**」；它永远无法回答「**在任何输入下都不会出错**」。

### 2.2 形式化验证的承诺

形式化验证反过来：它**不喂输入**，而是把「存在一组输入能让性质失效」翻译成一个数学问题（可满足性 / SMT），交给求解器（solver）去找反例。

- 求解器找得到反例 → 给你一条具体的出错轨迹（CEX，counterexample），说明设计**真的有 bug**。
- 求解器在给定深度内找不到反例 → 证明在该深度内**没有任何输入**能破坏性质，即性质恒成立。

用逻辑符号写，一条性质 \(P\) 的有界模型检验（BMC, Bounded Model Checking）是：

\[
\text{BMC}(P, k):\quad \neg\,\exists\, s_0, s_1, \dots, s_k.\; \bigl(\text{初始条件}(s_0) \wedge \text{转移关系} \wedge \neg P\bigr)
\]

把前 \(k\) 拍的设计展开成一个大布尔公式，若公式不可满足（UNSAT），则前 \(k\) 拍内性质不会被违反。这正是 `smtbmc z3` 引擎在做的事——`z3` 是微软的 SMT 求解器。

### 2.3 三种形式化语句

| 语句 | 含义 | 失败时 |
|------|------|--------|
| `assert(cond)` | 设计**必须**满足 `cond` | 找到反例 → 设计有 bug（CEX） |
| `assume(cond)` | 环境**被假定**满足 `cond`（约束求解器的搜索空间） | 环境违约 → 证明不再有效 |
| `cover(cond)` | `cond` **应该可达** | 找不到见证 → 死代码/不可达状态（疑似 bug） |

一句话记忆：`assert` 约束**设计**，`assume` 约束**环境**，`cover` 检查**覆盖**。

> 工具链：本讲全部基于开源工具链 **SymbiYosys（`sby`）** + **Yosys** 综合 + **Z3** 求解。AERIS-10 选它而非商业 Formality/JasperGold，是为了让任何人都能在没有付费许可证的情况下复现验证。

## 3. 本讲源码地图

所有形式化资产都集中在 `9_Firmware/9_2_FPGA/formal/`，按「被验证模块」成对组织（一个 `.sby` 配置 + 一个 `fv_*.v` 包装）：

| 文件 | 作用 | 被验证的 DUT |
|------|------|--------------|
| `fv_radar_mode_controller.sby` / `.v` | 扫描状态机（7 状态）的性质证明 | `radar_mode_controller.v` |
| `fv_doppler_processor.sby` / `.v` | Doppler FSM + 内存地址越界证明 | `doppler_processor.v`（含 `xfft_16`/`fft_engine`） |
| `fv_range_bin_decimator.sby` / `.v` | 距离抽取 FSM（5 状态）+ 输出计数证明 | `range_bin_decimator.v` |
| `fv_cdc_single_bit.sby` / `.v` | 单比特同步器双时钟证明 | `cdc_modules.v` |
| `fv_cdc_adc.sby` / `.v` | 多比特 Gray 码 CDC 双时钟证明 | `cdc_modules.v` |
| `fv_cdc_handshake.sby` / `.v` | 握手 CDC（req/ack）活性与数据完整性 | `cdc_modules.v` |
| `fv_cdc_handshake_cover.sby` | 握手 CDC 的纯覆盖任务（只跑 cover） | `cdc_modules.v` |

命名约定：`fv_<模块名>.v` 是**包装模块（wrapper）**，它例化被验证设计（DUT, Design Under Test）、产生求解器驱动的输入、写出全部 `assert/assume/cover`。DUT 本身一字不改，只在 `ifdef FORMAL` 下额外暴露几个 `fv_*` 观测端口供 wrapper 读取内部状态。

## 4. 核心概念与源码讲解

### 4.1 `.sby` 配置文件：告诉 SymbiYosys「验证什么、怎么验证」

#### 4.1.1 概念说明

`.sby` 是 SymbiYosys 的工程描述文件，INI 风格，分若干段。它回答四个问题：

1. **跑哪些任务**（`[tasks]`）：通常 `bmc`（找 bug）和 `cover`（查可达性）。
2. **每个任务的参数**（`[options]`）：BMC 的搜索深度、cover 的深度。
3. **用什么引擎**（`[engines]`）：这里统一用 `smtbmc z3`。
4. **怎么读设计**（`[script]`）：读哪些 Verilog 文件、顶层是谁。

#### 4.1.2 核心流程

```
sby <name>.sby
   │
   ├─ [script] read_verilog -formal <DUT>.v + <wrapper>.v
   ├─ prep -top fv_<模块>            # 综合 wrapper，保留断言
   ├─ [engines] smtbmc z3             # 把电路 + 断言编码成 SMT
   │     ├─ task bmc  : 找违反 assert 的反例（深度 = bmc: depth）
   │     └─ task cover: 找满足 cover 的见证  （深度 = cover: depth）
   └─ 输出 PASS / FAIL（FAIL 附带 CEX 波形）
```

`-formal` 这个标志告诉 Yosys：识别 `assert`/`assume`/`cover` 这些 SystemVerilog 断言原语，并把它们变成求解器的约束/目标，而不是当成普通 RTL 报错。

#### 4.1.3 源码精读

先看最简单的 `fv_radar_mode_controller.sby`：

[9_Firmware/9_2_FPGA/formal/fv_radar_mode_controller.sby:L1-L22](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/formal/fv_radar_mode_controller.sby#L1-L22) —— 定义两个任务：`bmc`（深度 200）找违反断言的反例，`cover`（深度 600）证明关键状态可达。注意 cover 深度（600）远大于 bmc 深度（200），因为「跑完一次完整扫描」需要穿越很多拍，而「找一个 bug」往往在浅层就暴露。

```ini
[tasks]
bmc
cover

[options]
bmc: mode bmc
bmc: depth 200
cover: mode cover
cover: depth 600

[engines]
smtbmc z3

[script]
read_verilog -formal radar_mode_controller.v
read_verilog -formal fv_radar_mode_controller.v
prep -top fv_radar_mode_controller

[files]
../radar_mode_controller.v
fv_radar_mode_controller.v
```

不同模块的 `.sby` 在「读哪些文件」上差异最大。对比 Doppler 的配置，因为它例化了 FFT 子核，必须把整条调用链都读进来：

[9_Firmware/9_2_FPGA/formal/fv_doppler_processor.sby:L14-L27](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/formal/fv_doppler_processor.sby#L14-L27) —— 顺序读入 `doppler_processor.v` → `xfft_16.v` → `fft_engine.v` → wrapper，再把 `fft_twiddle_16.mem`/`fft_twiddle_1024.mem` 两个旋转因子 ROM 一并带上，否则综合时 FFT 蝶形核缺常数。Doppler 的深度也大得多（bmc 512 / cover 1024），因为一帧要攒够 32 个 chirp 才出结果。

CDC 类模块的 `.sby` 多一行关键的 `clk2fflogic`：

[9_Firmware/9_2_FPGA/formal/fv_cdc_handshake.sby:L14-L19](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/formal/fv_cdc_handshake.sby#L14-L19) —— `clk2fflogic` 把「时钟」显式建模成逻辑信号，让求解器能任意交织两个异步时钟的边沿，这是证明跨时钟域性质的前提。FSM 类模块用不到它，因为它们是单时钟设计。

#### 4.1.4 代码实践

1. **目标**：建立「改一个 `.sby` 就能换验证策略」的直觉。
2. **步骤**：
   - 打开 `fv_range_bin_decimator.sby`、`fv_doppler_processor.sby`、`fv_radar_mode_controller.sby` 三个文件。
   - 把它们的 `bmc: depth` 与 `cover: depth` 列成表。
3. **观察**：FSM 越复杂、一帧越长，深度越大（decimator 50 → mode_controller 200 → doppler 512）。
4. **预期**：深度与「跑完一次完整事务所需的最少时钟周期」正相关——证明一帧 Doppler 要比证明一个 decimator 多得多拍数。

#### 4.1.5 小练习与答案

**练习 1**：`fv_cdc_handshake_cover.sby` 和 `fv_cdc_handshake.sby` 内容几乎一样，唯一区别是 `[tasks]` 段少了 `bmc`。为什么要把 cover 单独拆一个配置？

**答案**：BMC 找 bug、cover 查可达性是两类目标，求解难度和时间差异巨大。把 cover 单拆，可以让你在 CI 里只跑快速的 bmc 做门禁，而把耗时的 cover 当成「定期体检」离线跑，互不拖累。

**练习 2**：如果把 `bmc: depth 200` 改成 `bmc: depth 5`，`fv_radar_mode_controller` 的 bmc 还能 PASS 吗？

**答案**：很可能 PASS，但这个 PASS 几乎没有意义——深度 5 只检查前 5 拍，连一个长 chirp（参数 `LONG_CHIRP_CYCLES=5`）都跑不完，自然「碰不到」越界。这正是 BMC 的陷阱：**浅深度的 PASS ≠ 设计正确**，深度必须覆盖到性质可能被违反的最远拍数。

---

### 4.2 性质断言：用 `assert/assume/cover` 描述「永远成立」与「必须可达」

#### 4.2.1 概念说明

性质（property）是对设计行为的**精确陈述**。在形式化里它分三类：

- **不变式（invariant）**：在每一拍都成立，写成 `assert`。例如「状态编码永远 ≤ 6」。
- **转移性质**：涉及上一拍与这一拍的关系，用 `$past(sig)` 取上一拍的值。例如「单 chirp 模式监听结束后必须回 IDLE」。
- **活性（liveness）**：某事「最终」会发生。BMC 是有界的，不能直接表达「无穷」，所以工程上把它改写成**有界活性**：「busy 信号必须在 N 拍内撤销」。

`assume` 用来裁剪求解器的搜索空间到「合法输入」，否则求解器会用现实不可能的输入（例如 `dst_ready` 永远不响应）制造假反例。

#### 4.2.2 核心流程

写一条性质的通用套路：

```
1. 写 always @(posedge clk) begin ... end              // 每拍检查
2. 用 if (reset_n) / if (dut_initialized) 守门          // 复位期不查
3. 用 $past(x) 引用上一拍                              // 转移性质
4. assert(...) 证明 / cover(...) 查可达
```

每条性质在 wrapper 里都被独立注释块标出（`PROPERTY 1`、`PROPERTY 2`…），方便审查。

#### 4.2.3 源码精读

以 `fv_radar_mode_controller.v` 为例。它先做两件准备工作：声明「上一拍有效」标记 `f_past_valid`，以及「复位在第 0 拍有效、之后释放」的 `reset_n`：

[9_Firmware/9_2_FPGA/formal/fv_radar_mode_controller.v:L49-L58](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/formal/fv_radar_mode_controller.v#L49-L58) —— `f_past_valid` 用来守护所有用到 `$past` 的性质：第 0 拍没有「上一拍」，必须跳过，否则求解器会用未定义值制造假反例。

DUT 的输入全部标成 `(* anyseq *)`，意思是「求解器每拍自由驱动这个信号到任意值」——这正是「测试所有输入组合」的实现方式：

[9_Firmware/9_2_FPGA/formal/fv_radar_mode_controller.v:L63-L67](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/formal/fv_radar_mode_controller.v#L63-L67) —— `mode`、`trigger`、三个 `stm32_new_*` 都交给求解器自由翻转，模拟「主机/STM32 可能发任何命令序列」。

接着是六条核心性质。

**性质 1：状态编码上界**——DUT 只有 7 个状态（`S_IDLE=0`…`S_ADVANCE=6`），第 7 个编码必须不可达：

[9_Firmware/9_2_FPGA/formal/fv_radar_mode_controller.v:L140-L143](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/formal/fv_radar_mode_controller.v#L140-L143) —— 证明 `scan_state` 永远 ≤ 6。这条抓的是「FSM 跑飞到未定义状态」类 bug，对应 DUT 里 [radar_mode_controller.v:L103-L109](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_mode_controller.v#L103-L109) 的 7 个 `localparam`。

**性质 2：三级计数器上界**——chirp/elevation/azimuth 三个计数器永远不能达到上限值（达到就该进位回 0）：

[9_Firmware/9_2_FPGA/formal/fv_radar_mode_controller.v:L148-L154](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/formal/fv_radar_mode_controller.v#L148-L154) —— 证明扫描位置不会越界，这是 u5-l2 讲的 `32×31×50` 三级扫描结构的数学保证。

**性质 3：timer 上界**——定时器计数到参数值就归零，永不溢出：

[9_Firmware/9_2_FPGA/formal/fv_radar_mode_controller.v:L161-L164](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/formal/fv_radar_mode_controller.v#L161-L164) —— `timer < MAX_TIMER`（这里 `MAX_TIMER=5`），抓「定时器写错比较条件导致多等一拍/少等一拍」的细 bug。

**性质 4：模式一致性**——状态与输出必须自洽（长 chirp 状态下 `use_long_chirp` 必为 1）：

[9_Firmware/9_2_FPGA/formal/fv_radar_mode_controller.v:L171-L178](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/formal/fv_radar_mode_controller.v#L171-L178) —— 抓「输出与状态不同步」，例如某次 refactor 把 `use_long_chirp` 的赋值条件改错，导致短 chirp 状态下却输出了长 chirp 标志，下游 Doppler（u4-l4）会直接算错。

**性质 5：单 chirp 模式必须回 IDLE**（转移性质，用 `$past`）：

[9_Firmware/9_2_FPGA/formal/fv_radar_mode_controller.v:L185-L192](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/formal/fv_radar_mode_controller.v#L185-L192) —— 如果上一拍是 `mode==2'b10`、状态 `S_LONG_LISTEN`、timer 到顶，这一拍状态必须回到 `S_IDLE`。这是 u5-l2 讲的「单 chirp 调试模式」收尾逻辑的硬保证。

**性质 6：自动扫描不死锁在 IDLE**（活性）：

[9_Firmware/9_2_FPGA/formal/fv_radar_mode_controller.v:L199-L205](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/formal/fv_radar_mode_controller.v#L199-L205) —— 在自动扫描模式（`mode==2'b01`）下，FSM 不能连续两拍停在 `S_IDLE`。这条最值钱：它证明「上电默认模式不会卡死」，是板级 bring-up（u10-l2）能跑起来的前提。

最后两条是 `cover`——证明「好状态」真的可达，防止断言写在一个永远到不了的状态上（那样的 assert 永远空转 PASS）：

[9_Firmware/9_2_FPGA/formal/fv_radar_mode_controller.v:L210-L228](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/formal/fv_radar_mode_controller.v#L210-L228) —— `cover(scan_complete && mode==2'b01)` 证明完整扫描能跑完；逐状态 `cover` 证明 7 个状态全部可达（没有死状态）。

#### 4.2.4 代码实践

1. **目标**：体会 `assume` 与 `assert` 的不对称性。
2. **步骤**：打开 `fv_doppler_processor.v`，找到输入假设段。
3. **观察**：[fv_doppler_processor.v:L129-L140](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/formal/fv_doppler_processor.v#L129-L140) 用 `assume` 告诉求解器「缓冲区满时别再送数据」「FFT 处理态时别发 new_chirp_frame」——这些是上游真实会遵守的协议，若不 assume，求解器会用违约输入制造假 bug。
4. **预期**：如果把这两条 `assume` 改成 `assert`，求解器大概率会立刻给出反例（因为它能自由驱动输入违约）。这能帮你分清「设计义务」与「环境义务」。

#### 4.2.5 小练习与答案

**练习 1**：性质 5 用了 `$past(mode) == 2'b10 && mode == 2'b10`，为什么不能只写 `mode == 2'b10`？

**答案**：转移条件要求「上一拍和这一拍都是单 chirp 模式」。如果只查当前拍，求解器可以构造「上一拍是别的模式、这一拍才切到 2'b10」的轨迹，此时状态机还没机会走完 `S_LONG_LISTEN`，断言会误报。两个条件合起来锁死了「模式保持稳定」这个隐含前提。

**练习 2**：`cover(scan_state == S_ADVANCE)` 如果验证结果报告「未覆盖（uncovered）」，意味着什么？

**答案**：意味着 `S_ADVANCE` 在求解器探索的 600 拍内从未被到达——它可能是死状态（FSM 永远不会跳进这个分支），也可能是深度不够（需要更长的合法激励序列才能到达）。两种情况都值得排查：前者是设计冗余/bug，后者要加大 cover 深度。

**练习 3**：为什么所有性质都包在 `if (reset_n)` 里？

**答案**：复位期间寄存器值未定义/正在被强清零，此时检查性质没有意义，求解器还会用「复位未完成」制造假反例。`if (reset_n)` 把检查限定在「复位已释放、设计开始正常工作」之后。

---

### 4.3 适用模块与 `fv_` 包装层：哪些模块最该做形式化验证

#### 4.3.1 概念说明

形式化验证不是万能药——求解器对**状态空间大小**极度敏感，模块越大、状态位越多，求解越慢甚至不终止。所以工程上要挑「**高价值、小状态**」的模块：

1. **跨时钟域（CDC）模块**：仿真很难穷尽两个异步时钟的所有边沿交织，而 CDC 错误（亚稳态、数据撕裂）一旦流片几乎无法现场调试——最该做形式化。
2. **控制 FSM**：状态数少（几十个），但转移条件复杂、容易漏写某个分支——形式化能证明「无死锁、无越界、无非法状态」。
3. **地址生成器**：内存地址越界会写崩 BRAM，一条 `assert(addr < DEPTH)` 就能锁死。

反之，**数据通路**（FFT 蝶形、滤波器系数计算）通常用 cosim 做数值精确比对（见 u11-l1），因为它们状态少但数值空间大，形式化不好表达「数值近似正确」。

#### 4.3.2 核心流程：`fv_` 包装层的设计套路

```
module fv_<模块>(input clk);          // 单时钟：clk 是端口
// 或 module fv_<模块>;                 // 多时钟：无端口，内部自造时钟
  ── 把参数改小（localparam LONG=5）    // 让 BMC 可解
  ── 例化 DUT，把 cfg_* 绑到小常数
  ── DUT 在 `ifdef FORMAL` 下暴露 fv_* 观测端口
  ── 写 assert / assume / cover
endmodule
```

两个关键技巧：

- **参数缩小（parameter reduction）**：把 `LONG_CHIRP_CYCLES` 从真实值缩到 5，把 `RANGE_BINS` 从 64 缩到 2，把 CDC `WIDTH` 从 32 缩到 8。性质是**结构性的**（不依赖具体数值大小），小参数下证明成立，等价于大参数下成立，但求解时间从「不终止」变成「几秒」。
- **`fv_*` 观测端口**：DUT 用 `` `ifdef FORMAL `` 条件编译，额外输出内部寄存器（如 `fv_scan_state`、`fv_mem_read_addr`），让 wrapper 不必穿透层次就能读到内部状态。综合成真实比特流时这段代码被宏屏蔽，零开销。

#### 4.3.3 源码精读

DUT 侧的 `fv_*` 端口——以 `radar_mode_controller.v` 为例：

[9_Firmware/9_2_FPGA/radar_mode_controller.v:L90-L94](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_mode_controller.v#L90-L94) —— `fv_scan_state`/`fv_timer` 是只在 `FORMAL` 宏下才存在的额外输出端口，把内部 `scan_state` 与 `timer` 直接接到顶层给 wrapper 检查。

wrapper 侧的参数缩小——把真实扫描规模（32×31×50）缩到玩具规模（3×2×2）：

[9_Firmware/9_2_FPGA/formal/fv_radar_mode_controller.v:L17-L28](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/formal/fv_radar_mode_controller.v#L17-L28) —— 注意注释「Reduced parameters for tractable BMC」：定时器全压到 3~5 拍，扫描规模压到 3 chirp × 2 仰角 × 2 方位。这足以遍历所有状态转移，又让求解器秒级完成。运行时可配置的 `cfg_*` 端口也被钉死在这些小常数上：

[9_Firmware/9_2_FPGA/formal/fv_radar_mode_controller.v:L72-L77](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/formal/fv_radar_mode_controller.v#L72-L77) —— 注释明说「证明一个可解的配置，不遍历 cfg_* 全空间」。这是诚实的工程取舍：与其追求证明所有运行时配置（指数爆炸），不如先证明一个代表性配置的性质。

最有价值的「地址越界」性质在 Doppler wrapper 里，它直接对准一个历史上的高危 bug：

[9_Firmware/9_2_FPGA/formal/fv_doppler_processor.v:L157-L163](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/formal/fv_doppler_processor.v#L157-L163) —— `assert(mem_read_addr < MEM_DEPTH)`，注释点名「KEY BUG TARGET」：`fft_sample_counter + 2` 截断会导致 `read_doppler_index` 溢出，进而算出越界的 `mem_read_addr`（对应 DUT 里 [doppler_processor.v:L154](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/doppler_processor.v#L154) 的 `mem_read_addr = read_doppler_index * RANGE_BINS + read_range_bin`）。仿真要撞到这个 bug 需要恰好的计数器值，形式化则一次性证明它在所有轨迹下都不越界。

CDC 握手模块则展示了「活性」证明——这是仿真的盲区。`fv_cdc_handshake.v` 证明 `src_busy` 必须在 100 个 `gclk` 内撤销：

[9_Firmware/9_2_FPGA/formal/fv_cdc_handshake.v:L358-L371](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/formal/fv_cdc_handshake.v#L358-L371) —— 先 `assume` 消费端会在 8 个 `dst_clk` 内响应（环境活性假设），再 `assert` busy 100 拍内清零（设计活性保证）。这证明握手 CDC「不会永久卡死」，是 u3-l2 讲的握手 CDC 安全性闭环的数学封口。

#### 4.3.4 代码实践

1. **目标**：对比「FSM 类 wrapper」与「CDC 类 wrapper」的时钟建模差异。
2. **步骤**：
   - 打开 `fv_radar_mode_controller.v`（FSM 类）与 `fv_cdc_single_bit.v`（CDC 类）的模块声明与时钟段。
   - FSM 类：模块有 `input wire clk` 端口，`smtbmc` 自动驱动它（见 `.sby` 没有 `clk2fflogic`）。
   - CDC 类：模块**无端口**，内部用 `(* gclk *) reg formal_clk;` 声明全局形式时钟，再用 `$anyseq` 自由翻转两个域时钟，对应 `.sby` 里的 `clk2fflogic`。
3. **观察**：[fv_cdc_single_bit.v:L20-L40](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/formal/fv_cdc_single_bit.v#L20-L40) 里 `src_clk_en`/`dst_clk_en` 都赋成 `$anyseq`，让两个时钟的边沿由求解器任意交织——这才是真正「异步」的模型。
4. **预期**：FSM 单时钟证明相对快（秒级），CDC 多时钟证明慢得多（分钟级），因为求解器要枚举所有边沿交错。

#### 4.3.5 小练习与答案

**练习 1**：`range_bin_decimator` 的 wrapper 把 `decimation_mode` 标成 `(* anyconst *)` 而不是 `(* anyseq *)`，二者有何区别？

**答案**：`anyseq` 每拍可以变（求解器自由驱动时序信号），`anyconst` 整条轨迹保持不变（求解器选一个值后固定）。这里 `decimation_mode` 在一次扫描中是固定的运行时配置，用 `anyconst` 更贴近真实使用，且让求解器对「每个固定模式」分别证明，比每拍乱变更有意义。见 [fv_range_bin_decimator.v:L54-L55](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/formal/fv_range_bin_decimator.v#L54-L55)。

**练习 2**：为什么 Doppler wrapper 把 `RANGE_BINS` 缩到 2，却不缩 `DOPPLER_FFT_SIZE`（仍是 16）？

**答案**：因为 wrapper 要真实例化 `xfft_16`/`fft_engine`，FFT 点数是 IP 的固定接口（16 点旋转因子 ROM），改不了；而 `RANGE_BINS` 只是内存深度参数，缩小它能让 `MEM_DEPTH` 变小、地址空间收缩，加速求解，又不影响「地址是否越界」这类结构性性质的成立。见 [fv_doppler_processor.v:L22-L26](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/formal/fv_doppler_processor.v#L22-L26)。

**练习 3**：数据通路的 FFT 蝶形核为什么没单独写 `fv_fft_engine.sby`？

**答案**：蝶形核是纯数值计算，状态少但数值空间巨大，形式化不好表达「浮点/定点结果近似正确」这种性质。它由 u11-l1 的真实数据 cosim（`tb_fullchain_realdata.v` 做 exact-match）覆盖更合适。形式化只盯着它的「地址/状态」侧面（在 `fv_doppler_processor` 里一起证），这是分工。

## 5. 综合实践

> **任务**：把 `fv_radar_mode_controller.v` 当成一份「设计合同」来审计，并说清形式化如何补 `run_regression.sh` 的盲区。

### 步骤 1：列出它证明的全部性质

通读 [fv_radar_mode_controller.v](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/formal/fv_radar_mode_controller.v)，按下面表格填写（答案已在 4.2.3 给出，请自己先填再核对）：

| 编号 | 类型 | 性质 | 抓什么 bug |
|------|------|------|-----------|
| P1 | 不变式 | `scan_state <= 6` | FSM 跑飞到未定义状态 |
| P2 | 不变式 | 三级计数器各自 `< 上限` | 扫描位置越界 |
| P3 | 不变式 | `timer < MAX_TIMER` | 定时器比较条件写错 |
| P4 | 不变式 | 长短 chirp 状态与 `use_long_chirp` 自洽 | 输出与状态不同步 |
| P5 | 转移 | 单 chirp 监听结束 → 回 IDLE | 调试模式收尾逻辑漏 |
| P6 | 活性 | 自动模式不连续两拍停 IDLE | 上电死锁 |
| C1 | cover | 完整扫描能跑完 | 死代码/深度不足 |
| C2 | cover | 7 个状态全部可达 | 死状态 |

### 步骤 2：说明形式化如何补充 `run_regression.sh`

关键事实先确认：**`run_regression.sh` 并不调用形式化验证**——通读该脚本，里面只有 iverilog 仿真（Phase 0 lint + 单元/集成/信号处理四个阶段），没有 `sby` 命令。`.sby` 是**独立的一层**验证，需要单独安装 SymbiYosys 并手动 `sby formal/fv_xxx.sby` 运行。

两者互补关系如下：

| 维度 | `run_regression.sh`（仿真） | `formal/*.sby`（形式化） |
|------|------------------------------|--------------------------|
| 策略 | 喂特定激励（含真实雷达数据） | 不喂激励，求解器自由驱动 |
| 覆盖 | 沿几条轨迹采样 | 证明前 \(k\) 拍内**所有**输入 |
| 擅长 | 数值精确性（exact-match 黄金值） | 控制正确性（无死锁/越界/非法状态） |
| 弱点 | 状态空间覆盖率 \(\ll 1\) | 不能证数值近似、深度受限 |
| 运行 | CI 内自动跑 | 手动跑、耗时较长 |

具体到 `radar_mode_controller`：仿真能验证「喂入一组正常扫描序列时输出对」，但很难撞到「`mode` 在某拍恰好从 `2'b01` 切到 `2'b10`、同时 timer 到顶」这种边角——性质 P5/P6 恰好覆盖这类仿真盲区。反过来，形式化证明不了「距离像数值算得对」（那是 cosim 的活）。**两者叠加才接近「既算得对又不卡死」**。

### 步骤 3（可选·待本地验证）：实际跑一次

如果你的机器装了 `yosys`、`z3`、`symbiyosys`：

```bash
cd 9_Firmware/9_2_FPGA/formal
sby -f fv_radar_mode_controller.sby
```

预期看到 `PASS`（bmc 与 cover 两个任务各一行），并在生成的 `fv_radar_mode_controller/` 目录下看到求解日志与（若有失败）CEX 波形。若没装工具链，本步骤标注为「待本地验证」，不影响前两步的阅读收获。

## 6. 本讲小结

- **形式化验证用数学证明代替采样**：仿真检查「想到的输入」，形式化检查「所有输入」（前 \(k\) 拍内），二者是互补关系而非替代。
- **`.sby` 是 SymbiYosys 工程文件**：四段（`[tasks]`/`[options]`/`[engines]`/`[script]`）定义「跑 bmc+cover、深度多少、用 smtbmc z3、读哪些文件」。CDC 类多一行 `clk2fflogic` 来建模异步时钟。
- **三类语句分工**：`assert` 约束设计、`assume` 约束环境、`cover` 查可达性；活性用「有界 N 拍内必须发生」表达。
- **`fv_` wrapper 的两大技巧**：参数缩小（小到求解器秒级完成）+ `fv_*` 观测端口（DUT 在 `ifdef FORMAL` 下暴露内部状态，零综合开销）。
- **该做形式化的模块**：CDC（仿真难穷尽异步边沿）、控制 FSM（无死锁/越界）、地址生成器（防 BRAM 越界）；数据通路的数值正确性留给 cosim。
- **与回归的关系**：`run_regression.sh` 不调用形式化，`.sby` 是独立一层；仿真管「数值对」，形式化管「控制稳」。

## 7. 下一步学习建议

- **动手扩展一条性质**：仿照 `fv_radar_mode_controller.v` 的 PROPERTY 6（活性），给 `fv_range_bin_decimator.v` 加一条「`ST_PROCESS` 不会连续超过 N 拍」的有界活性断言，跑 `sby` 看是否 PASS。
- **对照 u11-l1 的 cosim**：重读 `tb_fullchain_realdata.v` 的 exact-match 比对，体会「数值正确性（cosim）+ 控制正确性（formal）」的双保险设计。
- **进入 u14-l2（二次开发扩展点）**：当你新增一个 opcode 或一个 FSM 分支时，形式化验证是「保证你没引入死锁/越界」的最后一道关——下一讲会讲如何把新模块接入这套验证体系。
- **进阶阅读**：SymbiYosys 官方文档的 smtbmc 章节、Claire Wolf 的 Yosys 形式化教程，理解 `clk2fflogic`、`async2sync`、`chformal` 等综合 pass 的内部原理。
