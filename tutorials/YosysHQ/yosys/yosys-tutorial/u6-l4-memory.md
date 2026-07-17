# memory：存储器推断与映射

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 Yosys 里「存储器」（`reg [W-1:0] mem [0:N-1]`）在 RTLIL 中是如何表示的：一个 `RTLIL::Memory` 对象 + 一组读/写端口单元。
- 说出 `memory` 这条大命令把存储器从「行为级端口单元」一路变到「触发器 + 多路器」所经过的子 pass 顺序，以及每个子 pass 的职责。
- 解释 `memory_collect` 如何把零散的端口「打包」成一个 `$mem_v2`，`memory_share` 如何用「按地址合并」和「SAT 互斥证明」减少端口数，`memory_narrow` 如何反向地拆分宽端口。
- 读懂 `memory_map` 把一个多端口存储器展开成「每个字一个 `$dff`、读端一棵 `$mux` 二叉树、写端一组地址译码 `$wrmux`」的源码。

本讲承接 u6-l3（`opt` 网表优化大流程）。`proc` 把行为级 `always` 翻成门级网表后，存储器仍以「端口单元」的高层形态存在，`memory` 这一组 pass 专门负责把它们下沉为底层门和触发器。下一讲 u6-l5 将进入 `techmap`/`simplemap` 的工艺映射。

## 2. 前置知识

在进入源码前，先建立两点直觉。

**存储器本质上是一组带地址的寄存器。** 一段 `reg [7:0] mem [0:255]` 在硬件里可以理解成「256 个 8 位寄存器排成一排」，读写时先用地址选中某一排，再读出或写入它的值。所以「把存储器映射成触发器」这句话的物理含义就是：为每个字造一个触发器存它的值，再用多路器（`$mux`）按地址选中读出，用地址译码电路把写入导向正确的那个字。理解了这一点，本讲后面所有源码都只是这句大白话的工程实现。

**端口（port）是抽象层，字（word）是物理层。** 在 RTL 综合的早期，Yosys 并不关心一个存储器有几个寄存器，只关心它有几个「读端口」和「写端口」：一个读端口 = 一组「地址输入 + 数据输出」+ 可选时钟，一个写端口 = 一组「地址 + 数据 + 使能」+ 可选时钟。这一层抽象方便做端口合并、时钟域分析等优化；等优化做完，再在 `memory_map` 里一次性展开成触发器阵列。`passes/memory/` 这一族 pass 的工作，就是在这两层抽象之间搬运：先在端口层收拾干净，再下沉到字/门层。

你还需要回忆 u2-l3、u3-l4 里几个内部单元：`$dff`（触发器）、`$mux`（二选一多路器，端口 `A/B/S→Y`）、`$and`、`$eq`（相等比较，`A/B→Y`）。本讲会反复用到它们。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [passes/memory/memory.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory.cc) | `memory` 编排 pass：自身不做算法，按固定顺序串联所有 `memory_*` 子 pass。 |
| [passes/memory/memory_collect.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_collect.cc) | `memory_collect`：把每个存储器的读/写端口「打包」成单个 `$mem_v2` 多端口单元。 |
| [passes/memory/memory_share.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_share.cc) | `memory_share`：按地址合并读/写端口，并用 SAT 证明「两个写端口永不同时激活」来共享端口。 |
| [passes/memory/memory_narrow.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_narrow.cc) | `memory_narrow`：把宽端口（一次访问多个字）拆回窄端口。注意：它**不在**默认 `memory` 流水线内，是供特殊流程调用的独立 pass。 |
| [passes/memory/memory_map.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_map.cc) | `memory_map`：把 `$mem_v2` 展开成「每字一个 `$dff` + 读端 `$mux` 二叉树 + 写端地址译码 `$wrmux`」。 |
| [kernel/mem.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/mem.h) | `Mem`/`MemRd`/`MemWr` 辅助类：把存储器单元载入成便于操作的结构体，是上面所有 pass 共享的核心抽象。 |
| [kernel/rtlil.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h) | `RTLIL::Memory` 结构体定义（仅几何信息：宽/起始偏移/大小）。 |

