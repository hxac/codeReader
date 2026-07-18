# FSM 提取优化、线程模型与测试体系

## 1. 本讲目标

本讲是「高级内部机制」的收尾篇，把三个相对独立但都属于「工程化」的主题放在一起讲。学完后你应该能够：

- 说清楚 Yosys 的 `fsm` 命令把一段行为级状态机从门级网表中「提取→优化→重编码→映射回逻辑」的完整流程，以及它为什么这样做。
- 理解 `kernel/threading` 提供的并行原语（线程池、分片容器、屏障同步），知道 `Multithreading` 守卫对象约束了什么，并能解释 `YOSYS_DISABLE_THREADS` / `YOSYS_ENABLE_THREADS` 这对开关。
- 读懂 `tests/` 目录的分层结构，能分别运行单元测试（GoogleTest）与功能测试（Makefile 自测框架），并知道 `test-unit` 这个构建目标需要什么前提。

这三块共同回答一个问题：一个 RTL 综合框架在「算法正确」之外，还要做哪些工程上的支撑工作（状态机抽象、并行加速、回归测试）。

## 2. 前置知识

本讲假定你已经掌握以下内容（对应前置讲义）：

- **RTLIL 的基本数据结构**（u2、u3）：Design / Module / Wire / Cell / SigSpec，以及 `$and`、`$dff`、`$mux`、`$pmux` 等内部单元。FSM 提取的输入就是一个「全是 `$dff` + `$mux` 树」的门级网表。
- **Pass 的注册与调度**（u4-l1、u4-l2）：`Pass::call` 如何按命令名查 `pass_register` 并执行；编排型 Pass（如 `synth`、`opt`）如何用 `Pass::call(design, "子命令")` 串联子 pass。本讲的 `fsm` 就是又一条编排型 Pass。
- **proc 与 opt**（u6-l2、u6-l3）：`always` 块先被 `proc` 翻译成 `$mux`/`$dff`/`$pmux`，再被 `opt` 清理。FSM 提取就发生在这之后的门级网表上。
- **CMake 的 condition 模式**（u1-l2）：用户层 `YOSYS_WITHOUT_*` / `YOSYS_WITH_*` 选项与依赖探测结果，经 `condition()` 宏合并成 `YOSYS_ENABLE_*` 编译宏。线程开关用的正是这套机制。

几个本讲会用到的术语先约定清楚：

- **FSM（有限状态机）**：由若干「状态」、一组「状态迁移」、一组输入输出构成。在硬件里通常实现为「状态寄存器 + 次态逻辑 + 输出逻辑」。
- **状态编码（state encoding）**：给每个状态分配一个二进制位串。常见两种：二进制编码（binary，N 个状态只需 \( \lceil \log_2 N \rceil \) 位）和独热编码（one-hot，N 个状态用 N 位，每个状态只有一位置 1）。
- **屏障同步（barrier synchronization）**：所有工作线程都跑到同一个汇合点后才一起继续，是 fork-join 式并行的基础。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `passes/fsm/fsm.cc` | 编排型 Pass `fsm`，按固定顺序串联各 `fsm_*` 子 pass |
| `passes/fsm/fsm_detect.cc` | 扫描网表，识别「像状态寄存器」的线并打 `fsm_encoding` 属性 |
| `passes/fsm/fsm_extract.cc` | 把被标记的状态寄存器及其驱动逻辑「符号仿真」成一个 `$fsm` 单元 |
| `passes/fsm/fsmdata.h` | `FsmData` 结构：状态表、迁移表，以及与 `$fsm` 单元参数互转的 `copy_to_cell` / `copy_from_cell` |
| `passes/fsm/fsm_opt.cc` | 优化 `$fsm` 单元本身：删不可达状态、合并等价输入、找 don't-care |
| `passes/fsm/fsm_recode.cc` | 重新分配状态编码（one-hot / binary） |
| `passes/fsm/fsm_map.cc` | 把 `$fsm` 单元映射回 `$dff` + `$pmux` + 比较逻辑 |
| `kernel/threading.h` | 并行原语：`Mutex`/`CondVar`、`ConcurrentQueue`、`ThreadPool`、`ParallelDispatchThreadPool`、`ShardedVector`、`MonotonicFlag` |
| `kernel/threading.cc` | 线程池的运行时实现与线程数计算 |
| `kernel/yosys_common.h` / `kernel/yosys.cc` | `Multithreading` 守卫对象：标记「当前是否有线程在并发访问 RTLIL」 |
| `passes/opt/opt_merge.cc` | 一个真实使用线程池的 Pass（按单元哈希合并相同 cell），作为 threading 的实战范例 |
| `tests/Makefile` | 功能测试的顶层入口，枚举各测试子目录 |
| `tests/common.mk` | 功能测试的公共规则（`vanilla-test` / `functional`） |
| `tests/unit/CMakeLists.txt` | 单元测试的 CMake 框架，定义 `yosys_gtest()` |
| `tests/unit/kernel/threadingTest.cc` | threading 的 GoogleTest 用例 |
| `tests/unit/kernel/bitpatternTest.cc` | 一个典型单元测试样本 |
| `tests/simple/always01.v` | 最简单的功能测试设计（一个带同步复位的计数器） |
| `tests/fsm/generate_mk.py` | 随机生成大量 FSM 测试用例的脚本 |

## 4. 核心概念与源码讲解

### 4.1 FSM 流程：从门级网表中识别并重写状态机

#### 4.1.1 概念说明

很多 RTL 设计里都藏着状态机：一段 `always @(posedge clk)` 里用一个 `case (state)` 描述状态转移。经过 `proc` 之后，这段行为级代码会被翻译成一组 `$dff`（状态寄存器）加上一棵由 `$mux`/`$pmux` 组成的「次态选择树」。此时 Yosys 并不知道这是状态机——它眼里只有门。