## 4. 核心概念与源码讲解

### 4.1 memory 编排与存储器数据模型

#### 4.1.1 概念说明

Yosys 处理存储器时，先要回答两个问题：存储器在 RTLIL 里长什么样？谁来把一堆子 pass 串起来？

**数据模型分两层。** 第一层是 `RTLIL::Memory`，它只描述存储器的「几何」：每个字多少位（`width`）、地址从几开始（`start_offset`）、一共几个字（`size`），外加一个名字和属性表。它**不**包含任何读写端口信息，端口是用独立的单元表达的。第二层是端口单元：读端口、写端口、初始化值各是一种单元，它们通过参数 `MEMID` 引用同一个 `RTLIL::Memory` 的名字。

**编排 pass。** `memory` 这条命令和 u6-l3 讲过的 `opt`、u6-l2 讲过的 `proc` 同属一类——编排型 pass：自己的 `execute()` 里没有任何算法，只是按一个精心排好的「黄金顺序」依次 `Pass::call` 一串子 pass。这样设计的好处是：每个子 pass 只做一件小事、易于测试，而用户只要敲一条 `memory` 就能拿到「端到端处理好」的存储器。

#### 4.1.2 核心流程

`memory` 的执行顺序（与 `help()` 列出的完全一致）：

```
opt_mem
opt_mem_priority
opt_mem_feedback
memory_bmux2rom          # 默认开，-norom 跳过
memory_dff               # 默认开，-nordff / -memx 跳过
opt_clean
memory_share             # -nowiden / -nosat 可关部分优化
opt_mem_widen
memory_memx              # 仅 -memx 时
opt_clean
memory_collect           # 把端口打包成 $mem_v2
memory_bram -rules ...   # 仅 -bram 时
memory_map               # 默认开，-nomap 跳过
```

几个要点：

- **早段（`opt_mem*` / `memory_bmux2rom` / `memory_dff`）**：仍在「端口单元」层做局部优化，比如把触发器吸收进读端口（`memory_dff`）、把纯多路器阵列识别成只读存储器 ROM（`memory_bmux2rom`）。
- **中段（`memory_share` / `opt_mem_widen`）**：减少端口数量——合并同地址端口、用 SAT 证明可共享的写端口、把窄端口拼成宽端口。
- **后段（`memory_collect` → `memory_map`）**：先打包成单个 `$mem_v2`，再下沉成触发器与门。`-nomap` 会停在 `memory_collect` 之后，保留 `$mem_v2` 不展开——这正是 `synth` 在 coarse 阶段的做法（见 4.1.4）。

#### 4.1.3 源码精读

`RTLIL::Memory` 的定义极其简短，只有三个几何字段：