`fsm` 命令做的事情，就是**把这些门重新「识别」成一个抽象的状态机对象**（一个 `$fsm` 单元），在抽象层面做优化（删掉不可达状态、合并冗余迁移、重新选择更省的编码），然后再把它**映射回门**。这样做的好处是：

- 抽象层面的优化比在门层面做更高效（例如把 16 个状态重新编码成 4 位二进制，省寄存器；或反过来用独热编码省译码逻辑）。
- 可以做「符号仿真」：不需要真正跑时钟，而是用 `ConstEval` 枚举所有控制输入组合，求出每个状态在每种输入下的次态。

需要特别说明：**`fsm` 不一定让电路更小**。状态机重编码可能破坏用户的时序假设或输出摩尔/米利结构。因此 Yosys 在检测阶段非常保守，只对「确实像状态机、且重编码大概率有益」的寄存器下手。

#### 4.1.2 核心流程

`fsm` 是一条编排型 Pass（和 `synth`、`opt` 同类），自身不实现算法，只按固定顺序调用一串子 pass。流程如下：

```
fsm_detect        # 可选：扫描网表，给像状态寄存器的线打 fsm_encoding 属性
fsm_extract       # 把被标记的寄存器及其驱动树提取成 $fsm 单元
fsm_opt           # 优化 $fsm 单元（删不可达状态等）
opt_clean         # 清理因提取而悬空的线/单元
fsm_opt           # 再优化一轮
[fsm_expand ...]  # 可选：尝试扩大 FSM 范围
fsm_recode        # 重新分配状态编码（one-hot / binary）
fsm_info          # 打印 FSM 信息
fsm_map           # 把 $fsm 单元映射回 $dff + $pmux + 逻辑
```

其中最关键的是三个阶段：

1. **检测（detect）**：决定哪些线是状态寄存器。判定标准是「驱动它的 D 端是不是一棵纯 `$mux`/`$pmux` 树」（次态选择树），且它的使用者「看起来像会从重编码中受益」。
2. **提取（extract）**：对每个被标记的寄存器，建一棵 `FsmData`（状态表 + 迁移表），方法是「以每个已知状态为初值，用 `ConstEval` 符号仿真所有控制输入组合，记录次态」。
3. **映射（map）**：把优化、重编码后的 `FsmData` 重新物化成状态寄存器（`$dff`/`$adff`）、状态译码逻辑（`$eq` 或独热直连）、次态选择（`$pmux`）和输出逻辑。

`fsm_extract` 顶部还引用了一篇 ISCAS 2010 论文，说明这套「从扁平门级网表提取 FSM」的方法有学术出处。

#### 4.1.3 源码精读

**编排型 Pass `fsm` 的执行顺序**

`fsm.cc` 的 `help()` 直接把顺序写明了——这正是 ScriptPass/编排型 Pass 的优良传统，help 文档与实际执行永远一致：

[fsm.cc:38-55](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/fsm/fsm.cc#L38-L55) 列出了完整的子 pass 顺序。

`execute()` 里用一连串 `Pass::call(design, "...")` 严格按此顺序调用子 pass（注意 `Pass::call` 就是 u4-l1 讲过的命令派发入口）：

[fsm.cc:138-160](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/fsm/fsm.cc#L138-L160) — 依次调用 `fsm_detect` → `fsm_extract` → `fsm_opt`/`opt_clean`/`fsm_opt` → （可选）`fsm_expand` → `fsm_recode` → `fsm_info` → （可选）`fsm_export` → `fsm_map`。

help 里还有一条对实战很重要的提示——FSM 检测只接受一小部分触发器类型，要先做准备：

[fsm.cc:70-71](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/fsm/fsm.cc#L70-L71) 提示在跑 `fsm` 前先执行 `opt -nosdff -nodffe`，把 `$sdff`/`$dffe` 退回成基础 `$dff`，否则检测会漏掉状态机。

**检测：什么样的线算状态寄存器**

`fsm_detect.cc` 的核心是 `detect_fsm()`。它先收集每个被 `$dff`/`$adff` 的 Q 端驱动的线作为候选，然后调用 `check_state_mux_tree()` 判断 D 端是不是一棵纯 mux 树：

[fsm_detect.cc:37-89](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/fsm/fsm_detect.cc#L37-L89) 递归遍历驱动树：只要遇到非 `$mux`/`$pmux` 的驱动、或线接到模块端口、或出现组合环路，就判定「不像状态机」（返回 false）；否则把沿途的 mux 记进 `muxtree_cells`。

当所有「利好」条件都满足（像状态寄存器、使用者看起来受益、没有 init 值、不是模块端口、不是自复位）时，才给线打上 `fsm_encoding = "auto"` 属性，这正是后续 `fsm_extract` 的触发条件：

[fsm_detect.cc:240-245](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/fsm/fsm_detect.cc#L240-L245) — 满足全部条件时打 `fsm_encoding = "auto"`。

用户也可以自己在 RTL 里用 `(* fsm_encoding = "one-hot" *)` 之类的属性强制标注，或用 `"none"` 禁止检测。

**提取：符号仿真构造迁移表**

`fsm_extract.cc` 是整套机制的核心。它先为每个模块建立 `SigSet`（u3-l2 讲过的「信号→驱动者」反查表）`sig2driver`/`sig2trigger`，然后对每根带 `fsm_encoding` 属性的线调用 `extract_fsm()`。

`extract_fsm()` 第一步是找到驱动该线的状态寄存器：

[fsm_extract.cc:271-289](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/fsm/fsm_extract.cc#L271-L289) — 在 `sig2driver` 里找到驱动状态线的 `$dff`/`$adff`，读取它的时钟、（异步）复位值与 D 端信号 `dff_in`（这就是次态选择树的根）。

然后调用 `find_states()` 沿 mux 树递归找出所有状态码与控制输入位：

[fsm_extract.cc:41-124](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/fsm/fsm_extract.cc#L41-L124) — 沿 `$mux`/`$pmux` 的 A/B 分支递归：叶子的常数就是一个状态码，选择端 S 的每一位是一个控制输入。若树不闭合（出现多重驱动或非 mux 驱动）则提取失败。状态数 ≤ 1 也判失败（`fsm_extract.cc:309-312`，至少要两个状态才有意义）。

迁移表则由 `find_transitions()` 用 `ConstEval`（模块级符号求值器，见 u9-l2）枚举：以某状态为初值，把所有控制输入设为「停止信号」，对无法直接求值的位逐一尝试 `S0`/`S1`，递归覆盖所有组合，记录每次的次态：

[fsm_extract.cc:204-253](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/fsm/fsm_extract.cc#L204-L253) — 对每个未定义的控制位，分别置 0 和置 1 两条分支递归（这是经典的符号仿真二叉展开）。

最后把 `FsmData` 装进一个新建的 `$fsm` 单元，并改名原始状态线：

[fsm_extract.cc:371-394](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/fsm/fsm_extract.cc#L371-L394) — `addCell("$fsm$...")` 创建 `$fsm` 单元，`copy_to_cell` 把状态表/迁移表写进参数，原始状态线改名为 `$fsm$oldstate...` 以便后续 `opt_clean` 清理。

顶部引用的论文（提示这套方法的学术来源）：

[fsm_extract.cc:20-23](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/fsm/fsm_extract.cc#L20-L23)。

**`$fsm` 单元的内存表示：`FsmData`**

`fsmdata.h` 定义了 FSM 的抽象数据结构，核心是状态表与迁移表：

[fsmdata.h:27-32](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/fsm/fsmdata.h#L27-L32) — `num_inputs/num_outputs/state_bits/reset_state` 加 `transition_table`（每条迁移：`state_in, ctrl_in → state_out, ctrl_out`）和 `state_table`（每个状态的状态码）。

`$fsm` 单元本身不持有「字段」，一切信息都打包进它的 `parameters`。`copy_to_cell` / `copy_from_cell` 负责 `FsmData` ↔ 单元参数的互转——它们把整张状态表压平成一个大 `RTLIL::Const`（`STATE_TABLE`），把整张迁移表压平成另一个大 `RTLIL::Const`（`TRANS_TABLE`）：

[fsmdata.h:34-69](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/fsm/fsmdata.h#L34-L69) — `copy_to_cell` 写入 `STATE_BITS`、`STATE_NUM`、`STATE_NUM_LOG2`、`STATE_TABLE`、`TRANS_NUM`、`TRANS_TABLE` 等参数；其中 `state_num_log2` 由「右移到 0 的次数」算出，即 \( \lceil \log_2(\text{状态数}) \rceil \)。

[fsmdata.h:71-116](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/fsm/fsmdata.h#L71-L116) — `copy_from_cell` 把压平的常量按定长切片还原成状态码与迁移。

**优化：`fsm_opt` 在抽象层做的清理**

`fsm_opt.cc` 的 `FsmOpt` 构造函数依次调用一串优化，每一条都对应一种典型的「状态机里能省的东西」：

[fsm_opt.cc:297-316](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/fsm/fsm_opt.cc#L297-L316) — 调用顺序：`opt_unreachable_states` → `opt_unused_outputs` → `opt_alias_inputs` → `opt_feedback_inputs` → `opt_find_dont_care` → `opt_const_and_unused_inputs`，最后 `copy_to_cell` 写回。

其中两个最有代表性：

- `opt_unreachable_states` 做不可达状态消除——除了复位态外，先把所有状态标为「不可达」，再用迁移表把「有迁移进入的状态」救回来：

  [fsm_opt.cc:37-78](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/fsm/fsm_opt.cc#L37-L78) — 典型的不动点循环，删掉不可达状态后还要重映射剩余状态的编号。

- `opt_find_dont_care` 把「仅在某一输入位不同、且输出相同」的成对迁移合并成一个 don't-care（用 `Sa` 状态表示），等价于卡诺图化简：

  [fsm_opt.cc:265-295](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/fsm/fsm_opt.cc#L265-L295) — 按 `(state_in, state_out, ctrl_out)` 分组，对每组在输入位上反复尝试合并（`opt_find_dont_care_worker`，把成对的 `0/1` 改成 `Sa`）。

注意 `optimize_fsm` 是定义在 `FsmData` 上的静态方法，但实现在 `fsm_opt.cc` 里（声明见 `fsmdata.h:158`），是「头文件声明 + .cc 实现」的跨文件拆分。

**重编码：`fsm_recode` 重新分配状态码**

`fsm_recode.cc` 支持两种编码，并对 `auto` 给出一条经验阈值：

[fsm_recode.cc:77-94](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/fsm/fsm_recode.cc#L77-L94) — `auto` 在状态数 < 32 时选独热编码，否则选二进制编码；二进制编码所需的位数为 `ceil_log2(状态数)`。

随后逐个状态重新生成新码并替换：

[fsm_recode.cc:101-120](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/fsm/fsm_recode.cc#L101-L120) — 复位态强制编号为 0；one-hot 用一个 `S1` 加其余 `Sa`，binary 用整数 `state_idx`。

`-encfile` 还能把「旧码 → 新码」的映射写出来，方便与外部形式验证工具对齐。

**映射：`fsm_map` 把抽象状态机变回门**

`fsm_map.cc` 的 `map_fsm()` 把优化后的 `FsmData` 物化为三部分逻辑：

[fsm_map.cc:169-188](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/fsm/fsm_map.cc#L169-L188) — 重建状态寄存器：根据是否有异步复位选 `$dff` 或 `$adff`。

[fsm_map.cc:196-228](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/fsm/fsm_map.cc#L196-L228) — 状态译码：对每个状态用 `$eq` 比较，得到独热向量 `state_onehot`；如果某状态码本身就是独热形式，直接连线并标记 `encoding_is_onehot`，最终给状态线打 `onehot` 属性。

[fsm_map.cc:240-296](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/fsm/fsm_map.cc#L240-L296) — 次态生成：对每个目标状态，收集「能迁移到它」的 `(控制输入模式, 源状态)`，用 `implement_pattern_cache` 生成 `$eq`+`$and`+`$reduce_or` 的匹配逻辑；独热编码走位拼接，非独热走一个大的 `$pmux`。

最后删除原 `$fsm` 单元（`fsm_map.cc:320`）。至此 FSM 完成了一个完整的「门 → 抽象状态机 → 优化重编码 → 门」的往返。

#### 4.1.4 代码实践

**目标**：亲手观察 `fsm` 命令如何把一个 4 状态状态机提取、重编码再映射回门，对比独热与二进制编码的差异。

**步骤**：

1. 准备一个最小状态机设计，保存为 `fsm_demo.v`（示例代码，非项目原有文件）：

```verilog
module fsm_demo(input clk, input rst, input start, output reg done);
    reg [1:0] state;
    localparam S0 = 2'd0, S1 = 2'd1, S2 = 2'd2, S3 = 2'd3;
    always @(posedge clk) begin
        if (rst) state <= S0;
        else case (state)
            S0: state <= start ? S1 : S0;
            S1: state <= S2;
            S2: state <= S3;
            S3: state <= S0;
            default: state <= S0;
        endcase
    end
    always @(*) done = (state == S3);
endmodule
```

2. 写一个脚本 `run_fsm.ys`，先只做基础综合（不重编码），观察提取出的 `$fsm`：

```
read_verilog fsm_demo.v
hierarchy -top fsm_demo
proc
opt -nosdff -nodffe
fsm -norecode        ; 故意跳过 recode，保留原始 2 位二进制编码
write_rtlil fsm_before.il
```

3. 跑脚本并查看 `fsm_before.il`：应该能看到一个 `cell $fsm$...` 单元，其 `STATE_TABLE` 参数记录了 4 个状态码（`00/01/10/11`），`TRANS_TABLE` 记录了迁移。

4. 改成完整 `fsm`（含 recode），默认 `auto` 会对 4 个状态选独热编码（< 32），状态线应变成 4 位、被打上 `onehot` 属性：

```
read_verilog fsm_demo.v
hierarchy -top fsm_demo
proc
opt -nosdff -nodffe
fsm
write_rtlil fsm_after.il
```

5. 想强制二进制编码，加 `-encoding binary`：`fsm -encoding binary`。

**需要观察的现象**：

- `fsm -norecode` 后存在 `$fsm` 单元；完整 `fsm` 后 `$fsm` 单元**消失**（被 `fsm_map` 拆回 `$dff`/`$pmux`/`$eq`）。
- 独热编码下，状态寄存器位宽从 2 变成 4，且状态线带 `onehot` 属性。
- 二进制编码下，状态寄存器仍是 2 位（`fsm_recode` 检测到已是紧致二进制会直接返回，见 `fsm_recode.cc:90-93`）。

**预期结果**：能清楚说出「$fsm 单元先出现、后消失」，以及两种编码下状态位宽的差异。具体单元名（带 `$` 序号）会随 `autoidx` 变化，属正常现象。

> 运行命令示例：`./build/yosys run_fsm.ys`。若尚未构建，参见 u1-l2。完整命令行结果「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `fsm` 的 help 里建议先跑 `opt -nosdff -nodffe`？

**参考答案**：`fsm_detect`/`fsm_extract` 只识别 `$dff`/`$adff` 这一小撮触发器类型（见 `fsm_extract.cc:275`）。`$sdff`（带同步复位/使能的触发器）和 `$dffe`（带使能的触发器）不在识别范围内，若不先把它们退回成 `$dff`，对应的状态机会被漏掉。

**练习 2**：`fsm_recode` 对 `auto` 编码是如何在 one-hot 与 binary 之间抉择的？

**参考答案**：状态数 < 32 选独热（一位对应一个状态，省译码逻辑），≥ 32 选二进制（省寄存器位），见 `fsm_recode.cc:81`。这条阈值是经验值，权衡的是「独热带来的译码简化」与「寄存器增多」的成本。

**练习 3**：`opt_unreachable_states` 为什么用 `while(1)` 循环而不是单趟？

**参考答案**：删掉一个不可达状态后，原本「只有从它出发才能到达」的状态可能又变成新的不可达状态（级联效应），需要跑到不动点才算干净，见 `fsm_opt.cc:39-77`（循环在 `unreachable_states.empty()` 时才 `break`）。

---

### 4.2 线程模型：Yosys 如何做并行网表处理

#### 4.2.1 概念说明

Yosys 的绝大多数 Pass 是单线程的——RTLIL 数据结构（Module 持有的 `wires_`/`cells_` 字典、`autoidx` 全局计数器、`IdString` 内部化表）**不是线程安全**的。但有些计算密集的 Pass（比如 `opt_merge` 在大模块上比较成千上万个 cell 是否相同）确实能从多核受益，所以 Yosys 在 `kernel/threading` 里提供了一套「受控并行」原语，并用一个守卫对象 `Multithreading` 来约束：**只有当这个守卫对象存在时，才允许多个线程并发访问 RTLIL，且此时禁止做 `autoidx++`、禁止新建 `IdString`**。

这意味着 Yosys 的并行模式是 **fork-join + 屏障同步**，而不是随时抢着改数据结构：

- 主线程把工作切成若干「分片」，每片独立、无冲突；
- 启动一组工作线程，各跑一片，跑完在屏障处汇合；
- 汇合后主线程再单线程地合并结果、改 RTLIL。

这套设计的好处是：并行的「计算」（如算哈希、比较）可以放手多线程跑，而「改 RTLIL」这种危险操作始终留在单线程段。

#### 4.2.2 核心流程

线程数计算遵循一个「环境变量 + 硬件探测 + 工作量阈值」的三层模型：

```
YOSYS_MAX_THREADS 环境变量      (上限，默认 INT32_MAX)
        ↓
std::thread::hardware_concurrency()   (CPU 核数)
        ↓ 取 min
可用线程数 available_threads
        ↓ 减去 reserved_cores、再受 max_worker_threads 限制
实际工作线程数 num_threads
        ↓ max(0, ...)
可能是 0（单线程回退）
```

关键在于「可能是 0」：当工作量太小、或核数太少时，`pool_size` 返回 0，调用方就退化为单线程，避免「创建线程的开销 > 并行收益」。`opt_merge` 就是典型——它要求「至少每千个 cell 一个线程」，所以模块不到 2000 个 cell 时根本不会多线程。

并发数据结构方面，Yosys 自带了一套「分片（sharded）」容器：`ShardedVector`、`ShardedHashtable`。思路是「每个线程一个桶」，写入时只写自己的桶（无锁），全部写完后再按线程顺序拼接，既快又确定（结果顺序可复现）。

#### 4.2.3 源码精读

**线程数与环境变量**

`threading.cc` 用函数内 `static` 变量缓存环境变量的读取（只读一次）：

[threading.cc:6-18](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/threading.cc#L6-L18) — `YOSYS_MAX_THREADS` 缺省为 `INT32_MAX`（即不限）。

线程数的核心算法在 `ThreadPool::pool_size`：

[threading.cc:43-55](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/threading.cc#L43-L55) — `min(hardware_concurrency, YOSYS_MAX_THREADS)` 再减 `reserved_cores`、受 `max_worker_threads` 上限，结果取 `max(0, ...)`。注意整个函数体在 `#ifdef YOSYS_ENABLE_THREADS` 内，否则直接返回 0。

[threading.cc:57-63](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/threading.cc#L57-L63) — `work_pool_size` 在 `pool_size` 基础上再除以「每线程工作量」，并允许用 `YOSYS_WORK_UNITS_PER_THREAD` 覆盖（测试用）。

**`YOSYS_ENABLE_THREADS` 这对开关**

注意「用户层选项」与「编译宏」是两层（承接 u1-l2 的 `condition()` 模式）：用户在 CMake 里看到的是 `YOSYS_DISABLE_THREADS`，它与线程库探测结果合并后生成编译宏 `YOSYS_ENABLE_THREADS`：

CMakeLists.txt:48 `option(YOSYS_DISABLE_THREADS ...)` 默认 OFF；
CMakeLists.txt:304 `condition(YOSYS_ENABLE_THREADS Threads_FOUND AND HAVE_PTHREAD_CREATE AND NOT YOSYS_DISABLE_THREADS)`。

`threading.h` 里大量 `#ifdef YOSYS_ENABLE_THREADS` 的目的，是在**单线程构建**下把 `Mutex`/`CondVar` 等退化成空操作（不依赖 `<mutex>` 头，零开销）：

[threading.h:20-43](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/threading.h#L20-L43) — `Mutex`/`CondVar`/`UniqueLock`/`LockGuard` 在未启用线程时变成 no-op 桩。

**守卫对象 `Multithreading`：约束 RTLIL 的并发访问**

`Multithreading` 是一个 RAII 守卫：构造时置 `active_ = true`，析构时置回 false。它的注释点明了它的约束作用：

[yosys_common.h:273-285](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys_common.h#L273-L285) — 「当多线程访问 RTLIL 时必须存在此守卫；`active()` 为真时不能用 `autoidx`、不能新建 `IdString`」。

实现与断言：

[yosys.cc:112-126](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L112-L126) — 构造/析构互相断言 `active_` 取反，防止嵌套。

约束落地的证据在 `Autoidx::operator++`：自增分配新序号时断言「当前不是多线程态」，因为序号分配必须串行：

[yosys.cc:131-135](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/yosys.cc#L131-L135) — `log_assert(!Multithreading::active())`。

**屏障同步：`ParallelDispatchThreadPool`**

这是最常用的并行 API。它的 `run()` 把一个闭包复制到每个线程上执行，然后等所有线程完成（屏障），同一时刻只允许一个 `run()`：

[threading.cc:122-135](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/threading.cc#L122-L135) — 设活跃线程数、置 `current_work`、`signal_workers_start`、主线程自己也跑一份（`work({{0}, ...})`）、`wait_for_workers_done`。

工作线程的主循环：

[threading.cc:137-153](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/threading.cc#L137-L153) — 等开始信号 → 跑闭包（线程号 +1，主线程是 0）→ 信号完成；`current_work == nullptr` 是析构时的退出标志。

`RunCtx` 提供了把 N 个元素自动切成 `num_threads` 段的便利方法：

[threading.h:198-203](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/threading.h#L198-L203) — `item_range(num_items)` 返回当前线程应处理的下标区间。

切分算法：

[threading.cc:85-96](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/threading.cc#L85-L96) — `item_range_for_worker`：均分后，前 `余数` 个线程各多拿一个元素。

**分片容器：写入无锁、顺序确定**

`ShardedVector` 是「每线程一桶」的向量，注释强调它的确定性（线程 0 的元素先于线程 1）：

[threading.h:333-356](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/threading.h#L333-L356) — `insert` 按线程号写入对应桶，`empty` 遍历所有桶。

**实战范例：`opt_merge` 如何用线程池**

`opt_merge.cc` 是 Yosys 里少数真正多线程化的 Pass，展示了「阈值控制 + 守卫 + 并发队列」的标准用法：

[opt_merge.cc:370-394](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_merge.cc#L370-L394) — 用 `ThreadPool::pool_size(0, cells_size/1000)` 算线程数（「每千 cell 一个线程」），用 `ConcurrentQueue` 在主线程与工作线程之间传「cell 区间 / 哈希 / 去重结果」，构造一个 `ThreadPool` 跑流水线。

[opt_merge.cc:408-423](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/opt/opt_merge.cc#L408-L423) — 在派发工作前构造 `Multithreading multithreading;` 守卫（仅作用域内生效），工作线程在守卫保护下并发算哈希；守卫离开作用域后主线程才安全地继续。

这个例子完整体现了 Yosys 并行的纪律：**并发段只算不改 RTLIL，改 RTLIL 留给守卫之外的单线程段**。

#### 4.2.4 代码实践

**目标**：通过阅读与运行，理解线程数随模块规模变化，并验证守卫的约束。

**步骤（源码阅读型 + 运行型）**：

1. 运行 threading 的单元测试（见 4.3 节），观察 `ParallelDispatchThreadPool`、`ShardedVector`、`ConcurrentQueue` 的行为，对照断言理解：
   - `ParallelDispatchThreadPool(0)` 被当作 1 个线程（`threadingTest.cc:15-30`）。
   - `item_range_for_worker(10, 0, 3) == {0,4}`、`(10,1,3)=={4,7}`、`(10,2,3)=={7,10}`，即均分并把余数分给前几个线程（`threadingTest.cc:118-122`）。

2. 在 `opt_merge.cc:372` 处确认阈值：`pool_size(0, cells_size/1000)`。推导：模块需要至少 2000 个 cell 才会得到 2 个工作线程（因为 1999/1000 = 1，`pool_size` 退化为 0 → 单线程）。

3. 想强制开启/关闭线程做对比实验，可在构建时设置环境：

```bash
# 关闭线程（单线程构建）
cmake -B build -DYOSYS_DISABLE_THREADS=ON .
cmake --build build

# 或在运行时限制线程数
YOSYS_MAX_THREADS=1 ./build/yosys run_fsm.ys
```

**需要观察的现象**：

- `YOSYS_DISABLE_THREADS=ON` 构建后，`opt_merge` 等永远单线程（`pool_size` 返回 0）。
- `YOSYS_MAX_THREADS=1` 运行时限制为单线程，但对大设计的 `opt_merge` 仍按工作量阈值决定是否真多线程（取 min）。

**预期结果**：能解释「用户层 `YOSYS_DISABLE_THREADS` → 编译宏 `YOSYS_ENABLE_THREADS` → 运行时 `YOSYS_MAX_THREADS`」三层各自的作用域。命令的实际耗时差异「待本地验证」（取决于机器核数与设计规模）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `autoidx++` 要断言 `!Multithreading::active()`？

**参考答案**：`autoidx` 是一个全局递增计数器，用于生成 `$` 开头的唯一自动名。多线程同时自增会 race（丢序号、重号），而新建 `IdString` 同样会动全局内部化表。所以 Yosys 规定：并发段里只做「读 + 计算」，凡是会动这些全局结构的操作（分配新序号、新建 IdString、改 RTLIL）都必须在守卫之外的单线程段做。

**练习 2**：`ShardedVector` 相比「一个带锁的 vector」有什么优势？

**参考答案**：写的时候每个线程只写自己的桶，完全无锁，没有竞争开销；读/合并时按线程号顺序拼接，结果顺序是确定的（可复现），这对回归测试很重要。带锁的共享 vector 则每次写都要竞争同一把锁，且元素顺序取决于调度，不确定。

**练习 3**：`pool_size` 在什么情况下返回 0？返回 0 后调用方该怎么办？

**参考答案**：当 `max_worker_threads` 为 0、或 `available_threads - reserved_cores ≤ 0`、或未启用线程编译宏时返回 0。调用方应据此退化为单线程（如 `opt_merge` 在 `num_worker_threads == 0` 时直接在主线程跑，见 `opt_merge.cc:379-380`）。

---

### 4.3 测试体系：单元测试与功能测试

#### 4.3.1 概念说明

Yosys 的测试分两个相对独立的体系，分别由不同的构建系统驱动：

- **单元测试（unit tests）**：C++ 层面、针对内核数据结构与算法的细粒度测试，用 GoogleTest（gtest/gmock）框架，由 CMake + CTest 驱动。位于 `tests/unit/`。
- **功能测试（functional tests）**：把 yosys 当黑盒，跑大量 `.ys` 脚本和 `.v` 设计，检查综合结果是否正确（常配合 iverilog 做仿真对比），由 `tests/` 下的 Makefile 体系驱动。

二者解决不同问题：单元测试快、隔离、能精确断言内部状态（如「这个 `SigSpec` 提取后等于那个」）；功能测试慢、端到端、能捕获「整条综合流水线的回归」。

此外还有一个值得注意的层次：很多功能测试目录（`tests/simple`、`tests/fsm` 等）的 Makefile 是**自动生成**的——用 Python 脚本扫描目录里的 `.v` 文件生成规则，部分目录（如 `tests/fsm`）甚至会用脚本随机生成大量测试设计。

#### 4.3.2 核心流程

**单元测试流程**：

```
CMake 找到 GoogleTest (find_package)
  ↓ GTest_FOUND?
tests/unit/CMakeLists.txt 定义 yosys_gtest() 函数
  ↓ 为每个 *.cc 生成可执行 gtest-<name>
gtest_discover_tests() 把每个 TEST 注册成 CTest 用例
  ↓
ctest --test-dir tests/unit   ← 由 test-unit 目标触发
```

**功能测试流程**：

```
tests/Makefile 枚举各测试目录 (MK_TEST_DIRS / 直接子目录)
  ↓
各目录的 Makefile（多数由 gen_tests_makefile.py 生成）
  ↓ 对每个 *.v：read_verilog → 综合 → (iverilog) 仿真 → 比较结果
make vanilla-test   ← 由 test-vanilla 目标触发
make functional     ← 由 test-functional 目标触发（需 ENABLE_FUNCTIONAL_TESTS）
```

#### 4.3.3 源码精读

**单元测试的 CMake 框架**

`tests/unit/CMakeLists.txt` 定义了 `yosys_gtest()` 函数，把「建可执行、链接 gtest + yosys 组件、注册到 CTest」三步打包，并要求 GoogleTest 存在：

[tests/unit/CMakeLists.txt:1-29](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/tests/unit/CMakeLists.txt#L1-L29) — `include(CTest)` + `include(GoogleTest)`；`yosys_gtest` 用 `gtest_discover_tests` 自动发现用例；只有 `GTest_FOUND` 时才 `add_subdirectory` 并 `enable_testing()`。

所以**没有 GoogleTest 就没有 `test-unit` 目标里的可执行**——这是 4.3.4 实践要注意的前提。

**顶层构建目标**

`CMakeLists.txt` 定义了几个 test 目标，是本讲实践任务的入口：

[CMakeLists.txt:535-560](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/CMakeLists.txt#L535-L560) — `test-unit`（跑 `ctest --test-dir tests/unit`）、`test-vanilla`（跑 `make vanilla-test`）、`test-functional`、以及聚合的 `test`（依赖 `test-unit` + `test-vanilla`）。

**功能测试的入口**

`tests/Makefile` 顶部用注释列出了「哪些目录的测试不跑」（errors/lut/pyosys/smv/sva/tools/unit/vloghtb），随后用 `MK_TEST_DIRS +=` 列出「Makefile 由脚本生成的目录」：

[tests/Makefile:1-9](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/tests/Makefile#L1-L9) — 注释说明 unit 目录不在这个 Makefile 里跑（它走 CMake/CTest），呼应了「两套体系分离」的设计。

`tests/fsm` 是「脚本随机生成测试」的典型：

[tests/fsm/generate_mk.py:1-20](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/tests/fsm/generate_mk.py#L1-L20) — 用随机表达式生成大量 `uut_*.v`（被测单元），默认 50 个，配合 `gen_tests_makefile` 产出 Makefile。这是对 FSM 流水线的模糊测试式回归。

**单元测试样本**

`threadingTest.cc` 正好覆盖了 4.2 节讲的全部并行原语，可作为理解 threading 的活教材：

[tests/unit/kernel/threadingTest.cc:15-30](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/tests/unit/kernel/threadingTest.cc#L15-L30) — 验证 `ParallelDispatchThreadPool(0)` 当作 1 线程、`run()` 把闭包复制到每个线程。

[tests/unit/kernel/threadingTest.cc:118-126](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/tests/unit/kernel/threadingTest.cc#L118-L126) — 用硬编码期望值断言 `item_range_for_worker` 的切分结果，正好印证 `threading.cc:85-96` 的算法。

[tests/unit/kernel/threadingTest.cc:218-239](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/tests/unit/kernel/threadingTest.cc#L218-L239) — `#ifdef YOSYS_ENABLE_THREADS` 区分期望，单线程构建下 counter 为 0，直接体现了编译宏的影响。

一个更简单的 gtest 样本（展示基本写法）：

[tests/unit/kernel/bitpatternTest.cc:1-25](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/tests/unit/kernel/bitpatternTest.cc#L1-L25) — `#include <gtest/gtest.h>`、`TEST(套件名, 用例名)`、`EXPECT_TRUE/EXPECT_FALSE` 断言。

**功能测试样本设计**

`tests/simple/always01.v` 是一个最小功能测试设计，正好与 u1-l4 讲过的计数器同构：

[tests/simple/always01.v:1-11](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/tests/simple/always01.v#L1-L11) — 带同步复位的 4 位计数器：`count <= reset ? 0 : count + 1`。这类设计会被功能测试框架综合、仿真并与参考输出比对。

#### 4.3.4 代码实践

**目标**：实际跑一遍单元测试，确认 threading/FSM 相关内核功能在当前构建下通过。

**步骤**：

1. 确保已按 u1-l2 用 CMake 构建（`cmake -B build && cmake --build build`），并且 CMake 找到了 GoogleTest（构建时会打印 `have_threads`、googletest 等特性信息）。

2. 运行单元测试目标：

```bash
cmake --build build --target test-unit
```

它等价于 `ctest --test-dir tests/unit --output-on-failure`。

3. 想只跑 threading 相关用例：

```bash
ctest --test-dir build/tests/unit -R Threading --output-on-failure
```

4. （可选）跑聚合目标，同时触发单元测试与功能测试：

```bash
cmake --build build --target test
```

**需要观察的现象**：

- `test-unit` 会列出 `gtest-kernel`、`gtest-opt`、`gtest-techmap` 等可执行及其下每个 `TEST`，全部 `PASSED` 即通过。
- 若构建时没找到 GoogleTest，`tests/unit` 下不会生成可执行，`test-unit` 会报告「无测试」——此时需要安装 googletest 或确保 CMake 能联网拉取（`CMakeLists.txt:294-296` 把它标为可选依赖）。
- 功能测试（`test-vanilla`）依赖 `iverilog`（见 `tests/simple/generate_mk.py` 开头的检查）和现成的 `yosys` 可执行，未安装时会跳过或报错。

**预期结果**：能区分「单元测试走 CTest、功能测试走 Makefile」两条路径，并知道 `test-unit` 的前提是 GoogleTest。本地是否安装了 iverilog/googletest「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `tests/Makefile` 的注释里把 `unit` 列为「不在这里跑」的目录？

**参考答案**：因为单元测试由 CMake/CTest 体系驱动（`tests/unit/CMakeLists.txt` + `gtest_discover_tests`），与 `tests/Makefile` 这套 Makefile 功能测试体系是分离的。`test-unit` 这个 CMake 目标才负责跑它，避免两套体系重复或冲突。

**练习 2**：`yosys_gtest()` 函数里的 `gtest_discover_tests(${target})` 起什么作用？

**参考答案**：它在构建后实际运行一遍测试可执行，枚举出其中所有的 `TEST`/`TEST_f`，为每一个注册一个独立的 CTest 用例。好处是能 `ctest -R 名字` 精确筛选单个用例，且每个用例的通过/失败单独上报。

**练习 3**：`tests/fsm/generate_mk.py` 为什么用随机生成而不是手写固定设计？

**参考答案**：FSM 流水线（detect/extract/opt/recode/map）分支极多，手写设计难以覆盖边角；用随机表达式批量生成（默认 50 个、可换种子）是一种轻量模糊测试，能更高效地捕获回归，且种子可复现。

## 5. 综合实践

把三个主题串起来：写一个含状态机的设计，**先用 `fsm` 走完提取-优化-重编码-映射**，再观察 Yosys **在 `opt` 阶段是否/如何对它多线程处理**，最后**用单元测试与功能测试做回归**。

1. 用 4.1.4 的 `fsm_demo.v`，写脚本：

```
read_verilog fsm_demo.v
hierarchy -top fsm_demo
proc
opt -nosdff -nodffe
fsm                       ; 完整 FSM 流程
opt                       ; 再做一轮通用优化（含 opt_merge）
stat                      ; 统计单元
write_rtlil result.il
```

2. 运行 `./build/yosys run.ys`，对照本讲的源码解读回答：
   - 综合后还有没有 `$fsm` 单元？（应没有，已被 `fsm_map` 拆解）
   - 状态寄存器是几位？带不带 `onehot` 属性？（取决于 auto 选的编码）
   - `stat` 报告里状态机被拆成了哪些 `$` 单元（`$dff`、`$pmux`、`$eq` 等）？

3. 把这个设计也放进功能测试思路：参考 `tests/simple` 的模式，用 iverilog 仿真原设计、用 yosys 综合后再仿真，比较输出是否一致（这其实就是 `tests/simple` 每个用例在做的事）。

4. 运行 `cmake --build build --target test-unit` 确认内核层（含 threading、RTLIL、celltypes）测试通过，作为「底座是稳的」的保证。

**完成标志**：你能用一句话向别人解释「`fsm` 在抽象状态机层做了什么、为什么 Yosys 的多线程是受限的 fork-join、以及单元测试和功能测试分别由哪套构建系统驱动」。

## 6. 本讲小结

- `fsm` 是一条编排型 Pass，按 `fsm_detect → fsm_extract → (fsm_opt/opt_clean) → fsm_recode → fsm_info → fsm_map` 串联子 pass；它只识别 `$dff`/`$adff`，故需先 `opt -nosdff -nodffe`。
- 提取的核心是 `ConstEval` 符号仿真：沿 mux 树找出状态码与控制输入，再对未定义控制位做 0/1 二叉展开，枚举出完整迁移表，结果存入 `$fsm` 单元的压平参数（`FsmData::copy_to_cell`）。
- 优化与重编码在抽象层做：删不可达状态、找 don't-care（卡诺图式合并）、按阈值（< 32 选独热、否则二进制）重选编码，最后 `fsm_map` 物化回 `$dff`+`$pmux`+`$eq`。
- Yosys 的 RTLIL 不是线程安全的；并行受 `Multithreading` 守卫约束——并发段只算不改，`autoidx++` 与新建 `IdString` 被禁。线程数经 `YOSYS_MAX_THREADS`（运行时）与 `YOSYS_DISABLE_THREADS→YOSYS_ENABLE_THREADS`（编译期）两层控制。
- 并行原语集中在 `kernel/threading`：`ThreadPool`、`ParallelDispatchThreadPool`（屏障式 `run()`）、`ShardedVector`/`ShardedHashtable`（无锁分片）、`ConcurrentQueue`；`opt_merge` 是真实使用范例（「每千 cell 一个线程」阈值）。
- 测试分两套：单元测试（GoogleTest + CTest，`tests/unit`，`test-unit` 目标）与功能测试（Makefile + iverilog，`tests/`，`test-vanilla` 目标）；部分功能测试目录（如 `tests/fsm`）由脚本随机生成。

## 7. 下一步学习建议

- 回到 **u6-l3（opt）** 重读 `opt_merge`/`opt_clean`，现在你可以对照它们的线程化代码理解「为什么大模块才会多线程」。
- 阅读 `passes/opt/opt_clean/` 目录（`wires.cc`、`cells_all.cc` 等都用了 `ParallelDispatchThreadPool`），作为 threading 的进阶实战阅读。
- 想深入形式化与状态机的关系，可结合 **u10-l1（SAT/形式验证）**：FSM 的迁移表本质是一个状态转移系统，正是 BMC/k-归纳要证明的对象。
- 若想给 Yosys 贡献测试，参考 `tests/unit/CMakeLists.txt` 的 `yosys_gtest()` 写一个新单元测试，或在 `tests/simple` 放一个 `.v` + 期望输出，体验「脚本自动生成 Makefile」的流程。