[kernel/rtlil.h:2485-2499](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/rtlil.h#L2485-L2499) —— `RTLIL::Memory` 继承自 `NamedObject`（因此有名、有属性），仅含 `width`、`start_offset`、`size`。它对应的文本形态由后端打印：

[backends/rtlil/rtlil_backend.cc:160-171](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/backends/rtlil/rtlil_backend.cc#L160-L171) —— `dump_memory` 打印出形如 `memory width 8 size 256 \mem` 的文本，即一个 `RTLIL::Memory` 的全部几何信息。

`memory` 编排 pass 的 `help()` 把子 pass 顺序明明白白列出来，`execute()` 则一一调用：

[passes/memory/memory.cc:36-54](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory.cc#L36-L54) —— `help()` 中声明的子 pass 顺序。

[passes/memory/memory.cc:108-127](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory.cc#L108-L127) —— `execute()` 用一串 `Pass::call(design, "子pass名" + 选项)` 严格按 help 的顺序执行；各开关（`-norom`/`-nordff`/`-nomap`/`-memx`/`-bram` 等）只是条件性地跳过某些调用。

这一节的关键，是理解所有 `memory_*` 子 pass 并不直接去解析 `RTLIL::Memory` 或一堆零散的端口单元，而是统一通过 `Mem` 辅助类来操作。`Mem` 类（[kernel/mem.h:92-101](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/mem.h#L92-L101)）把一个存储器载入成 `width`/`start_offset`/`size` + `inits` + `rd_ports` + `wr_ports` 的结构体，各 pass 修改它后用 `emit()` 写回——这是 4.2～4.5 所有 pass 共享的工作方式。

#### 4.1.4 代码实践

**实践目标：** 在真实综合流程里定位 `memory` 的两个调用点，确认「先 `-nomap` 后 `memory_map`」的两阶段策略。

**操作步骤：**

1. 打开 [techlibs/common/synth.cc](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/techlibs/common/synth.cc)，搜索 `memory`。
2. 在 coarse 阶段（约 315 行）找到 `run("memory -nomap" + memory_opts);`，在 fine 阶段（约 321 行）找到 `run("memory_map");`。

**需要观察的现象：** `synth` 故意在 coarse 用 `-nomap` 让存储器以 `$mem_v2` 形态保留（便于后续 BRAM/DSP 等推断），到了 fine 阶段才真正展开成触发器。

**预期结果：** 你能说清楚——如果某厂商流程想把存储器推断成块 RAM，它会在 coarse 之后的 `$mem_v2` 形态上动手；若想强制展开成寄存器，则用 `memory_map`。

### 4.2 memory_collect：把端口打包成 `$mem_v2`

#### 4.2.1 概念说明

前端（`read_verilog`）产出存储器时，每一个读端口、写端口、初始化值都是**独立的单元**（分别形如 `$memrd`/`$memwr`/`$meminit` 的端口单元）。这种「解包」形态好处是简单、贴近 RTL 语义，坏处是端口分散、不便整体分析。

`memory_collect` 的工作就是「打包」：把属于同一个 `RTLIL::Memory` 的所有读端口、写端口、初始化数据，合并进**单个** `$mem_v2` 多端口单元。打包后，`RTLIL::Memory` 对象本身会被删除（信息已全部进入 `$mem_v2` 的参数里），后续 pass 只需要面对「一个存储器 = 一个单元」的整齐表示。

#### 4.2.2 核心流程

`memory_collect` 的逻辑极短，真正的重活在共享的 `Mem` 类里：

```
对每个模块、每个未打包的 Mem：
    置 packed = true
    调 emit()   # emit 负责把 rd_ports/wr_ports/inits 写进一个 $mem_v2 单元
```

`Mem::emit()` 在 `packed == true` 时，会新建（或复用）一个 `$mem_v2` 单元，把 `WIDTH/OFFSET/SIZE/MEMID` 等几何信息写成参数，把所有读端口、写端口的 `clk/en/addr/data` 分别拼成大向量写进端口参数，最后删除原来的零散端口单元。

#### 4.2.3 源码精读

整个 pass 的 `execute()` 只有几行：

[passes/memory/memory_collect.cc:41-51](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_collect.cc#L41-L51) —— 遍历选中模块里的每个 `Mem`，凡是 `packed == false` 的，置 `packed = true` 并 `emit()`。打包与否完全由这个布尔位决定。

打包的细节在 `Mem::emit()`，注意它**总是**写成 `_v2` 形态：

[kernel/mem.cc:117-133](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/mem.cc#L117-L133) —— `packed` 分支：先删掉旧的 `RTLIL::Memory` 对象，再创建/复用类型为 `$mem_v2` 的单元，把 `MEMID/WIDTH/OFFSET/SIZE` 写成参数，随后把所有端口拼成向量写进单元（节选仅展示开头）。

关于命名：当前 Yosys 的存储器单元族是 `$mem_v2`/`$memrd_v2`/`$memwr_v2`/`$meminit_v2`。`Mem` 类能同时读取旧的 `$mem`/`$memrd`/`$memwr`（兼容形态）和新的 `_v2` 形态，但在 `emit()` 时一律归一化成 `_v2`。所以即便前端产出的是旧名端口单元，经过 `memory_collect` 后都会变成规整的 `$mem_v2`。

#### 4.2.4 代码实践

**实践目标：** 观察「打包」前后单元形态的变化。

**操作步骤：**

1. 写一个最小存储器 `mem_test.v`（见本讲末尾综合实践的同名文件，或自己写一个 `reg [7:0] mem [0:3]` 带一个写端口、一个异步读端口）。
2. `read_verilog mem_test.v` 后立刻 `write_rtlil`，观察端口单元（`$memrd`/`$memwr` 散落）。
3. 只跑到 `memory_collect`（`memory -nomap` 也会跑到这一步），再 `write_rtlil`，观察它们合并成了单个 `$mem_v2`。

**需要观察的现象：** 打包前是多个独立端口单元 + 一个 `memory ... \mem` 声明；打包后声明消失，取而代之的是一个 `cell $mem_v2 ...`，其参数里集中了 `RD_PORTS`/`WR_PORTS` 等计数与拼接好的端口信号。

**预期结果：** 端口数（在单元层面）从「多个」变成「一个」。

### 4.3 memory_share：合并读/写端口

#### 4.3.1 概念说明

RTL 代码很容易写出「看起来很多端口、实际可以合并」的存储器。比如两个读端口恒定读同一个地址，或两个写端口分属互斥的条件分支、永不同时激活。若不加优化，`memory_map` 会为每个端口都造一套译码与多路逻辑，造成面积浪费。

`memory_share` 专门减少端口数，用三种手段：

1. **按地址合并读端口**：两个读端口地址相同（或经拓宽后对齐），且时钟/使能/复位一致，就并成一个（可能更宽的）端口。
2. **按地址合并写端口**：同上，针对写端口。
3. **基于 SAT 的写端口共享**：调用 SAT 求解器证明两个写端口的使能信号「不可能同时为真」，于是安全地共享一个端口（用多路器在两者间选择地址/数据/使能）。

第 3 点最巧妙：它不是静态看地址，而是动态证明「这俩端口在任意输入下都不会同时写」，从而把两个写端口合并成一个——这正是 u10-l1 SAT 基础设施在综合优化里的实际用例。

#### 4.3.2 核心流程

`memory_share` 的 worker 先建好 `SigMap`（信号归一化，回忆 u3-l2）和初始化值表，然后对每个 `Mem`：

```
while (按地址合并读端口还有变化) consolidate_rd_by_addr(mem);
while (按地址合并写端口还有变化) consolidate_wr_by_addr(mem);
if (SAT 开启):
    对每个 Mem 跑 consolidate_wr_using_sat(mem)
```

「宽端口」（`wide_log2 > 0`，即一次访问 \(2^{\text{wide\_log2}}\) 个字）是合并中的关键概念：两个端口地址的高位相同、只有低位落在不同子字上时，可以把它们拼成一个一次读/写两个字的宽端口，从而省掉一个端口。`-nowiden` 关掉这种「拓宽」合并，`-nosat` 关掉 SAT 共享。

#### 4.3.3 源码精读

按地址合并读端口的入口，先做一系列「必须一致」的过滤：

[passes/memory/memory_share.cc:101-116](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_share.cc#L101-L116) —— 两个读端口要合并，必须时钟使能、时钟、极性、使能、异步/同步复位、`ce_over_srst` 全部一致，否则 `continue` 跳过。

接着判断能否通过「拓宽」对齐地址（即把窄端口拼成宽端口）：

[passes/memory/memory_share.cc:120-145](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_share.cc#L120-L145) —— 取两个端口地址的高位段比较；不一致时，若允许拓宽（`flag_widen`），再多拓宽一位尝试对齐。

SAT 共享写端口则把使能信号的输入锥编码成 SAT 问题：

[passes/memory/memory_share.cc:382-413](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_share.cc#L382-L413) —— 用 `QuickConeSat` 把一组写端口的使能信号导入 SAT，对每对端口求解「两者使能能否同时为真」。若 `solve(en_i, en_j)` 返回真（存在一组输入使两者同时激活），说明不能合并；返回假（永不同时激活）才安全合并。

合并后用 `$mux` 在两端口的数据/地址/使能间选择，等价地用一套端口承载原来两个端口的语义。pass 的 `help()` 也明确列出了这三种合并策略：

[passes/memory/memory_share.cc:522-534](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_share.cc#L522-L534) —— help 文本里「同地址合并」「相邻地址拓宽合并」「SAT 证明互斥后共享」三段说明。

#### 4.3.4 代码实践

**实践目标：** 观察 SAT 共享对端口数的影响。

**操作步骤：**

1. 写一个存储器，它有两个写端口，但被互斥的 `if/else` 驱动（例如 `if (sel) mem[a] <= x; else mem[b] <= y;` 经 `proc` 后会产生两个写端口，但 `sel` 保证它们不同时激活）。
2. 跑 `memory -nomap`（默认会跑 `memory_share`），用 `write_rtlil` 或 `stat` 查看写端口数。
3. 对比加 `-nosat` 重跑（`memory -nomap` 后单独 `memory_share -nosat`），再查写端口数。

**需要观察的现象：** 开 SAT 时两个写端口被合并成一个；关 SAT 时保留两个。

**预期结果：** 若无法本地运行，标注「待本地验证」亦可——重点是理解：SAT 证明互斥是端口数下降的根本原因。

#### 4.3.5 小练习与答案

**练习 1：** 为什么 `memory_share` 要先建 `SigMap`？
**答案：** 两个端口的「地址」可能经过 `assign` 改名、切片或拼接，直接比较 `SigSpec` 会误判为不同；用 `SigMap` 把连通的信号位归一化到同一个代表位后，才能正确判定「地址相同」。

**练习 2：** `-nowiden` 关掉的是什么优化？
**答案：** 关掉「把地址高位相同、低位对齐的两个端口，拼成一个一次访问多个字的宽端口」这种合并，即 `consolidate_rd_by_addr`/`consolidate_wr_by_addr` 里的拓宽分支。

### 4.4 memory_narrow：拆分宽端口（独立 pass）

#### 4.4.1 概念说明

`memory_share` 和 `opt_mem_widen` 的方向是「合并 / 拓宽」（减少端口、加宽每个端口）。`memory_narrow` 恰好相反：它把一个宽端口（一次访问 \(2^{\text{wide\_log2}}\) 个字）拆成多个一次只访问一个字的窄端口。

为什么需要这种反向操作？因为有些下游流程（例如把存储器映射到非对称块 RAM、或某些自定义展开流程）要求所有端口都是「最窄」的形态，才能正确匹配硬件资源。`memory_narrow` 就是为此提供的「拆分」工具。

**关键事实：** `memory_narrow` **不在**默认 `memory` 流水线里。回忆 4.1.2，`memory.cc` 调的是方向相反的 `opt_mem_widen`，根本没有 `memory_narrow`。它是一条供特殊流程按需手动调用的独立 pass。

#### 4.4.2 核心流程

```
对每个模块、每个 Mem：
    若任一读/写端口 wide_log2 > 0（即存在宽端口）：
        mem.narrow()    # 由 Mem 类负责把宽端口拆成若干等价窄端口
        mem.emit()      # 把改动写回单元
```

真正的拆分算法在 `Mem::narrow()` 里——它保证语义不变，只是把「一次访问 K 个字」改写成「K 次各访问 1 个字」。

#### 4.4.3 源码精读

整段 pass 很短，核心就是一个「有没有宽端口」的判断：

[passes/memory/memory_narrow.cc:48-66](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_narrow.cc#L48-L66) —— 扫描各端口，只要某个端口的 `wide_log2` 非零就置 `wide = true`，然后调 `mem.narrow()` 再 `emit()`。

[kernel/mem.h:150-153](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/mem.h#L150-L153) —— `Mem::narrow()` 的契约注释：把所有宽端口拆成等价的窄端口，且在 `emit()` 之前不改动实际网表。

#### 4.4.4 代码实践

**实践目标：** 理解 `memory_narrow` 与默认流水线方向相反。

**操作步骤：** 在 `memory.cc`（4.1）中确认 `memory_narrow` 不在调用序列里；再读 `memory_narrow.cc` 的 help 文本确认它「split wide ports」。

**预期结果：** 你能向别人解释：默认 `memory` 想让端口更少更宽，而 `memory_narrow` 是按需把端口拆窄的特殊工具。

### 4.5 memory_map：存储器映射为 `$dff` 与 `$mux`

#### 4.5.1 概念说明

`memory_map` 是这一族 pass 里工作量最大的一个，负责把一个 `$mem_v2`「物化」成具体的触发器和组合逻辑。它的输出可以用一句话概括：

> 每个字一个 `$dff` 存值；读端用一棵 `$mux` 二叉树按地址选字；写端用地址译码（`$eq`/`$and`）配合 `$wrmux` 把新值导向正确的字。

举一个 \(N=4\) 个字、地址位宽 \(a=2\) 的例子。读端口地址 `addr[1:0]` 选中一个字，读端会生成一棵两层深度的 `$mux` 树：第一层用 `addr[1]` 在「字 0/1」和「字 2/3」两组间选，第二层用 `addr[0]` 在组内选——共 \(N-1=3\) 个 `$mux`。写端口则对每个字、每个写端口，先用地址译码得到「这个写端口是否正写这个字」的一位选择信号，再 `$wrmux` 在「保持旧值」和「写入新值」间选择，结果接到该字 `$dff` 的 D 端。

读端 `$mux` 树的规模为 \(O(N)\)，写端每个字的译码为 \(O(a)\)。地址位宽 \(a=\lceil\log_2 N\rceil\)。

\[ \text{读}\$mux\text{ 数} = N - 1, \qquad \text{每字}\$dff\text{ 数} = 1 \]

#### 4.5.2 核心流程

`handle_memory(mem)` 分三步：

```
1. 为每个字造存储元件
   for i in 0..size:
       若是静态写（地址/数据/使能全常数）→ 直接接成常数/线
       否则 → 造一个 $dff（formal 流程用 $ff），D 端接 data_reg_in[i]，Q 端接 data_reg_out[i]
              并把 init 值挂到 Q 线的 init 属性上

2. 读接口：每个读端口造一棵 $mux 二叉树
   从「读数据输出」出发，逐位地址（MSB→LSB）每层把候选信号翻倍，
   最终 2^a 个叶子分别接到对应字的 data_read[]

3. 写接口：每个字、每个写端口
   addr_decode(wr_addr, 该字地址) 得到一位「命中」信号 → 与使能 AND → $wrmux 选旧值/新值
   所有写端口链式汇入 sig，接到 data_reg_in[i]（即该字 $dff 的 D）
```

地址译码 `addr_decode` 用分治：地址不到 2 位就用一个 `$eq`；否则对半切，递归译码两半再 `$and`，结果缓存在 `decoder_cache` 里避免重复。

需要特别处理的几种「不展开为 `$dff`」的特殊情况：

- **纯 ROM**（无写端口且无写时钟）：每个字直接用初值常数 / 静态写值，省掉触发器。
- **静态写端口**：地址、数据、使能全为常数 → 把对应字直接接到常数，不生成动态写入逻辑。
- **formal 流程**（`-formal`）：ROM 部分用无时钟的 `$ff`，并设置 `hdlname` 属性方便形式验证工具对照；还有限度支持 `clk2fflogic` 产生的异步写端口。
- **混合时钟 / 无时钟写端口**：`memory_map` 会打印「Not mapping ...」并放弃映射该存储器（保持 `$mem_v2` 不变），交由用户处理。

#### 4.5.3 源码精读

地址译码的分治实现：

[passes/memory/memory_map.cc:85-104](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_map.cc#L85-L104) —— `addr_decode`：地址不到 2 位时用 `module->Eq` 比较；否则对半切，递归译码两半再用 `module->And` 合并；结果缓存进 `decoder_cache`，同一个「地址对某值」的译码只造一次。

为每个字造存储元件的主循环：

[passes/memory/memory_map.cc:205-270](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_map.cc#L205-L270) —— 遍历每个字：静态写命中则接常数（`static_cells_map`）；否则造 `$dff`（或 formal 下的 `$ff`/异步下的 `$ff`），设 `WIDTH` 参数、接 `D=w_in`、`Q=w_out`，并把初值 `w_init` 写到 `w_out` 的 `init` 属性。日志在 [272-273 行](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_map.cc#L272-L273) 汇报造了几个 `$dff`/静态字。

读接口的二叉 `$mux` 树：

[passes/memory/memory_map.cc:288-308](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_map.cc#L288-L308) —— 每深入一位地址（`abits - wide_log2` 层），对当前每个候选信号造一个 `$mux`，选择端 `S` 取该地址位（`rd_addr.extract(abits-j-1, 1)`，MSB 优先），`A`/`B` 各接一根新线，于是候选信号数量每层翻倍，最终得到 \(2^a\) 个叶子。

写接口的 `$wrmux` 链：

[passes/memory/memory_map.cc:328-381](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/passes/memory/memory_map.cc#L328-L381) —— 对每个字、每个写端口：先用 `addr_decode` 得到命中位 `w_seladdr`，若使能不恒为 1 再 `$and` 上逐位使能（`$wren`），最后用 `$wrmux`（`$mux`）在「旧值 `sig`」与「新数据」间选择，结果回写到 `sig`；所有写端口链式汇入后接到 `data_reg_in[idx]`，即该字 `$dff` 的 D 端。

最后 `mem.remove()` 删掉原 `$mem_v2` 与 `RTLIL::Memory`，整个存储器彻底变为门级网表。

#### 4.5.4 代码实践

**实践目标：** 在一个小 ROM/RAM 上亲眼看到 `$dff` 与 `$mux` 的生成。

**操作步骤：**

1. 准备 `mem_test.v`：

   ```verilog
   module mem_test(input clk, input [1:0] a, input [1:0] wa,
                   input [7:0] wd, input we, output reg [7:0] rd);
       reg [7:0] mem [0:3];
       always @(posedge clk) if (we) mem[wa] <= wd;
       always @(*) rd = mem[a];
   endmodule
   ```

2. 综合并映射：`read_verilog mem_test.v; proc; opt; memory_map; write_rtlil out.il`。
3. 在 `out.il` 中搜索 `$dff`、`$mux`、`$wrmux`、`$rdmux`。

**需要观察的现象：** 出现 4 个 `width 8` 的 `$dff`（每个字一个），3 个读端 `$mux`（\(N-1=3\)，二叉树），以及写端每个字对应一组 `$and`（`$wren`）+ `$mux`（`$wrmux`）。

**预期结果：** 你能指着 RTLIL 文本说：这根是某字的 Q 输出、这棵 `$mux` 树是读译码、这条 `$wrmux` 链是写入路径。若本地未装 yosys，标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1：** 一个 256 字的存储器，单读端口，`memory_map` 后读端会产生多少个 `$mux`？
**答案：** \(256 - 1 = 255\) 个。读端是一棵地址位宽 \(a=8\) 的满二叉树，叶子 256 个、内部节点 255 个，每个内部节点一个 `$mux`。

**练习 2：** 为什么 `addr_decode` 要用对半分治的 AND 树，而不是直接比较整个地址？
**答案：** 分治把「整个地址相等」拆成一棵平衡的 AND 树，可以让 `abc` 等后续逻辑优化更容易复用子表达式、平衡延迟；同时配合 `decoder_cache` 复用相同子比较，减少门数。直接整个比较虽然在语义上等价，但会生成更扁平、更难共享的逻辑。

**练习 3：** 一个存储器若某个写端口没有时钟（异步写），`memory_map` 会怎样？
**答案：** 默认（非 formal）流程下，`memory_map` 会打印「write port has no clock」并放弃映射该存储器（保持 `$mem_v2` 不变），因为异步写无法用同步 `$dff` 表达；只有 `-formal` 流程下才有限度地用 `$ff` 处理 `clk2fflogic` 产生的这种端口。

## 5. 综合实践

把整讲串起来：亲手走一遍「端口单元 → `$mem_v2` → 触发器与门」的完整旅程，并记录每个阶段的单元数变化。

**任务：** 对下面这个含读写的 4×8 存储器设计，分阶段综合并统计。

```verilog
// mem_test.v
module mem_test(input clk, input [1:0] a, input [1:0] wa,
                input [7:0] wd, input we, output reg [7:0] rd);
    reg [7:0] mem [0:3];
    integer i;
    initial for (i = 0; i < 4; i = i + 1) mem[i] = 8'h00;
    always @(posedge clk) if (we) mem[wa] <= wd;
    always @(*) rd = mem[a];
endmodule
```

**步骤：**

1. `read_verilog mem_test.v` → `write_rtlil step0.il`：观察 `memory ... \mem` 声明 + 散落的 `$memrd`/`$memwr`/`$meminit` 端口单元。
2. `proc; opt_clean` → `write_rtlil step1.il`：行为级 `always` 已变门级，但存储器仍是端口单元。
3. `memory_collect`（或 `memory -nomap`）→ `write_rtlil step2.il`：端口单元合并成单个 `$mem_v2`，`memory` 声明消失。
4. `memory_map` → `write_rtlil step3.il`：`$mem_v2` 展开成 4 个 `$dff` + 一棵读 `$mux` 树 + 写端 `$and`/`$mux`。
5. 每步用 `stat` 记录 `$memrd`/`$memwr`/`$mem_v2`/`$dff`/`$mux` 的数量，做成一张变迁表。

**预期结果：** 你应当看到 `$memrd`/`$memwr` 数量在 step0→step2 从「多个」收敛到 0（被 `$mem_v2` 取代），再到 step3 连 `$mem_v2` 也消失，转化为 \(N\) 个 `$dff` 与读/写多路器。这张表就是本讲三条主线（collect 打包、share 合并、map 物化）的最佳注脚。

## 6. 本讲小结

- `memory` 是一条编排型 pass，自身不做算法，按固定黄金顺序串联 `opt_mem*` → `memory_bmux2rom` → `memory_dff` → `memory_share` → `opt_mem_widen` → `memory_collect` → `memory_map` 等子 pass。
- 存储器的 RTLIL 表示分两层：`RTLIL::Memory`（仅几何信息 width/start_offset/size）+ 一组读/写端口单元；所有 pass 通过 `kernel/mem.h` 的 `Mem` 辅助类统一载入、操作、`emit()` 写回。
- `memory_collect` 把零散端口打包成单个 `$mem_v2`（代码极短：置 `packed=true` 再 `emit()`）；当前单元族为 `_v2` 形态，旧 `$mem` 由 `Mem` 类兼容并归一化。
- `memory_share` 用「按地址合并读/写端口」「相邻地址拓宽成宽端口」「SAT 证明两写端口永不同时激活」三种手段减少端口数。
- `memory_narrow` 方向相反（把宽端口拆窄），且**不在**默认 `memory` 流水线内，是供特殊流程按需调用的独立 pass。
- `memory_map` 把 `$mem_v2` 物化为「每字一个 `$dff` + 读端 `$mux` 二叉树 + 写端地址译码 `$wrmux`」，对 ROM/静态写/formal/异步写有专门处理，混合时钟或无时钟写端口时会放弃映射。

## 7. 下一步学习建议

- **衔接工艺映射：** `memory_map` 产出的是通用 `$dff`/`$mux`，下一讲 u6-l5 的 `techmap`/`simplemap` 会把它们进一步映射到底层门或库单元；之后 u6-l6 的 `abc9`/`dfflibmap` 会做逻辑优化与标准单元映射。
- **进阶存储器映射：** 想了解「存储器映射到 FPGA 块 RAM」的厂商流程，可读 `passes/memory/memory_bram.cc`（`-bram` 选项调用）与 u8-l2 的 `synth_xilinx`/`synth_ice40`，它们正是在 coarse 阶段保留的 `$mem_v2` 形态上做 BRAM 推断。
- **SAT 应用：** 4.3 的 SAT 写端口共享是 u10-l1 SAT 基础设施的综合用例，建议学完 u10-l1 后回看 `memory_share.cc` 的 `consolidate_wr_using_sat`，对照体会 `QuickConeSat` 的用法。
- **源码延伸：** 通读 [kernel/mem.h](https://github.com/YosysHQ/yosys/blob/45ea2b8d6c6e94b06ff39b0117f0961ae5c16561/kernel/mem.h) 与 `kernel/mem.cc`，理解 `Mem` 类的 `narrow`/`widen_wr_port`/`emulate_transparency` 等方法，能让你对存储器端口的各种等价改写有完整把握。
